"""
需求：实现基于 MCP 的商品语义推荐服务器（RAG 模式）
端口：8002
提供工具：
    - semantic_search_products  将用户自然语言需求转成向量，在 Milvus 中做余弦相似度检索，
                                返回最匹配的商品（支持按品类过滤）

数据源：Milvus 向量库的 products 集合（由 sql/init_product_rag.py 初始化）
架构：Qwen Embedding + Milvus COSINE 搜索（商品语义检索）
"""

import json

import requests
from pymilvus import connections, Collection
from fastmcp import FastMCP

# 确保项目根目录在 sys.path 中（支持直接以脚本方式运行 python mcp_server/xxx.py）
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from create_logger import logger

conf = Config()

# ==================== Milvus + Embedding 配置 ====================
MILVUS_HOST = conf.milvus_host
MILVUS_PORT = conf.milvus_port
DB_NAME = "default"
COLLECTION_NAME = "products"
EMBEDDING_URL = "https://ws-n9otlvquyp7rga26.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/embeddings"
EMBEDDING_DIM = 1024


def get_embedding(text: str) -> list:
    """调用 Qwen Embedding API 生成 1024 维向量"""
    headers = {
        "Authorization": f"Bearer {conf.api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "text-embedding-v3",
        "input": [text],
        "dimensions": EMBEDDING_DIM
    }
    response = requests.post(EMBEDDING_URL, headers=headers, json=payload, timeout=30)
    result = response.json()
    return result["data"][0]["embedding"]


def search_products_in_milvus(query_text: str, category: str = None, limit: int = 5) -> list:
    """
    在 Milvus 中进行商品语义搜索

    参数：
        query_text (str): 用户需求描述，如"降噪好出差用的耳机"
        category (str, optional): 品类过滤，如"数码配件"
        limit (int): 返回数量，默认 5

    返回值：
        list: 匹配商品列表（含相似度）
    """
    connections.connect(alias="default", host=MILVUS_HOST, port=MILVUS_PORT)
    collection = Collection(COLLECTION_NAME)
    collection.load()

    expr = f'category == "{category}"' if category else None

    results = collection.search(
        data=[get_embedding(query_text)],
        anns_field="embedding",
        param={"metric_type": "COSINE", "params": {"nprobe": 10}},
        limit=limit,
        output_fields=["product_id", "name", "category", "brand", "price",
                       "original_price", "stock", "rating", "sales_volume",
                       "description", "specs"],
        expr=expr
    )

    product_list = []
    for hits in results:
        for hit in hits:
            product_list.append({
                "product_id": hit.entity.get("product_id"),
                "name": hit.entity.get("name"),
                "category": hit.entity.get("category"),
                "brand": hit.entity.get("brand"),
                "price": hit.entity.get("price"),
                "original_price": hit.entity.get("original_price"),
                "stock": hit.entity.get("stock"),
                "rating": round(hit.entity.get("rating"), 1),
                "sales_volume": hit.entity.get("sales_volume"),
                "description": hit.entity.get("description"),
                "specs": hit.entity.get("specs"),
                "similarity": round(hit.distance, 4),
            })
    return product_list


# ==================== 创建 MCP 服务器 ====================
def create_recommendation_mcp_server():
    recommend_mcp = FastMCP(
        name="RecommendationTools",
        instructions="商品语义推荐工具，基于 Milvus 向量检索。将用户自然语言需求匹配最相关的商品，支持品类过滤。",
    )

    @recommend_mcp.tool(
        name="semantic_search_products",
        description="商品语义搜索（推荐/导购核心工具）。参数：query_text(需求描述,如'送女朋友的显白口红'), "
                    "category(品类过滤,可选: 数码配件/智能家居/美妆个护/食品饮料/服饰鞋包/运动户外), limit(返回数量,默认5)"
    )
    def semantic_search_products(query_text: str, category: str = None, limit: int = 5) -> str:
        logger.info(f"语义搜索商品: query='{query_text}', category='{category}', limit={limit}")
        try:
            product_list = search_products_in_milvus(query_text, category, limit)
            if product_list:
                return json.dumps({"status": "success", "data": product_list}, ensure_ascii=False)
            return json.dumps({
                "status": "no_data",
                "message": f"未找到匹配的商品。{'请确认品类名称，' if category else ''}或换个说法描述需求。"
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"商品语义搜索错误: {str(e)}")
            return json.dumps({"status": "error", "message": f"商品语义搜索出错：{str(e)}"}, ensure_ascii=False)

    logger.info("=== 商品推荐 MCP 服务器信息 ===")
    logger.info(f"名称: {recommend_mcp.name}")
    logger.info(f"描述: {recommend_mcp.instructions}")

    try:
        print("服务器已启动，请访问 http://127.0.0.1:8002/mcp")
        recommend_mcp.run(transport="http", host="0.0.0.0", port=8002)
    except Exception as e:
        print(f"服务器启动失败: {e}")


if __name__ == '__main__':
    create_recommendation_mcp_server()
