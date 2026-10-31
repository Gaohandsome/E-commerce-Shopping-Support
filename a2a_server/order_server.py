"""
需求：实现基于 A2A 的电商订单服务器（OrderAssistant）
端口：5006（复用原票务 Agent 端口）
职责：帮用户进行商品比价、查询/试算优惠券，并在确认后下单。

架构：
    与 recommendation_server.py 一致 —— LangChain Agent + MCP Tools 模式：
    1. 主助手通过 A2A 把用户的购买/比价需求发给本服务器
    2. 本服务器用 LLM Agent 自主选择调用商品/订单 MCP 工具
    3. 工具返回结果后，LLM 组织成友好回复（比价表 / 优惠说明 / 下单结果）

MCP Server（端口 8001）提供的工具：
    - search_products / compare_price / get_product : 商品筛选与比价
    - list_coupons / calculate_coupon             : 优惠券查询与试算
    - create_order                                : 下单（写库、核销券、扣库存）
"""

import os
import json
import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from python_a2a import A2AServer, run_server, AgentCard, AgentSkill, TaskStatus, TaskState

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_mcp_adapters.tools import load_mcp_tools
from langchain.agents import create_tool_calling_agent, AgentExecutor
from fastmcp import Client
from datetime import datetime
import pytz

# 确保项目根目录在 sys.path 中（支持直接以脚本方式运行 python a2a_server/xxx.py）
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from create_logger import logger

conf = Config()

# ==================== 初始化大模型 ====================
llm = ChatOpenAI(**conf.llm_config)

# ==================== MCP 客户端配置 ====================
# 商品/订单 MCP 运行在 8001 端口
MCP_URL = os.getenv("PRODUCT_MCP_URL", "http://127.0.0.1:8001/mcp")
MCP_TIMEOUT = int(os.getenv("PRODUCT_MCP_TIMEOUT", "45"))
AGENT_TIMEOUT = int(os.getenv("PRODUCT_AGENT_TIMEOUT", "100"))


async def handle_order(conversation: str) -> dict:
    """
    通过 LangChain Agent + 商品/订单 MCP 执行比价、优惠券试算与下单

    参数：
        conversation (str): 用户的购买/比价需求，如"帮我比一下蓝牙耳机，再下单最便宜那个"

    返回值：
        dict: {"status": "success/error", "message": "..."}
    """
    try:
        async with Client(MCP_URL, timeout=MCP_TIMEOUT) as mcp_client:
            tools = await load_mcp_tools(mcp_client.session)

            prompt = ChatPromptTemplate.from_messages([
                ("system", """你是一个电商订单助手，能够处理商品比价、优惠券查询与试算、以及下单购买。
工作流程建议：
1. 比价/找商品：用 search_products 或 compare_price 按用户需求筛选商品，按价格/性价比给出对比建议。
   - 参数填写规范：
     * keyword：填"商品类型词/名称关键词"，如 蓝牙耳机、充电宝、扫地机器人、手机。
       keyword 可用空格分隔多个词（会按"同时命中"处理），但**不要**把品牌和型号硬拼成一串；
       如用户说"倍思充电宝"，keyword 填"充电宝"、brand 填"倍思"最稳妥。
     * brand：用户明确提到品牌（倍思、小米、华为、罗马仕等）时填入。
     * category：只能填真实品类之一——[数码配件、智能家居、美妆个护、服饰鞋包、食品饮料、运动户外]；拿不准就留空。
       **严禁把"充电宝/耳机/手机"这类商品类型词填进 category**，否则会检索不到商品。
     * max_price / min_price：用户说"200元以内""预算300""不超过500"时，务必分别填入 max_price / min_price。
   - 纯比价、找性价比之选时优先用 compare_price（价格升序）。
2. 优惠券：用 list_coupons 查看可用券，用 calculate_coupon 试算某商品/金额能优惠多少，并主动告知用户可叠加的优惠。
3. 下单（务必“先检索、再下单”）：
   - 第一步：永远先调用 search_products，按用户提到的商品名称或关键词检索，从返回结果里拿到真实存在的 product_id（以及价格、库存）。
   - 严禁凭空猜测 product_id 去调用 get_product 或 create_order；也不要先调 get_product 来“找商品”（get_product 需要确切 id，而你手上并没有）。
   - 第二步：确认商品、数量、优惠券后，才调用 create_order，并传入 search_products 返回的真实 product_id。
   - 若 search_products 未找到匹配商品，明确告知用户“未找到”，并请其确认商品名称或换个关键词；绝不臆造 product_id 继续下单。
   - 如果用户只说"比价""推荐"而没有购买意图，不要调用 create_order，只给比价和建议。
   - 信息不足（不知道买哪个、买几件）时，先向用户追问，不能自己编撰参数。
每一轮只调用一个工具，等结果返回后再决定下一步，不要并行调用多个工具。
输出格式（中文，简洁清晰）：
- 比价：用列表展示候选商品（名称 品牌 ¥价格 评分），并指出性价比之选。
- 优惠：说明可用券及预计优惠金额。
- 下单：确认下单结果（订单号、商品、数量、应付金额、优惠）。
当前日期是{current_date}。"""),
                ("human", "{input}"),
                ("placeholder", "{agent_scratchpad}"),
            ])

            agent = create_tool_calling_agent(llm, tools, prompt)
            agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=True)

            current_date = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')

            response = await asyncio.wait_for(
                agent_executor.ainvoke({
                    "input": conversation,
                    "current_date": current_date
                }),
                timeout=AGENT_TIMEOUT
            )
            return {"status": "success", "message": response["output"]}

    except asyncio.TimeoutError:
        logger.error("订单处理超时")
        return {"status": "error", "message": "订单处理超时，请稍后重试。"}
    except ExceptionGroup as eg:
        first_exc = eg.exceptions[0] if eg.exceptions else eg
        logger.error(f"订单 MCP 出错：{first_exc}")
        return {"status": "error", "message": f"订单处理出错：{first_exc}"}
    except BaseException as e:
        logger.error(f"订单 MCP 出错：{str(e)}")
        return {"status": "error", "message": f"订单处理出错：{str(e)}"}


# ==================== Agent Card ====================
agent_card = AgentCard(
    name="OrderAssistant",
    description="电商订单助手，提供商品比价、优惠券试算与下单购买服务",
    url="http://127.0.0.1:5006",
    version="2.0.0",
    capabilities={"streaming": True, "memory": True},
    skills=[
        AgentSkill(
            name="compare products",
            description="按需求比价商品，返回候选商品对比与性价比建议",
            examples=["帮我比一下蓝牙耳机", "200以内有什么充电宝", "扫地机器人哪个便宜"]
        ),
        AgentSkill(
            name="query coupons",
            description="查询可用优惠券并试算优惠金额",
            examples=["有什么优惠券可以用", "满200减30的券我这笔能省多少"]
        ),
        AgentSkill(
            name="place order",
            description="在用户确认商品、数量、优惠券后下单购买",
            examples=["下单买最便宜的蓝牙耳机2件", "用FULL200券买那个充电宝"]
        ),
    ]
)


# ==================== 服务器类 ====================
class OrderServer(A2AServer):
    def __init__(self):
        super().__init__(agent_card=agent_card)

    def handle_task(self, task):
        content = (task.message or {}).get("content", {})
        conversation = content.get("text", "") if isinstance(content, dict) else ""
        logger.info(f"订单需求: {conversation}")

        try:
            result = asyncio.run(handle_order(conversation))
            logger.info(f"订单返回: {result}")

            if result.get("status") == "success":
                task.artifacts = [{"parts": [{"type": "text", "text": result.get("message", "")}]}]
                task.status = TaskStatus(state=TaskState.COMPLETED)
            elif result.get("status") == "error":
                task.status = TaskStatus(
                    state=TaskState.FAILED,
                    message={"role": "agent", "content": {"text": result.get("message", "订单处理失败，请重试。")}}
                )
            else:
                task.status = TaskStatus(
                    state=TaskState.FAILED,
                    message={"role": "agent", "content": {"text": "订单处理失败，请重试或提供更多细节。"}}
                )
            return task
        except Exception as e:
            logger.error(f"订单处理失败: {str(e)}")
            task.status = TaskStatus(
                state=TaskState.FAILED,
                message={"role": "agent", "content": {"text": f"订单处理失败: {str(e)} 请重试或提供更多细节。"}}
            )
            return task


if __name__ == "__main__":
    server = OrderServer()
    print("\n=== 服务器信息 ===")
    print(f"名称: {server.agent_card.name}")
    print(f"描述: {server.agent_card.description}")
    for skill in server.agent_card.skills:
        print(f"- {skill.name}: {skill.description}")
    run_server(server, host="0.0.0.0", port=5006)
