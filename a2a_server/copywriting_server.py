"""
需求：实现基于 A2A 的小红书电商种草（开箱测评）文案服务器

架构说明：
    本服务器是 SmartVoyage 电商系统中的文案子代理（CopywritingAssistant），
    运行在独立进程中（127.0.0.1:5009），通过 A2A 协议与主助手通信。

    它与其他子代理最大的区别是：它自己会同时作为 A2A 客户端与 MCP 客户端，
    按需收集真实素材，而不是凭记忆编造。形成两层 Agent 协作：

        主助手 ChatService
             │ A2A
             ▼
        CopywritingAssistant (5009)
             ├─ 商品 MCP (8001) ──→ MySQL（结构化事实：价格/原价/库存/评分/规格/优惠券）
             ├─ 推荐 Agent (5005) ──→ 推荐 MCP (8002) ──→ Milvus（语义找货/替代款）
             ├─ 订单 Agent (5006) ──→ 商品 MCP (8001)（文字版比价结论与优惠说明）
             └─ 图片 MCP (8004) ──→ 商品实拍图检索（360/Bing/开源图库）

    为什么既用 MCP 又用 A2A？
    - 结构化事实（价格、原价、库存、规格）必须原样准确，走商品 MCP 拿 JSON 最可靠；
    - 语义找货、比价结论这类需要推理的活儿，交给导购/订单 Agent 更符合职责划分。

    输出内容（固定 7 段，便于评估脚本做格式与事实校验）：
    - 【标题备选】3 个
    - 【正文】600~900 字第一人称开箱体验
    - 【商品信息】由系统按真实商品数据确定性重写（不交给 LLM 复述，杜绝价格写错）
    - 【商品实拍】由系统按真实图片检索结果填充
    - 【配图建议】
    - 【标签】
    - 【数据说明】哪些来自实时查询，哪些是主观体验

    三道防幻觉闸门：
    1. 提示词铁律：只允许引用工具返回的参数，禁止编造实测数据与跨品牌对比；
    2. 确定性重写：【商品信息】段落用工具返回的 JSON 直接生成，价格/库存零容错；
    3. 生成后校对：用事实清单对正文做二次校对，删掉无依据的数字与"实测"话术。
"""

# ==================== 导入依赖 ====================
import asyncio  # 异步 IO
import json  # 解析 MCP / 图片返回的 JSON
import os  # 环境变量
import re  # 商品名抽取与段落替换
import uuid  # 任务 ID

from datetime import datetime  # 当前日期注入
import pytz  # 时区

from fastmcp import Client  # 商品/图片 MCP 客户端
from langchain_mcp_adapters.tools import load_mcp_tools  # MCP 工具 → LangChain 工具

from python_a2a import (
    A2AServer, run_server, AgentCard, AgentSkill,
    TaskStatus, TaskState,
    TextContent, Message, MessageRole, Task,
)
from python_a2a.client import A2AClient

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from langchain.agents import create_tool_calling_agent, AgentExecutor

# 确保项目根目录在 sys.path 中（支持直接以脚本方式运行 python a2a_server/xxx.py）
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from create_logger import logger

conf = Config()

# ==================== 初始化大模型 ====================
llm = ChatOpenAI(**conf.llm_config)

# ==================== 下游 A2A 代理地址 ====================
# 文案代理需要向其他代理取素材，地址支持本地和 Docker 环境
AGENT_URLS = {
    "RecommendationAssistant": os.getenv("RECOMMEND_A2A_URL", "http://127.0.0.1:5005"),
    "OrderAssistant": os.getenv("ORDER_A2A_URL", "http://127.0.0.1:5006"),
}

# 单个下游代理的超时时间（秒）
AGENT_TIMEOUT = int(os.getenv("COPYWRITING_AGENT_TIMEOUT", "180"))

# 商品 MCP（结构化事实来源，端口 8001）
PRODUCT_MCP_URL = os.getenv("PRODUCT_MCP_URL", "http://127.0.0.1:8001/mcp")
MCP_TIMEOUT = int(os.getenv("COPYWRITING_MCP_TIMEOUT", "45"))

# 只允许文案代理使用"只读"的商品工具：绝不把 create_order 暴露给写文案的 Agent
ALLOWED_MCP_TOOLS = {
    "search_products", "get_product", "compare_price",
    "list_coupons", "calculate_coupon",
}

# 商品实拍图 MCP（独立进程，端口 8004）
IMAGE_MCP_URL = os.getenv("IMAGE_MCP_URL", "http://127.0.0.1:8004/mcp")
IMAGE_TIMEOUT = int(os.getenv("IMAGE_MCP_TIMEOUT", "60"))
IMAGE_MAX_PRODUCTS = int(os.getenv("IMAGE_MAX_PRODUCTS", "3"))

# 下游代理（导购/订单）内部只维护一个 MCP 客户端，
# 并发调用会互相干扰甚至卡死，因此文案代理串行地向它们取素材。
_DOWNSTREAM_LOCK = asyncio.Lock()


# ==================== 调用其他 A2A 代理 ====================
async def call_agent(agent_name: str, query: str) -> str:
    """
    通过 A2A 协议调用其他子代理，并把返回结果转成文本。

    参数：
        agent_name (str): 下游代理名称，必须是 AGENT_URLS 中的 key
        query (str): 交给下游代理的自然语言查询

    返回值：
        str: 下游代理返回的文本；调用失败时返回可读的错误说明，
             让文案 Agent 知道该素材缺失，而不是编造数据。
    """
    url = AGENT_URLS.get(agent_name)
    if not url:
        return f"未配置 {agent_name} 的地址，无法获取该数据。"

    client = A2AClient(url, timeout=AGENT_TIMEOUT)
    message = Message(content=TextContent(text=query), role=MessageRole.USER)
    task = Task(id="copy-" + str(uuid.uuid4()), message=message.to_dict())

    try:
        # 同一时刻只调用一个下游代理，避免多个请求争用下游的 MCP 客户端
        async with _DOWNSTREAM_LOCK:
            raw_response = await client.send_task_async(task)
        logger.info(f"文案代理调用 {agent_name} 返回: {raw_response}")

        if raw_response.status.state == "completed" and raw_response.artifacts:
            return raw_response.artifacts[0]["parts"][0]["text"]

        if raw_response.status.message:
            return raw_response.status.message.get("content", {}).get(
                "text", str(raw_response.status.message)
            )

        return f"{agent_name} 未返回有效数据。"

    except Exception as e:
        logger.error(f"文案代理调用 {agent_name} 失败: {e}")
        return (
            f"{agent_name} 暂时不可用（{e}）。"
            "请跳过该项数据，不要编造，并在【数据说明】中写明未获取到。"
        )


# ==================== 封装成 LangChain 工具 ====================
# 这些工具的 docstring 会作为工具描述交给 LLM，
# 所以必须写清楚：查什么、query 需要包含哪些参数、数据如何使用。

@tool
async def query_product_recommendation(query: str) -> str:
    """按自然语言需求，从向量库语义检索最匹配的商品，返回商品名、品牌、价格、评分和推荐理由。
    query 用自然语言描述即可，例如：“300元以内适合通勤的降噪耳机”。
    适合用来找"替代款 / 同价位还有什么选择"；
    如果用户已经指明具体商品型号，请优先用 search_products 查准，而不是用本工具。"""
    return await call_agent(
        "RecommendationAssistant",
        f"请检索并列出以下需求最匹配的商品（名称、品牌、价格、评分、推荐理由），最多 3 款：{query}\n"
        "注意：只返回数据库真实存在的商品，找不到就明确说明未找到，不要编造商品。",
    )


@tool
async def query_price_and_coupons(query: str) -> str:
    """查询同品类商品的价格对比结论，以及当前可用的优惠券（券码、门槛、面额、适用范围）。
    query 用自然语言描述，例如：“降噪蓝牙耳机的价格对比和可用优惠券”。
    只有文案需要写"多少钱划算/有没有优惠/券后价/同价位怎么选"时才调用；
    需要精确的券码与面额时，优先用 list_coupons 拿原始数据。
    返回的券码与面额必须原样使用，不得编造不存在的优惠。"""
    return await call_agent(
        "OrderAssistant",
        f"请查询以下商品的价格情况与可用优惠券：{query}\n"
        "要求：列出同品类 3 款以内的真实价格对比，并列出真实可用的优惠券"
        "（券码、门槛、面额、适用范围）；没有可用券就明确说明。",
    )


A2A_TOOLS = [
    query_product_recommendation,
    query_price_and_coupons,
]


# ==================== 商品事实：抽取与确定性重写 ====================
def _loads(text):
    """尽力把工具返回的观察值解析成 JSON，失败返回 None。"""
    if isinstance(text, (dict, list)):
        return text
    if not isinstance(text, str):
        return None
    for candidate in (text, text.strip()):
        try:
            return json.loads(candidate)
        except (ValueError, json.JSONDecodeError):
            continue
    # 有些框架会把 JSON 包在列表或额外文字里，退一步做一次括号截取
    match = re.search(r"[\[{].*[\]}]", text, re.S)
    if match:
        try:
            return json.loads(match.group(0))
        except (ValueError, json.JSONDecodeError):
            return None
    return None


def _collect_facts(steps) -> tuple[dict, dict]:
    """
    从 AgentExecutor 的 intermediate_steps 中收集商品与优惠券的结构化事实。

    返回值：
        (products, coupons)：products 以商品 id 为键，coupons 以券码为键
    """
    products: dict = {}
    coupons: dict = {}

    for _action, observation in steps or []:
        data = _loads(observation)
        if not isinstance(data, dict):
            continue
        payload = data.get("data")
        if isinstance(payload, dict):
            items = [payload]
        elif isinstance(payload, list):
            items = payload
        else:
            continue

        for item in items:
            if not isinstance(item, dict):
                continue
            if "code" in item and ("coupon_type" in item or "threshold" in item):
                coupons[str(item["code"])] = item
            elif "name" in item and "price" in item:
                key = item.get("id") or item.get("product_id") or item["name"]
                products[key] = item

    return products, coupons


def _specs_text(specs) -> str:
    """把 specs（可能是 JSON 字符串或 dict）转成“键 值、键 值”的展示文本。"""
    if isinstance(specs, str):
        try:
            specs = json.loads(specs)
        except (ValueError, json.JSONDecodeError):
            return specs.strip()[:200]
    if isinstance(specs, dict):
        return "、".join(f"{k} {v}" for k, v in specs.items() if str(v).strip())
    return ""


def _pick_main_product(products: dict, user_input: str, copy_text: str) -> dict | None:
    """选出文案真正在写的那款商品（标题/正文/需求里出现最多、销量最高者优先）。"""
    if not products:
        return None

    def score(item: dict) -> tuple:
        name = str(item.get("name") or "")
        brand = str(item.get("brand") or "")
        total = 0
        if name and name in copy_text:
            total += 6
        if name and name in user_input:
            total += 10
        if brand and brand in user_input:
            total += 4
        if brand and brand in copy_text:
            total += 2
        return total, float(item.get("sales_volume") or 0)

    return max(products.values(), key=score)


def _best_coupon(coupons: dict, product: dict) -> tuple[dict | None, float]:
    """从已查询到的券里挑出该商品可用的、优惠力度最大的一张，并算出券后价。"""
    price = float(product.get("price") or 0)
    category = str(product.get("category") or "")
    best, best_final = None, price

    for coupon in coupons.values():
        scope = str(coupon.get("scope") or "all")
        if scope not in ("all", "", category):
            continue
        threshold = float(coupon.get("threshold") or 0)
        if price < threshold:
            continue
        value = float(coupon.get("value") or 0)
        if str(coupon.get("coupon_type")) == "discount":
            discount = price * (1 - value)
        else:
            discount = value
        final = round(max(0.0, price - discount), 2)
        if final < best_final:
            best, best_final = coupon, final
    return best, best_final


def _render_product_info(product: dict, coupon: dict | None, final_price: float) -> str:
    """用真实商品数据生成【商品信息】段落正文（不含标题），保证价格/库存零误差。"""
    price = float(product.get("price") or 0)
    original = float(product.get("original_price") or 0)
    rating = float(product.get("rating") or 0)
    stock = int(product.get("stock") or 0)
    specs = _specs_text(product.get("specs"))

    price_line = f"- 价格：¥{price:.2f}"
    if original > price:
        price_line += f"（原价 ¥{original:.2f}）"

    lines = [
        f"- 商品：{product.get('name', '')}",
        price_line,
        f"- 评分：{rating:.1f} 分｜库存：{stock} 件",
        f"- 核心规格：{specs or '数据库未提供'}",
    ]

    if coupon:
        threshold = float(coupon.get("threshold") or 0)
        value = float(coupon.get("value") or 0)
        if str(coupon.get("coupon_type")) == "discount":
            rule = f"{int(value * 10)}折"
        else:
            rule = f"立减 ¥{value:.2f}"
        threshold_text = f"满 ¥{threshold:.0f}" if threshold > 0 else "无门槛"
        lines.append(
            f"- 优惠：{coupon.get('code')} {coupon.get('name') or ''}"
            f"（{threshold_text}{rule}）"
            f"{'，券后 ¥%.2f' % final_price if final_price < price else ''}"
        )
    else:
        lines.append("- 优惠：未查询到该商品可用优惠券")

    return "\n".join(lines)


def build_fact_sheet(product: dict | None, coupon: dict | None, final_price: float,
                     products: dict) -> str:
    """生成给校对环节用的事实清单（纯文本，便于 LLM 对照）。"""
    if not product:
        return "（未获取到任何商品结构化数据）"

    price = float(product.get("price") or 0)
    original = float(product.get("original_price") or 0)
    rating = float(product.get("rating") or 0)
    stock = int(product.get("stock") or 0)
    specs = _specs_text(product.get("specs"))

    lines = [
        f"商品名称：{product.get('name', '')}",
        f"品牌：{product.get('brand', '')}",
        f"品类：{product.get('category', '')}",
        f"售价：¥{price:.2f}",
        f"原价：{'¥%.2f' % original if original else '数据库未提供'}",
        f"评分：{rating:.1f}",
        f"库存：{stock} 件",
        f"规格（仅这些参数可用）：{specs or '数据库未提供'}",
    ]
    if coupon:
        lines.append(
            f"可用优惠券：{coupon.get('code')} {coupon.get('name') or ''}"
            f"（门槛 ¥{float(coupon.get('threshold') or 0):.0f}，"
            f"优惠值 {coupon.get('value')}）→ 券后 ¥{final_price:.2f}"
        )
    else:
        lines.append("可用优惠券：无（不要写任何券码或券后价）")

    others = [
        f"{p.get('name')} ¥{float(p.get('price') or 0):.2f}"
        for p in products.values()
        if product and p.get("name") != product.get("name")
    ]
    if others:
        lines.append("同批检索到的其他商品（可做对比，仅限这些价格）：" + "；".join(others[:4]))

    return "\n".join(lines)


# ==================== 生成后校对（删掉无依据的数字与话术） ====================
PROOFREAD_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """你是电商文案的事实校对员。给你一份"事实清单"和一篇待发布的小红书种草笔记草稿，你的任务是**只做删改，不做润色**，确保草稿里每一个数字和参数都能在事实清单中找到依据。

必须执行：
1. 删掉或改写所有"事实清单里没有依据"的数字、参数与结论，包括但不限于：重量、尺寸、厚度、充电时间、续航次数、回血百分比、循环寿命、防水等级、屏幕/数显、接口具体组合、协议版本。
2. 删掉所有伪造的测试过程与来源：如"实测充了4次""15分钟回血30%""查了官网/详情页""评论区高频词是..."。可以保留主观感受，但要改写为明显的个人感受（如"用下来感觉挺耐用"）。
3. 删掉所有与事实清单价格不一致的数字；正文提到价格时必须与清单一致（售价 / 原价 / 券后价三者不能混用）。
4. 删掉与未在清单中的其他品牌/型号的价格、参数对比。
5. 保留原有的 7 个段落标题和顺序（【标题备选】【正文】【商品信息】【商品实拍】【配图建议】【标签】【数据说明】）；不要新增段落，不要解释你改了什么。
6. 正文长度尽量保持在 500 字以上：删掉不实内容后，用**主观体验**（手感、佩戴/握持感、使用场景、适合谁）补足，不要补数字。
7. 标题备选里如果出现无依据的数字（如具体重量、续航时长），也要改掉。
8. 删掉所有"反向推断的缺点"：凡出现"没有/不支持/缺少 + 某功能"（如"没有无线充电""没有数显屏""不支持 PD""没有防水"），只要事实清单里没写这个功能，就**直接整句删除**；不要插入"这部分商品页未标注，建议以下单页为准"这类占位句，避免语义悬空。
9. 删掉任何对具体指示灯数量、灯格比例、按键数量等硬件细节的描述，除非事实清单里明确写了。
10. 删掉或改写成模糊表达：包装盒图案/配色/联名、赠品、配件清单、耳塞数量与材质等**未在事实清单中出现的外观与配件描述**；
    并且描述不能与事实矛盾（例如事实清单写"半入耳"，就不能出现"硅胶耳塞""深入耳道"；写"半入耳"就不能写"入耳式降噪塞"）。

只输出修改后的完整笔记，不要输出任何说明性文字。"""),
    ("human", "【事实清单】\n{fact_sheet}\n\n【待校对草稿】\n{draft}"),
])


async def proofread_copy(draft: str, fact_sheet: str) -> str:
    """二次校对：失败时返回原稿，保证主流程不中断。"""
    try:
        response = await llm.ainvoke(PROOFREAD_PROMPT.format_messages(
            fact_sheet=fact_sheet,
            draft=draft[:8000],
        ))
        text = response.content if hasattr(response, "content") else str(response)
        text = str(text).strip()
        # 校对结果至少要保留核心段落，否则视为失败，回退原稿
        if "【正文】" in text and len(text) >= len(draft) * 0.4:
            return text
        logger.warning("校对结果异常（缺少段落或长度骤减），回退原稿")
    except Exception as exc:
        logger.warning(f"文案校对失败，回退原稿：{exc}")
    return draft


# ==================== 确定性兜底：剥离无依据的硬件细节 ====================
# LLM 在"开箱"段落特别爱编包装、配件、指示灯、触控这些细节，
# 而这些几乎不会出现在 specs/description 里。这里做一次确定性的句子级过滤，
# 只删除"该细节在工具返回中完全没有出现"的句子，避免与事实矛盾。
DETAIL_TERMS = (
    "配件", "附赠", "赠品", "耳塞", "耳翼", "耳帽", "耳挂", "硅胶",
    "包装盒", "包装", "说明书", "塑封", "磁吸",
    "指示灯", "呼吸灯", "数显", "屏幕显示", "触控", "按键", "开机键",
    "渐变", "哑光", "磨砂", "丝印", "logo",
)


def scrub_unsupported_details(text: str, product: dict | None) -> str:
    """删除正文中"工具未返回任何依据"的硬件/包装细节句，其余段落保持不动。"""
    if not product or "【正文】" not in text:
        return text

    allowed_source = " ".join([
        str(product.get("description") or ""),
        _specs_text(product.get("specs")),
        str(product.get("name") or ""),
    ])
    allowed = {term for term in DETAIL_TERMS if term in allowed_source}
    terms = [t for t in DETAIL_TERMS if t not in allowed]
    if not terms:
        return text

    pattern = re.compile(rf"{re.escape('【正文】')}.*?(?=\n【[^\n】]{{1,12}}】|\Z)", re.S)
    match = pattern.search(text)
    if not match:
        return text

    body = match.group(0)
    _, _, content = body.partition("【正文】")

    kept, removed = [], 0
    for line in content.split("\n"):
        # 按句切分，逐句判断是否含未获支持的细节词
        sentences = re.split(r"(?<=[。！？!?])", line)
        kept_sentences = []
        for sentence in sentences:
            if any(term in sentence for term in terms):
                removed += 1
                continue
            kept_sentences.append(sentence)
        rebuilt = "".join(kept_sentences)
        if rebuilt.strip():
            kept.append(rebuilt)

    if removed:
        logger.info(f"细节兜底过滤：删除 {removed} 个无依据细节句")

    new_body = "【正文】\n" + "\n".join(kept)
    return text[:match.start()] + new_body + text[match.end():]


# ==================== 商品实拍图（图片 MCP） ====================
# 文案写完后，先让 LLM 从文案中提取真正写到的商品，再调用图片 MCP 检索实拍图，
# 最后确定性地把【商品实拍】段落插入文案。这样图片 URL 一定来自真实检索结果，
# 不依赖 LLM 复制长链接，也不会出现编造的图片地址。

CATEGORY_NAMES = [
    "数码配件", "智能家居", "美妆个护", "服饰鞋包", "食品饮料", "运动户外",
]

PRODUCT_EXTRACT_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """你是小红书电商笔记的配图编辑。请从用户需求和已经生成的文案中，找出 1~3 个真正被写成主角/重点推荐的商品，用来检索商品实拍图。

规则：
- 只选具体的商品（有品牌或型号的商品名，例如“声海 X1 真无线蓝牙耳机”）；
- 不要选泛化品类词（“耳机”“充电宝”“零食”），不要选店铺、平台、快递、优惠券；
- 商品名最多 24 个字，必须能在文案或用户需求中找到对应内容；
- 品牌填商品品牌（找不到就填空字符串），品类只能从以下选一个：数码配件 / 智能家居 / 美妆个护 / 服饰鞋包 / 食品饮料 / 运动户外（拿不准就填空字符串）。

只输出 JSON，不要解释，格式：
{{"products": [{{"name": "声海 X1 真无线蓝牙耳机", "brand": "声海", "category": "数码配件"}}]}}"""),
    ("human", "用户需求：\n{user_input}\n\n已生成文案：\n{copy_text}"),
])


def _parse_product_json(raw: str) -> list[dict]:
    """从 LLM 输出中解析 {"products": [...]}，容忍 ```json 代码块。"""
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except (ValueError, json.JSONDecodeError):
        return []

    products = []
    for item in (data.get("products") or [])[:6]:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = re.sub(r"[\[\]【】\"'“”‘’#*]", "", str(item.get("name") or "")).strip()
        brand = re.sub(r"[\[\]【】\"'“”‘’#*]", "", str(item.get("brand") or "")).strip()
        category = str(item.get("category") or "").strip()
        if category not in CATEGORY_NAMES:
            category = next((c for c in CATEGORY_NAMES if c in category), "")
        if 2 <= len(name) <= 30 and all(p["name"] != name for p in products):
            products.append({"name": name, "brand": brand[:20], "category": category})
    return products


def _fallback_products(user_input: str, copy_text: str) -> list[dict]:
    """LLM 抽取失败时，用「已知品类词」从文案里兜底找商品名。"""
    keywords = (
        "真无线蓝牙耳机", "降噪蓝牙耳机", "蓝牙耳机", "头戴式耳机", "充电宝", "移动电源",
        "扫地机器人", "吸尘器", "空气炸锅", "电饭煲", "破壁机", "咖啡机", "电水壶",
        "机械键盘", "无线鼠标", "显示器", "路由器", "精华液", "面霜", "洁面乳", "面膜",
        "洗发水", "电动牙刷", "运动鞋", "跑步鞋", "双肩包", "卫衣", "咖啡豆", "坚果",
        "瑜伽垫", "哑铃", "帐篷",
    )
    found = []
    text = f"{user_input}\n{copy_text}"
    for keyword in keywords:
        index = text.find(keyword)
        if index < 0:
            continue
        # 往前取最多 12 个字当作品牌/型号部分（截断到空格或标点）
        prefix = text[max(0, index - 12):index]
        prefix = re.split(r"[\s，。、,.;；:：!！?？\n（）()【】\[\]]", prefix)[-1]
        name = f"{prefix}{keyword}".strip()
        if 3 <= len(name) <= 30 and all(p["name"] != name for p in found):
            found.append({"name": name, "brand": "", "category": ""})
        if len(found) >= IMAGE_MAX_PRODUCTS:
            break
    return found


async def extract_products(user_input: str, copy_text: str) -> list[dict]:
    """优先用 LLM 抽取商品，失败时退回关键词兜底，保证图片功能不会阻塞文案返回。"""
    try:
        response = await llm.ainvoke(PRODUCT_EXTRACT_PROMPT.format_messages(
            user_input=user_input[:2000],
            copy_text=copy_text[:6000],
        ))
        raw = response.content if hasattr(response, "content") else str(response)
        products = _parse_product_json(str(raw))
        if products:
            return products[:IMAGE_MAX_PRODUCTS]
    except Exception as exc:
        logger.warning(f"商品名称抽取失败，使用关键词兜底：{exc}")

    return _fallback_products(user_input, copy_text)[:IMAGE_MAX_PRODUCTS]


def _safe_md_url(url: str) -> str:
    """只允许 http(s) 且不含空格/括号，避免破坏 Markdown 或注入属性。"""
    url = str(url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        return ""
    if any(ch in url for ch in " \t\r\n\"'<>"):
        return ""
    return url.replace(")", "%29").replace("(", "%28")


def _safe_md_text(text: str) -> str:
    return re.sub(r"[\[\]*`_<>]", "", str(text or "")).strip()


async def _fetch_product_photo(client: Client, product: dict) -> dict | None:
    result = await client.call_tool(
        "search_product_photos",
        {
            "product": product["name"],
            "brand": product.get("brand", ""),
            "category": product.get("category", ""),
            "count": 1,
        },
        timeout=IMAGE_TIMEOUT,
    )
    data = getattr(result, "data", None)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, json.JSONDecodeError):
            data = None
    if not isinstance(data, dict):
        text = "".join(
            block.text for block in getattr(result, "content", []) if hasattr(block, "text")
        )
        try:
            data = json.loads(text)
        except (ValueError, json.JSONDecodeError):
            return None
    if data.get("status") != "success" or not data.get("images"):
        return None
    image = dict(data["images"][0])
    image["match_level"] = data.get("match_level", "name")
    return image


def render_product_section(photos: list[tuple[dict, dict]], error: str = "") -> str:
    """生成【商品实拍】正文（不含标题），图片和来源都来自图片 MCP 的真实返回。"""
    lines = []
    for index, (product, image) in enumerate(photos, start=1):
        image_url = _safe_md_url(image.get("image_url", ""))
        if not image_url:
            continue
        name = _safe_md_text(product["name"])
        title = _safe_md_text(image.get("title") or name)[:80]
        site = _safe_md_text(image.get("site") or "")
        page_url = _safe_md_url(image.get("page_url", ""))
        license_name = _safe_md_text(image.get("license") or "")

        # 只有型号级命中才算商品实拍；品牌其他型号 / 同类商品都如实标注
        level = image.get("match_level") or "generic"
        if level in ("exact", "name"):
            label = "商品实拍"
        elif level == "brand":
            label = "品牌同系列参考图（非该型号实拍）"
        else:
            label = "同类商品参考图（未检索到该商品实拍）"

        lines.append(f"{index}. **{name}** —— {label}")
        lines.append(f"![{name}实拍]({image_url})")
        source = f"图源：{title}"
        if site and site not in title:
            source += f" · {site}"
        if page_url:
            source += f"｜[原图页]({page_url})"
        if license_name and license_name.lower() != "unknown":
            source += f"｜授权：{license_name}"
        else:
            source += "｜授权未知，商用需取得原作者授权"
        lines.append(source)
        lines.append("")

    if not lines:
        reason = "图片检索服务暂时不可用" if error else "暂未检索到可用的商品图片"
        lines.append(f"{reason}，可参考下方【配图建议】。")
    return "\n".join(lines).strip()


def _replace_section(text: str, heading: str, body: str) -> str:
    """替换同名段落内容；原段落缺失时插入到【配图建议】之前。"""
    pattern = re.compile(rf"{re.escape(heading)}.*?(?=\n【[^\n】]{{1,12}}】|\Z)", re.S)
    if pattern.search(text):
        return pattern.sub(f"{heading}\n{body}\n", text, count=1).rstrip() + "\n"

    for anchor in ("【配图建议】", "【标签】", "【数据说明】"):
        index = text.find(anchor)
        if index != -1:
            return (
                text[:index].rstrip()
                + f"\n\n{heading}\n{body}\n\n"
                + text[index:].lstrip()
            )
    return text.rstrip() + f"\n\n{heading}\n{body}\n"


def _append_image_note(text: str, note: str) -> str:
    if "【数据说明】" in text:
        return text.rstrip() + "\n" + note
    return text.rstrip() + "\n\n【数据说明】\n" + note


async def attach_product_photos(copy_text: str, user_input: str) -> str:
    """
    为已生成的文案补充真实商品实拍图，保留原有【配图建议】。

    流程：LLM 抽取商品 → 调图片 MCP（8004）逐张检索 → 确定性写入【商品实拍】。
    图片服务失败时仍然返回文案，并在段落与数据说明中写明未获取到。
    """
    products = await extract_products(user_input, copy_text)
    logger.info(f"文案配图：products={products}")

    photos: list[tuple[dict, dict]] = []
    error = ""
    if products:
        try:
            async with Client(IMAGE_MCP_URL, timeout=IMAGE_TIMEOUT) as client:
                for product in products[:IMAGE_MAX_PRODUCTS]:
                    try:
                        image = await _fetch_product_photo(client, product)
                    except Exception as exc:
                        error = str(exc)
                        logger.warning(f"检索“{product['name']}”实拍图失败：{exc}")
                        continue
                    if image:
                        photos.append((product, image))
        except Exception as exc:
            error = str(exc)
            logger.warning(f"连接图片 MCP 失败：{exc}")

    section = render_product_section(photos, error=error)
    output = _replace_section(copy_text, "【商品实拍】", section)

    if photos:
        providers = sorted({image.get("provider") or "" for _, image in photos} - {""})
        source = "、".join(providers) or "图片检索"
        exact = sum(1 for _, image in photos
                    if (image.get("match_level") or "generic") in ("exact", "name"))
        note = (
            f"- 商品图片：共 {len(photos)} 张，{exact} 张为该型号实拍、"
            f"{len(photos) - exact} 张为品牌/同类参考图，来自 {source} 的真实图片检索；"
            "图片版权归原作者，仅用于课程演示，商用请通过原图页取得授权。"
        )
    else:
        note = "- 商品图片：未检索到可用图片，已在【商品实拍】中说明。"
    return _append_image_note(output, note)


# ==================== 文案 Agent 系统提示 ====================
def build_prompt():
    """
    构建文案 Agent 的提示模板。

    LangChain 会把 {current_date} 替换成真实日期，
    {input} 是主助手转发过来的用户需求（可能包含对话历史）。
    """
    return ChatPromptTemplate.from_messages([
        ("system", """你是一位资深的小红书电商内容策划与文案撰写专家，擅长把商品写成有真实体验感、可收藏、能带货的开箱种草笔记。读者是正在比价、准备下单的年轻消费者。

【第一步：整理简报】
先从用户当前需求和对话历史中提取：商品（品牌/型号或品类）、预算、人群/使用场景、必须提到的卖点、笔记风格。
- 追问的门槛很高：**只有当需求里完全没有任何商品、品类线索（例如只说"帮我写个种草笔记"）时**，才输出一句追问：
  需要补充信息：请告诉我要种草的商品或品类（有品牌/型号更好）。
  这种情况下不要输出标题或正文。
- 只要出现了具体商品名、品牌、型号或品类（如"倍思充电宝""20000mAh 充电宝""降噪耳机""面膜"），**必须直接进入第二步查事实，绝不追问**。
- 缺预算、缺人群、缺使用场景都不是追问理由：按常识合理设定（如"通勤场景""大众消费者"），并在【数据说明】里注明这是默认假设。
- 用户明确给出的信息优先，不能自行改动商品、预算和价格。

【第二步：先查事实，再写文案】
写正文前必须先调用工具拿真实商品数据，绝不凭记忆编造商品、价格、参数：
- 用 search_products 查准商品（可按 keyword / brand / category / max_price 过滤），
  它会返回商品 id、名称、品牌、价格、原价、库存、评分、specs（规格）和 description（卖点）。
- 用 list_coupons 拿真实优惠券（券码、门槛、面额、适用范围），需要时用 calculate_coupon 试算券后价。
- 用 query_product_recommendation 找"同价位替代款/还有什么选择"；
  用 query_price_and_coupons 拿文字版的比价结论。
- 工具返回为空或明确说未找到时，不要硬写：直接说明"未找到该商品"，并建议用户换个关键词或放宽预算。
- 严禁虚构：不存在的型号、编造的参数、编造的优惠券、编造的销量评价都属于严重错误。

【铁律·参数只能来自工具返回】
- 只允许引用工具返回的 specs / description 里明确出现的参数（例如 specs 里写"续航 6h+24h""蓝牙 5.3""半入耳"，才能写这些）。
- 工具没返回的参数——重量、尺寸、厚度、防水等级、充电盒总续航、编解码协议、APP 功能、是否数显、接口具体组合——一律不写具体数值，也不要写"查了官网参数""看了详情页"这类伪造来源。
- **禁止编造测试过程与测试数据**：不得出现"实测充了 4 次""15 分钟回血 30%""续航跑了 3 天""我测了 78%"这类结论性数字，除非工具返回里就有。
- **禁止跨品牌/跨型号对比**：只有工具返回里明确出现了对比对象的名称和价格，才可以做价格对比（例如 search_products 同时返回了小米某款 ¥149）。
- 不要反向推断缺点：工具没提到防水，就不能写"没有 IPX5 防水"；没提到无线充电，就不能写"不支持无线充电"。缺点只能写明显的主观体验，并说明是自己感受。
- 不要编造包装与配件细节（包装图案/配色/联名、赠品、配件清单、耳塞数量与材质），也不要写与佩戴方式矛盾的描述
  （例如 specs 是"半入耳"，就不能出现"硅胶耳塞""塞入耳道"）。
- 价格/原价/库存/评分必须与工具返回完全一致，不得改写、四舍五入或自行推算。
- 【商品信息】段落不要自己编数字：系统会在你写完后用真实数据重写这一段，你只要按格式给出结构即可。

【第三步：写一篇真实感的开箱种草笔记】
风格要求：
- 第一人称，像朋友分享真实开箱体验，不像商品详情页或官方广告。
- 标题 3 个备选，每个 20 字以内，可带 1 个 emoji，有钩子但不夸张，**标题里不要出现未返回的具体参数**。
- 正文 600~900 字，短段落，适量 emoji（每段最多 2 个），可用【开箱】【上手】【亮点】【值不值得买】这类小标题。
- 正文必须自然写到：为什么买/什么场景用、**初次上手的第一印象（只说整体质感与佩戴/握持感受，不列举包装内容与配件）**、2~3 个有依据的卖点、一段"适合谁/不适合谁"的客观提醒、价格与性价比判断。
- "不适合谁"只能用**使用场景与人群**来描述（例如"不适合每天只带手机、追求极致轻便的人"），
  **不能写成"没有某功能/不支持某协议/缺少某配置"**——那属于对产品的断言，而你只知道工具返回的信息。
- 不要出现"作为AI""根据数据""工具显示""以下是"等字眼。
- 正文结尾用一句轻松的话引导收藏、评论或"蹲个链接"。
- 正文里只写体验和卖点，不要重复输出【商品信息】【商品实拍】【配图建议】【标签】这些小标题。
- 6~10 个小红书话题标签必须单独放在【标签】段落。

【输出格式】
必须严格按以下顺序输出 7 个段落标题，标题文字原样保留，一个都不能省略，
也不要自创或合并段落；不要添加额外解释：

【标题备选】
1. ...
2. ...
3. ...

【正文】
（完整的小红书开箱种草正文）

【商品信息】
- 商品：
- 价格：
- 评分：｜库存：
- 核心规格：
- 优惠：
（这一段的数字系统会用真实数据覆盖，你按上面字段填即可）

【商品实拍】
（这里由系统根据真实图片检索结果自动填充，你只需要原样保留这个标题，并写一行"由系统检索真实商品实拍图"，不要编造图片链接）

【配图建议】
1. ...
2. ...
3. ...

【标签】
#... #... #...

【数据说明】
- 商品信息/价格/优惠：来自实时检索（写查询日期）
- 主观体验：作者个人感受，非实测数据
- 未获取到的信息：...

当前日期：{current_date}。"""),
        ("human", "{input}"),
        ("placeholder", "{agent_scratchpad}"),
    ])


# ==================== 文案生成核心逻辑 ====================
PRODUCT_HINTS = (
    "耳机", "充电宝", "移动电源", "扫地机", "机器人", "吸尘器", "空气炸锅", "电饭煲",
    "破壁机", "咖啡机", "电水壶", "键盘", "鼠标", "显示器", "路由器", "摄像头",
    "精华", "面霜", "洁面", "面膜", "洗发水", "牙刷", "口红", "唇釉",
    "运动鞋", "跑步鞋", "板鞋", "双肩包", "卫衣", "T恤", "牛仔裤", "袜子",
    "咖啡豆", "坚果", "牛奶", "气泡水", "零食", "礼盒",
    "瑜伽垫", "哑铃", "跳绳", "帐篷", "露营",
)


def _has_product_hint(text: str) -> bool:
    """粗判需求里是否已经给出商品线索，用于兜底阻止模型无端追问。"""
    return any(hint in (text or "") for hint in PRODUCT_HINTS)


async def _run_copywriting_agent(conversation: str, current_date: str) -> tuple[str, dict, dict, dict | None, float]:
    """
    跑一次带工具的文案 Agent，并抽取结构化事实。

    返回值：
        (草稿文本, products, coupons, 主商品, 券后价)
    """
    async with Client(PRODUCT_MCP_URL, timeout=MCP_TIMEOUT) as mcp_client:
        mcp_tools = await load_mcp_tools(mcp_client.session)
        # 只保留只读工具，防止文案代理误触下单
        read_only = [t for t in mcp_tools if t.name in ALLOWED_MCP_TOOLS]
        blocked = sorted({t.name for t in mcp_tools} - ALLOWED_MCP_TOOLS)
        if blocked:
            logger.info(f"文案代理已屏蔽非只读工具: {blocked}")

        tools = read_only + A2A_TOOLS
        agent = create_tool_calling_agent(llm, tools, build_prompt())
        executor = AgentExecutor(
            agent=agent,
            tools=tools,
            verbose=True,
            max_iterations=14,
            return_intermediate_steps=True,
        )
        response = await executor.ainvoke({
            "input": conversation,
            "current_date": current_date,
        })

    draft = (response.get("output") or "").strip()
    products, coupons = _collect_facts(response.get("intermediate_steps"))
    logger.info(
        f"文案事实收集：products={len(products)}, coupons={len(coupons)}"
    )

    main_product = _pick_main_product(products, conversation, draft)
    coupon, final_price = (None, 0.0)
    if main_product:
        coupon, final_price = _best_coupon(coupons, main_product)
        logger.info(
            f"文案主商品：{main_product.get('name')} ¥{main_product.get('price')}"
            f"，券={coupon.get('code') if coupon else '-'}，券后={final_price}"
        )
    return draft, products, coupons, main_product, final_price


async def query_copywriting(conversation: str) -> dict:
    """
    执行一次小红书电商种草（开箱测评）文案生成。

    流程：
    1. 带工具（商品 MCP + 导购/订单 A2A）的 LangChain Agent 生成初稿
    2. 从工具调用记录里抽真实商品/优惠券 → 确定性重写【商品信息】
    3. 用事实清单对全文做二次校对，删掉无依据的数字与伪造实测
    4. 抽商品 → 图片 MCP 检索 → 写入【商品实拍】

    参数：
        conversation (str): 用户需求 + 由主助手拼接的对话历史

    返回值：
        dict: {"status": "success"/"error", "message": "..."}
    """
    try:
        current_date = datetime.now(pytz.timezone("Asia/Shanghai")).strftime("%Y-%m-%d")

        draft, products, coupons, main_product, final_price = await _run_copywriting_agent(
            conversation, current_date
        )
        if not draft:
            return {"status": "error", "message": "文案生成为空，请重试。"}

        # 兜底：需求里明明有商品线索，模型却误判信息不足 → 强指令重跑一次
        if draft.startswith("需要补充信息") and _has_product_hint(conversation):
            logger.warning("模型误判信息不足，按强指令重跑一次：先查商品再写")
            retry_input = (
                f"{conversation}\n\n"
                "【系统指令】用户已明确给出商品线索，禁止追问、禁止输出\"需要补充信息\"。"
                "请立刻调用 search_products 查询该商品，拿到真实价格与规格后，"
                "按 7 个固定段落输出完整笔记。"
            )
            draft, products, coupons, main_product, final_price = await _run_copywriting_agent(
                retry_input, current_date
            )
            if not draft or draft.startswith("需要补充信息"):
                return {"status": "error", "message": "未能检索到该商品，请换个关键词或补全型号后重试。"}

        # 信息不足时直接追问，不做后续处理
        if draft.startswith("需要补充信息"):
            return {"status": "success", "message": draft}

        # 主商品与可用优惠券（用于确定性重写【商品信息】）
        coupon, final_price = (None, 0.0)
        if main_product:
            coupon, final_price = _best_coupon(coupons, main_product)

        output = draft

        # 闸门 2：用真实数据重写【商品信息】，避免 LLM 复述错价格/库存
        if main_product:
            info_body = _render_product_info(main_product, coupon, final_price)
            output = _replace_section(output, "【商品信息】", info_body or "- 商品：未获取到")

        # 闸门 3：事实清单校对，删掉无依据数字与"实测"话术
        if products:
            fact_sheet = build_fact_sheet(main_product, coupon, final_price, products)
            output = await proofread_copy(output, fact_sheet)
            # 校对可能改动段落，重写一次商品信息确保数字仍然权威
            if main_product:
                output = _replace_section(
                    output, "【商品信息】",
                    _render_product_info(main_product, coupon, final_price),
                )
                # 保险：正文里若残留错误原价，直接纠正
                original = float(main_product.get("original_price") or 0)
                price = float(main_product.get("price") or 0)
                if original > price:
                    output = re.sub(
                        r"原价\s*¥?\s*%d(?:\.0+)?" % int(price),
                        f"原价 ¥{original:.0f}",
                        output,
                    )

        # 闸门 4：确定性剥离无依据的硬件/包装细节（LLM 校对不稳定，这里兜底）
        if main_product:
            output = scrub_unsupported_details(output, main_product)

        # 配图：真实商品实拍图
        try:
            output = await attach_product_photos(output, conversation)
        except Exception as exc:
            logger.warning(f"文案配图失败，返回纯文案：{exc}")

        return {"status": "success", "message": output}

    except ExceptionGroup as eg:
        first_exc = eg.exceptions[0] if eg.exceptions else eg
        logger.error(f"文案生成出错：{first_exc}")
        return {"status": "error", "message": f"文案生成出错：{first_exc}"}
    except BaseException as e:
        logger.error(f"文案生成出错：{str(e)}")
        return {"status": "error", "message": f"文案生成出错：{str(e)}"}


# ==================== Agent Card ====================
agent_card = AgentCard(
    name="CopywritingAssistant",
    description="小红书电商种草（开箱测评）文案撰写助手，会通过商品 MCP 获取真实价格与规格、调用导购和订单代理做比价推荐，并通过图片 MCP 检索商品实拍图，生成个人体验风格的商品种草文案",
    url="http://127.0.0.1:5009",
    version="1.0.0",
    capabilities={"streaming": True, "memory": True},
    skills=[
        AgentSkill(
            name="write xiaohongshu product note",
            description="根据商品/品类、预算和使用场景撰写小红书开箱种草笔记，包含标题、正文、商品信息、商品实拍图、配图建议和标签",
            examples=[
                "帮我写一篇声海 X1 真无线蓝牙耳机的开箱种草笔记",
                "写一篇300元以内降噪耳机的种草文，学生党用",
            ],
        ),
        AgentSkill(
            name="write ecommerce marketing copy",
            description="生成个人体验风格的带货文案，可引用真实商品价格、评分、规格和优惠券信息",
            examples=[
                "给这款充电宝写一篇小红书种草文案，主打通勤便携",
                "写一篇适合送礼的数码好物推荐文案",
            ],
        ),
        AgentSkill(
            name="collect product facts for copy",
            description="在写文案前通过商品 MCP 获取真实价格/规格/库存，调用导购代理找替代款、订单代理查优惠券，写完后通过图片 MCP 检索商品实拍图",
            examples=[
                "这篇文案需要真实的耳机价格和可用优惠券",
                "查一下同价位有哪些替代款，写进对比里",
            ],
        ),
    ],
)


# ==================== 文案 A2A 服务器 ====================
class CopywritingServer(A2AServer):
    """
    文案撰写 A2A 服务器。

    与普通子代理一样，接收主助手任务后调用 query_copywriting；
    不同之处是 query_copywriting 内部的工具会继续调用商品 MCP 与其他 A2A 代理。
    """

    def __init__(self):
        super().__init__(agent_card=agent_card)

    def handle_task(self, task):
        content = (task.message or {}).get("content", {})
        conversation = content.get("text", "") if isinstance(content, dict) else ""
        logger.info(f"文案需求: {conversation}")

        try:
            result = asyncio.run(query_copywriting(conversation))
            logger.info(f"文案生成返回: {str(result)[:300]}...")

            if result.get("status") == "success":
                result_text = result.get("message", "")

                # 关键信息不足时，文案 Agent 会以固定前缀返回追问
                if result_text.startswith("需要补充信息"):
                    task.status = TaskStatus(
                        state=TaskState.INPUT_REQUIRED,
                        message={"role": "agent", "content": {"text": result_text}},
                    )
                else:
                    task.artifacts = [{"parts": [{"type": "text", "text": result_text}]}]
                    task.status = TaskStatus(state=TaskState.COMPLETED)

            elif result.get("status") == "error":
                task.status = TaskStatus(
                    state=TaskState.FAILED,
                    message={
                        "role": "agent",
                        "content": {"text": result.get("message", "文案生成失败，请重试。")},
                    },
                )
            else:
                task.status = TaskStatus(
                    state=TaskState.FAILED,
                    message={"role": "agent", "content": {"text": "文案生成失败，请重试。"}},
                )

            return task

        except Exception as e:
            logger.error(f"文案生成失败: {str(e)}")
            task.status = TaskStatus(
                state=TaskState.FAILED,
                message={
                    "role": "agent",
                    "content": {"text": f"文案生成失败: {str(e)} 请重试或补充更多信息。"},
                },
            )
            return task


# ==================== 主函数 ====================
if __name__ == "__main__":
    copywriting_server = CopywritingServer()

    print("\n=== 服务器信息 ===")
    print(f"名称: {copywriting_server.agent_card.name}")
    print(f"描述: {copywriting_server.agent_card.description}")
    print("\n技能:")
    for skill in copywriting_server.agent_card.skills:
        print(f"- {skill.name}: {skill.description}")

    # 文案代理需要调用其他代理，打印下游地址方便排查
    print("\n下游 A2A 代理:")
    for name, url in AGENT_URLS.items():
        print(f"- {name}: {url}")
    print(f"\n商品 MCP: {PRODUCT_MCP_URL}")
    print(f"图片 MCP: {IMAGE_MCP_URL}")

    run_server(copywriting_server, host="0.0.0.0", port=5009)
