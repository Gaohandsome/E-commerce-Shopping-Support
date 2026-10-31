"""
需求：实现基于 A2A 的物流服务器（LogisticsAssistant）
端口：5007
职责：查询订单的发货 / 物流状态，返回最新状态与轨迹节点。

架构：与 order_server.py 一致 —— LangChain Agent + MCP Tools 模式：
    1. 主助手通过 A2A 把用户的物流查询发给本服务器
    2. 本服务器用 LLM Agent 自主选择调用物流 MCP 工具
    3. 工具返回结果后，LLM 组织成友好回复

MCP Server（端口 8003）提供的工具：
    - query_logistics    : 按订单号查物流轨迹（最新状态 + 全部节点）
    - query_order        : 按订单号查订单详情
    - list_user_orders   : 列出用户最近订单（帮助定位订单号）
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
MCP_URL = os.getenv("LOGISTICS_MCP_URL", "http://127.0.0.1:8003/mcp")
MCP_TIMEOUT = int(os.getenv("LOGISTICS_MCP_TIMEOUT", "45"))
AGENT_TIMEOUT = int(os.getenv("LOGISTICS_AGENT_TIMEOUT", "100"))


async def handle_logistics(conversation: str) -> dict:
    """
    通过 LangChain Agent + 物流 MCP 查询发货 / 物流状态

    参数：
        conversation (str): 用户的物流查询，如"我昨天买的衣服发货没"

    返回值：
        dict: {"status": "success/error", "message": "..."}
    """
    try:
        async with Client(MCP_URL, timeout=MCP_TIMEOUT) as mcp_client:
            tools = await load_mcp_tools(mcp_client.session)

            prompt = ChatPromptTemplate.from_messages([
                ("system", """你是一个电商物流助手，负责查询订单的发货与物流状态。
工作流程：
1. 用户给出订单号时，直接调用 query_logistics 查询该订单的物流轨迹。
2. 用户没有给出订单号（如"我昨天买的东西发货没"）时，先调用 list_user_orders 列出最近订单，选出最可能的订单号，再调用 query_logistics。
3. 需要订单明细（商品、金额、下单时间）时调用 query_order。
输出要点（中文、简洁）：
- 先给结论：当前物流状态（待发货/已发货/运输中/派送中/已签收）+ 承运商 + 运单号。
- 再列关键轨迹节点（时间 + 描述），最多列最近 3~4 个。
- 若"待发货"，明确告知尚未发货。
【铁律·严禁编造】
- 只能使用工具返回的真实订单号、状态、运单号、节点时间与描述；不得虚构物流信息、时效或签收情况。
- 查不到订单/物流时，如实说明"未查到该订单的物流信息"，并请用户确认订单号，绝不编造轨迹。
- 若用户未提供订单号且无法从最近订单判断，主动追问订单号。
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
        logger.error("物流处理超时")
        return {"status": "error", "message": "物流查询超时，请稍后重试。"}
    except ExceptionGroup as eg:
        first_exc = eg.exceptions[0] if eg.exceptions else eg
        logger.error(f"物流 MCP 出错：{first_exc}")
        return {"status": "error", "message": f"物流查询出错：{first_exc}"}
    except BaseException as e:
        logger.error(f"物流 MCP 出错：{str(e)}")
        return {"status": "error", "message": f"物流查询出错：{str(e)}"}


# ==================== Agent Card ====================
agent_card = AgentCard(
    name="LogisticsAssistant",
    description="电商物流助手，提供订单发货状态与物流轨迹查询",
    url="http://127.0.0.1:5007",
    version="2.0.0",
    capabilities={"streaming": True, "memory": True},
    skills=[
        AgentSkill(
            name="track logistics",
            description="按订单号查询订单发货状态与物流轨迹",
            examples=["我的订单发货了吗", "帮我查下 EC20261030002 的物流", "这个包裹到哪了"]
        ),
        AgentSkill(
            name="query order",
            description="按订单号查询订单详情（商品、金额、下单时间）",
            examples=["查下 EC20261030001 这个订单", "我买的什么"]
        ),
        AgentSkill(
            name="list recent orders",
            description="列出最近订单，帮助用户定位订单号",
            examples=["我最近买了什么", "我昨天买的衣服是哪个订单"]
        ),
    ]
)


# ==================== 服务器类 ====================
class LogisticsServer(A2AServer):
    def __init__(self):
        super().__init__(agent_card=agent_card)

    def handle_task(self, task):
        content = (task.message or {}).get("content", {})
        conversation = content.get("text", "") if isinstance(content, dict) else ""
        logger.info(f"物流需求: {conversation}")

        try:
            result = asyncio.run(handle_logistics(conversation))
            logger.info(f"物流返回: {result}")

            if result.get("status") == "success":
                task.artifacts = [{"parts": [{"type": "text", "text": result.get("message", "")}]}]
                task.status = TaskStatus(state=TaskState.COMPLETED)
            else:
                task.status = TaskStatus(
                    state=TaskState.FAILED,
                    message={"role": "agent",
                             "content": {"text": result.get("message", "物流查询失败，请重试。")}}
                )
            return task
        except Exception as e:
            logger.error(f"物流处理失败: {str(e)}")
            task.status = TaskStatus(
                state=TaskState.FAILED,
                message={"role": "agent",
                         "content": {"text": f"物流查询失败: {str(e)} 请重试或提供订单号。"}}
            )
            return task


if __name__ == "__main__":
    server = LogisticsServer()
    print("\n=== 服务器信息 ===")
    print(f"名称: {server.agent_card.name}")
    print(f"描述: {server.agent_card.description}")
    for skill in server.agent_card.skills:
        print(f"- {skill.name}: {skill.description}")
    run_server(server, host="0.0.0.0", port=5007)
