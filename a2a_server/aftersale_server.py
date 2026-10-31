"""
需求：实现基于 A2A 的售后服务端（AfterSaleAssistant）
端口：5008
职责：退换货规则判断、售后时效校验、售后工单创建与查询。

架构：与 order_server.py 一致 —— LangChain Agent + MCP Tools 模式：
    1. 主助手通过 A2A 把用户的售后诉求发给本服务器
    2. 本服务器用 LLM Agent 调用售后 MCP 的规则引擎与工单工具
    3. 工具返回结果后，LLM 组织成友好回复

MCP Server（端口 8005）提供的工具：
    - get_order                : 订单详情（含商品品类）
    - get_after_sale_rule      : 按品类查退换货规则
    - judge_after_sale         : 规则引擎，判断是否可售后
    - create_after_sale_ticket : 创建售后工单
    - query_after_sale_ticket  : 查询工单状态
"""

import os
import asyncio

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
MCP_URL = os.getenv("AFTERSALE_MCP_URL", "http://127.0.0.1:8005/mcp")
MCP_TIMEOUT = int(os.getenv("AFTERSALE_MCP_TIMEOUT", "45"))
AGENT_TIMEOUT = int(os.getenv("AFTERSALE_AGENT_TIMEOUT", "100"))


async def handle_after_sale(conversation: str) -> dict:
    """
    通过 LangChain Agent + 售后 MCP 完成规则判断与工单处理

    参数：
        conversation (str): 用户售后诉求，如"这件衣服不合适，帮我退掉"

    返回值：
        dict: {"status": "success/error", "message": "..."}
    """
    try:
        async with Client(MCP_URL, timeout=MCP_TIMEOUT) as mcp_client:
            tools = await load_mcp_tools(mcp_client.session)

            prompt = ChatPromptTemplate.from_messages([
                ("system", """你是一个电商售后助手，负责退换货规则判断与售后工单处理。
工作流程：
1. 明确售后类型：return(退货退款) / exchange(换货) / refund(仅退款)，默认 return。
2. 确定订单号：用户给出则直接使用；没有则主动询问，绝不臆造订单号。
3. 调用 judge_after_sale(order_no, request_type) 获取规则判定（是否可售后、时效、截止日期、条件）。
4. 条件式诉求（如"没发货就退掉""不合适就换"）：先调用 get_order 查看该订单的 shipping_status_cn；
   若条件成立（如 shipping_status_cn 为"待发货"或"未发货"），即视为用户已明确要求办理，直接调用 create_after_sale_ticket 建单，不要再向用户二次确认。
5. 严格按工具结果处理，不得自行添加平台政策：
   - eligible=true 且用户明确要办理（如"帮我退掉""申请退货""换一个"）→ **必须调用 create_after_sale_ticket 创建工单**，并返回真实工单号；
   - eligible=true 但用户只是咨询（如"能退吗"）→ 说明时效与条件，并询问是否需要办理；
   - eligible=false → 按工具返回的原因说明，不要创建工单。
6. 用户查询工单进度时，调用 query_after_sale_ticket。
输出要点（中文、简洁 150-250 字）：先给结论（可以/不可以退换 + 原因），再给期限与前置条件；办理成功时给出真实工单号。
【铁律·严禁编造】
- 只能使用工具返回的真实订单号、规则、时效、截止日期与工单号。
- 严禁臆造工具未返回的平台政策（如"未发货不能退货""必须先取消订单""系统会拒绝建工单""全额秒退"等）。
- 订单是否已发货以工具/上下文真实信息为准；只要 judge_after_sale 判定 eligible=true，就按判定办理，绝不自行以"未发货/已支付"等理由拒绝或改口让用户去取消订单。
- **办理工单必须真实调用工具**：在调用 create_after_sale_ticket 并拿到返回之前，绝对禁止在回复中出现"已创建工单/已提交申请/工单号"等任何表述。
- **真实工单号一律以 `AS` 开头**（如 AS20261031...）。如果你准备写的工单号不是 AS 开头，说明你在编造，必须立即改为调用 create_after_sale_ticket。
- 查不到订单时如实说明"未查到该订单"，并请用户确认订单号。
【回复前自检】若你的回复里出现"已创建/已提交/工单号"，则本轮必须确实调用过 create_after_sale_ticket，且所写工单号来自该工具返回值（AS 开头）；否则请勿声称已办理，应改为调用工具或向用户询问。
当前日期是{current_date}。"""),
                ("human", "{input}"),
                ("placeholder", "{agent_scratchpad}"),
            ])

            agent = create_tool_calling_agent(llm, tools, prompt)
            agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=True)

            current_date = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')

            response = await asyncio.wait_for(
                agent_executor.ainvoke({"input": conversation, "current_date": current_date}),
                timeout=AGENT_TIMEOUT,
            )
            return {"status": "success", "message": response["output"]}

    except asyncio.TimeoutError:
        logger.error("售后处理超时")
        return {"status": "error", "message": "售后处理超时，请稍后重试。"}
    except ExceptionGroup as eg:
        first_exc = eg.exceptions[0] if eg.exceptions else eg
        logger.error(f"售后 MCP 出错：{first_exc}")
        return {"status": "error", "message": f"售后处理出错：{first_exc}"}
    except BaseException as e:
        logger.error(f"售后 MCP 出错：{str(e)}")
        return {"status": "error", "message": f"售后处理出错：{str(e)}"}


# ==================== Agent Card ====================
agent_card = AgentCard(
    name="AfterSaleAssistant",
    description="电商售后助手，提供退换货规则判断、时效校验与售后工单处理",
    url="http://127.0.0.1:5008",
    version="2.0.0",
    capabilities={"streaming": True, "memory": True},
    skills=[
        AgentSkill(
            name="judge after-sale eligibility",
            description="判断某订单能否退货/换货/退款，说明时效与条件",
            examples=["这件衣服能退吗", "EC20261030003 还能换货吗"]
        ),
        AgentSkill(
            name="create after-sale ticket",
            description="在符合规则时创建退货/换货/退款售后工单",
            examples=["帮我退掉 EC20261030001", "申请换货"]
        ),
        AgentSkill(
            name="query after-sale ticket",
            description="查询售后工单处理进度",
            examples=["我的退货工单到哪一步了", "查下工单 AS2026..."]
        ),
    ]
)


# ==================== 服务器类 ====================
class AfterSaleServer(A2AServer):
    def __init__(self):
        super().__init__(agent_card=agent_card)

    def handle_task(self, task):
        content = (task.message or {}).get("content", {})
        conversation = content.get("text", "") if isinstance(content, dict) else ""
        logger.info(f"售后需求: {conversation}")

        try:
            result = asyncio.run(handle_after_sale(conversation))
            logger.info(f"售后返回: {result}")

            if result.get("status") == "success":
                task.artifacts = [{"parts": [{"type": "text", "text": result.get("message", "")}]}]
                task.status = TaskStatus(state=TaskState.COMPLETED)
            else:
                task.status = TaskStatus(
                    state=TaskState.FAILED,
                    message={"role": "agent",
                             "content": {"text": result.get("message", "售后处理失败，请重试。")}}
                )
            return task
        except Exception as e:
            logger.error(f"售后处理失败: {str(e)}")
            task.status = TaskStatus(
                state=TaskState.FAILED,
                message={"role": "agent",
                         "content": {"text": f"售后处理失败: {str(e)} 请重试或提供订单号。"}}
            )
            return task


if __name__ == "__main__":
    server = AfterSaleServer()
    print("\n=== 服务器信息 ===")
    print(f"名称: {server.agent_card.name}")
    print(f"描述: {server.agent_card.description}")
    for skill in server.agent_card.skills:
        print(f"- {skill.name}: {skill.description}")
    run_server(server, host="0.0.0.0", port=5008)
