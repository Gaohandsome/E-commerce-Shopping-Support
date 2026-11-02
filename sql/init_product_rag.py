"""
需求：初始化电商商品 RAG 系统
思路：
1. 从 MySQL 的 products 表读取全部商品（作为结构化数据的事实来源）
2. 使用 Qwen 的 Embedding API 将每个商品的"名称+品类+品牌+描述+标签"生成向量
3. 在本地 Milvus 中创建 products Collection 并插入数据
4. 支持后续通过语义搜索匹配用户自然语言描述的商品（导购 Agent 使用）

说明：
- 与 init_tour_group_rag.py 保持一致的接口与工程化写法（含 Embedding 重试与错误处理）
- Milvus 连接参数读取自 conf（config.py），库名沿用 GY_demo，集合名 products
"""

import json
import os
import sys
import time
import requests
import mysql.connector
from pymilvus import (connections, Collection, CollectionSchema,
                      FieldSchema, DataType, utility, MilvusClient)
from dotenv import load_dotenv

# 将项目根目录加入 Python 路径，以便导入配置
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 在读取任何环境变量 / 配置之前，先加载项目根目录的 .env
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

# 确保项目根目录在 sys.path 中（支持直接以脚本方式运行 python sql/xxx.py）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
conf = Config()
DASHSCOPE_API_KEY = conf.api_key
# 与 init_tour_group_rag.py 保持一致的 maas 网关地址（含 /embeddings 后缀）
EMBEDDING_URL = "https://ws-n9otlvquyp7rga26.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/embeddings"


def get_embedding(text: str, max_retry: int = 3) -> list:
    """
    调用 Qwen Embedding API 生成文本向量（1024 维）

    参数：
        text (str): 需要生成向量的文本
        max_retry (int): 失败重试次数

    返回值：
        list: 1024 维向量
    """
    if not DASHSCOPE_API_KEY:
        raise RuntimeError(
            "未配置 API_KEY 环境变量（DashScope 密钥为空）。\n"
            "请在运行前设置：set API_KEY=你的_DashScope_API_Key  (Windows)\n"
            "或 export API_KEY=你的_DashScope_API_Key  (Linux/Mac)"
        )

    headers = {
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "text-embedding-v3",
        "input": [text],
        "dimensions": 1024
    }

    last_err = None
    for attempt in range(1, max_retry + 1):
        try:
            response = requests.post(EMBEDDING_URL, headers=headers, json=payload, timeout=30)

            if response.status_code != 200:
                last_err = f"Embedding 接口 HTTP 错误 {response.status_code}: {response.text[:300]}"
                if 400 <= response.status_code < 500:
                    raise RuntimeError(last_err)
                time.sleep(2 * attempt)
                continue

            result = response.json()
            if "data" not in result:
                last_err = f"Embedding 接口返回异常（无 data 字段）：{json.dumps(result, ensure_ascii=False)[:500]}"
                time.sleep(2 * attempt)
                continue

            data_item = result["data"][0]
            if "embedding" not in data_item:
                last_err = f"Embedding 接口返回结构与预期不符：{json.dumps(result, ensure_ascii=False)[:500]}"
                time.sleep(2 * attempt)
                continue

            return data_item["embedding"]

        except (requests.RequestException, ValueError) as e:
            last_err = f"调用 Embedding 接口异常（第 {attempt}/{max_retry} 次）：{e}"
            time.sleep(2 * attempt)
            continue

    raise RuntimeError(f"获取向量失败，已重试 {max_retry} 次：{last_err}")


# ==================== Milvus 配置 ====================
MILVUS_HOST = conf.milvus_host
MILVUS_PORT = conf.milvus_port
# 电商业务使用独立数据库，避免与旅游项目的集合混在一起
DB_NAME = os.getenv("MILVUS_DB_NAME", "ecommerce")
COLLECTION_NAME = os.getenv("MILVUS_COLLECTION", "products")
DIMENSION = 1024  # text-embedding-v3 输出维度


def fetch_products_from_mysql():
    """
    从 MySQL products 表读取全部商品

    返回值：
        list[dict]: 商品列表
    """
    conn = mysql.connector.connect(
        host=conf.host,
        user=conf.user,
        password=conf.password,
        database=conf.database
    )
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, name, category, brand, price, original_price, stock, rating, "
            "sales_volume, description, specs, tags FROM products"
        )
        rows = cursor.fetchall()
        return rows
    finally:
        cursor.close()
        conn.close()


def create_product_collection():
    """
    在 Milvus 中创建商品 Collection

    字段设计（与 MySQL products 表对齐，额外增加 embedding 向量字段）：
    - id / product_id: 商品ID
    - name / category / brand / price / rating / sales_volume / description
    - specs / tags: JSON 字符串
    - embedding: 1024 维向量
    """
    # 先连默认库，确认电商业务库是否存在，不存在则创建（Milvus 2.2.9+ 支持多库）
    # 注意：pymilvus 2.6 的数据库 API 在 MilvusClient 上，utility 里没有
    client = MilvusClient(uri=f"http://{MILVUS_HOST}:{MILVUS_PORT}", timeout=10)
    if DB_NAME not in client.list_databases():
        client.create_database(DB_NAME)
        print(f"已创建 Milvus 数据库: {DB_NAME}")
    else:
        print(f"Milvus 数据库 {DB_NAME} 已存在")
    client.close()

    connections.connect(alias="default", host=MILVUS_HOST, port=MILVUS_PORT,
                        db_name=DB_NAME, timeout=10)
    print(f"已连接 Milvus: {MILVUS_HOST}:{MILVUS_PORT} (db={DB_NAME})")

    # 如果已存在则删除重建（演示数据可重复初始化）
    if utility.has_collection(COLLECTION_NAME):
        print(f"集合 {COLLECTION_NAME} 已存在，删除重建")
        utility.drop_collection(COLLECTION_NAME)

    fields = [
        FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
        FieldSchema(name="product_id", dtype=DataType.INT64),
        FieldSchema(name="name", dtype=DataType.VARCHAR, max_length=200),
        FieldSchema(name="category", dtype=DataType.VARCHAR, max_length=50),
        FieldSchema(name="brand", dtype=DataType.VARCHAR, max_length=50),
        FieldSchema(name="price", dtype=DataType.FLOAT),
        FieldSchema(name="original_price", dtype=DataType.FLOAT),
        FieldSchema(name="stock", dtype=DataType.INT64),
        FieldSchema(name="rating", dtype=DataType.FLOAT),
        FieldSchema(name="sales_volume", dtype=DataType.INT64),
        FieldSchema(name="description", dtype=DataType.VARCHAR, max_length=1000),
        FieldSchema(name="specs", dtype=DataType.VARCHAR, max_length=1000),
        FieldSchema(name="tags", dtype=DataType.VARCHAR, max_length=500),
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=DIMENSION),
    ]

    schema = CollectionSchema(fields, description="电商商品信息表（RAG 向量检索）")
    collection = Collection(COLLECTION_NAME, schema)
    print(f"集合 {COLLECTION_NAME} 创建成功")

    index_params = {
        "index_type": "IVF_FLAT",
        "metric_type": "COSINE",
        "params": {"nlist": 64}
    }
    collection.create_index(field_name="embedding", index_params=index_params)
    print("向量索引创建成功 (IVF_FLAT, COSINE)")

    return collection


def insert_products(collection, products):
    """
    将商品数据生成向量后插入 Milvus
    """
    print(f"\n开始为 {len(products)} 个商品生成向量...")
    rows = []
    for i, p in enumerate(products):
        # 拼接用于向量化的文本：名称 + 品类 + 品牌 + 描述 + 标签
        tags = p.get("tags") or ""
        if isinstance(tags, str):
            try:
                tags = " ".join(json.loads(tags))
            except (json.JSONDecodeError, TypeError):
                tags = tags
        embedding_text = " ".join([
            str(p.get("name", "")),
            str(p.get("category", "")),
            str(p.get("brand", "")),
            str(p.get("description", "")),
            str(tags)
        ])

        if (i + 1) % 5 == 0:
            print(f"  已处理 {i + 1}/{len(products)} 个商品...")

        embedding = get_embedding(embedding_text)

        rows.append({
            "product_id": p["id"],
            "name": p.get("name", ""),
            "category": p.get("category", ""),
            "brand": p.get("brand", ""),
            "price": float(p.get("price") or 0),
            "original_price": float(p.get("original_price") or 0),
            "stock": int(p.get("stock") or 0),
            "rating": float(p.get("rating") or 0),
            "sales_volume": int(p.get("sales_volume") or 0),
            "description": p.get("description") or "",
            "specs": p.get("specs") or "",
            "tags": tags if isinstance(tags, str) else "",
            "embedding": embedding,
        })

    collection.insert(rows)
    collection.flush()
    print(f"成功插入 {len(products)} 个商品数据")


def test_search(collection):
    """
    测试语义搜索：用自然语言查询商品
    """
    test_queries = [
        "想买一副降噪效果好、出差能用的耳机",
        "出门旅游用的轻便大容量充电宝",
        "适合送女朋友的显白口红",
        "家里打扫卫生用的自动扫地机",
        "周末露营要用的帐篷",
        "平价好用的补水面膜",
    ]

    print("\n=== 语义搜索测试 ===")
    for query in test_queries:
        print(f"\n查询: {query}")
        query_embedding = get_embedding(query)

        collection.load()
        results = collection.search(
            data=[query_embedding],
            anns_field="embedding",
            param={"metric_type": "COSINE", "params": {"nprobe": 10}},
            limit=3,
            output_fields=["product_id", "name", "category", "brand", "price", "rating"]
        )

        for hits in results:
            for hit in hits:
                print(f"  [{hit.entity.get('product_id')}] {hit.entity.get('name')} "
                      f"- {hit.entity.get('brand')} ¥{hit.entity.get('price')} "
                      f"评分{hit.entity.get('rating')} 距离: {hit.distance:.4f}")


if __name__ == '__main__':
    if not DASHSCOPE_API_KEY:
        print("=" * 60)
        print("错误：未检测到 API_KEY 环境变量（DashScope 密钥为空）。")
        print("请先设置后再运行本脚本：")
        print("  Windows:  set API_KEY=你的_DashScope_API_Key")
        print("  Linux/Mac: export API_KEY=你的_DashScope_API_Key")
        print("=" * 60)
        sys.exit(1)

    print("正在从 MySQL 读取商品数据...")
    products = fetch_products_from_mysql()
    if not products:
        print("未读取到任何商品，请先执行 sql/demo_product_data.sql 导入演示数据。")
        sys.exit(1)
    print(f"读取到 {len(products)} 个商品。")

    collection = create_product_collection()
    insert_products(collection, products)
    test_search(collection)
