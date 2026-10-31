"""
需求：实现基于 A2A 的电商导购（推荐）服务器（RecommendationAssistant）
端口：5005（复用原天气 Agent 端口）
职责：理解用户自然语言购物需求，通过语义检索（Milvus RAG）推荐最匹配的商品。

架构：
    采用 LangChain Agent + MCP Tools 模式：
    1. 主助手通过 A2A 把用户需求发给本服务器
    2. 本服务器用 LLM Agent 自主选择调用商品语义检索工具
    3. 工具返回候选商品后，LLM 组织成友好的导购推荐文案

MCP Server（端口 8002）提供的工具：
    - semantic_search_products：将需求描述转成向量，在 Milvus 中检索最相关商品
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
# 商品语义推荐 MCP 运行在 8002 端口
MCP_URL = os.getenv("RECOMMEND_MCP_URL", "http://127.0.0.1:8002/mcp")
MCP_TIMEOUT = int(os.getenv("RECOMMEND_MCP_TIMEOUT", "45"))
AGENT_TIMEOUT = int(os.getenv("RECOMMEND_AGENT_TIMEOUT", "100"))


async def recommend_products(conversation: str) -> dict:
    """
    通过 LangChain Agent + 语义检索 MCP 执行商品推荐

    参数：
        conversation (str): 用户的购物需求，如"想买一副出差用的降噪耳机"

    返回值：
        dict: {"status": "success/error", "message": "..."}
    """
    try:
        async with Client(MCP_URL, timeout=MCP_TIMEOUT) as mcp_client:
            tools = await load_mcp_tools(mcp_client.session)

            prompt = ChatPromptTemplate.from_messages([
                ("system", """你是一个电商导购推荐助手，能够调用工具根据用户的需求语义检索并推荐商品。
你需要仔细分析用户的需求（品类偏好、预算、使用场景、人群等），然后从工具返回的商品中筛选 3-5 个最匹配的，组织成清晰的导购推荐。
如果用户提供的信息不足以推荐（例如完全没说想买什么），则向用户追问，以获取需求。不能自己编撰商品。
每一轮只调用一个工具，等工具结果返回后再决定下一步，不要并行调用多个工具。
推荐输出格式（中文）：
- 先一句话总结用户的需求画像（如"为经常出差的用户推荐降噪耳机"）
- 逐个商品列出：商品名（品牌） ¥价格 | 评分 | 推荐理由（结合商品描述/亮点与用户需求的匹配点） | 相似度
- 结尾给出一句选购建议（如性价比之选 / 预算充足选旗舰）
如果工具返回 no_data，请回复"暂时没有匹配的商品，您可以换个描述或放宽条件。"
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
        logger.error("商品推荐超时")
        return {"status": "error", "message": "商品推荐超时，请稍后重试。"}
    except ExceptionGroup as eg:
        first_exc = eg.exceptions[0] if eg.exceptions else eg
        logger.error(f"商品推荐 MCP 出错：{first_exc}")
        return {"status": "error", "message": f"商品推荐出错：{first_exc}"}
    except BaseException as e:
        logger.error(f"商品推荐 MCP 出错：{str(e)}")
        return {"status": "error", "message": f"商品推荐出错：{str(e)}"}


# ==================== Agent Card ====================
agent_card = AgentCard(
    name="RecommendationAssistant",
    description="基于 Milvus 语义检索的电商导购推荐助手，理解自然语言需求并推荐商品",
    url="http://127.0.0.1:5005",
    version="2.0.0",
    capabilities={"streaming": True, "memory": True},
    skills=[
        AgentSkill(
            name="recommend products",
            description="根据用户自然语言需求（品类/预算/场景/人群）语义检索并推荐商品",
            examples=["推荐一副出差用的降噪耳机", "送女朋友生日礼物有什么好推荐", "预算200以内的充电宝"]
        ),
    ]
)


# ==================== 服务器类 ====================
class RecommendationServer(A2AServer):
    def __init__(self):
        super().__init__(agent_card=agent_card)

    def handle_task(self, task):
        content = (task.message or {}).get("content", {})
        conversation = content.get("text", "") if isinstance(content, dict) else ""
        logger.info(f"导购需求: {conversation}")

        try:
            result = asyncio.run(recommend_products(conversation))
            logger.info(f"导购返回: {result}")

            if result.get("status") == "success":
                task.artifacts = [{"parts": [{"type": "text", "text": result.get("message", "")}]}]
                task.status = TaskStatus(state=TaskState.COMPLETED)
            elif result.get("status") == "error":
                task.status = TaskStatus(
                    state=TaskState.FAILED,
                    message={"role": "agent", "content": {"text": result.get("message", "推荐失败，请重试。")}}
                )
            else:
                task.status = TaskStatus(
                    state=TaskState.FAILED,
                    message={"role": "agent", "content": {"text": "推荐失败，请重试或提供更多细节。"}}
                )
            return task
        except Exception as e:
            logger.error(f"导购失败: {str(e)}")
            task.status = TaskStatus(
                state=TaskState.FAILED,
                message={"role": "agent", "content": {"text": f"推荐失败: {str(e)} 请重试或提供更多细节。"}}
            )
            return task


if __name__ == "__main__":
    server = RecommendationServer()
    print("\n=== 服务器信息 ===")
    print(f"名称: {server.agent_card.name}")
    print(f"描述: {server.agent_card.description}")
    for skill in server.agent_card.skills:
        print(f"- {skill.name}: {skill.description}")
    run_server(server, host="0.0.0.0", port=5005)
