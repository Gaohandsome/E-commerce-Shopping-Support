"""
需求：实现基于 MCP 的物流服务器（电商物流查询，本地 mock）
端口：8003
提供工具：
    - query_logistics   按订单号查询物流轨迹（含最新状态 + 全量节点）
    - query_order       按订单号查询订单详情
    - list_user_orders  列出某用户的最近订单（帮助"我昨天买的东西"定位订单号）

数据源：MySQL travel_rag 库的 orders / logistics 表（均为本地 mock）
架构：与 mcp_product_server.py 一致（每次查询独立建连）
"""

import json
import os

import mysql.connector
from fastmcp import FastMCP

# 确保项目根目录在 sys.path 中（支持直接以脚本方式运行 python mcp_server/xxx.py）
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from create_logger import logger
from utils.format import DateEncoder, default_encoder

conf = Config()

# 物流状态中文映射
STATUS_CN = {
    "pending": "待发货",
    "shipped": "已发货",
    "in_transit": "运输中",
    "out_for_delivery": "派送中",
    "delivered": "已签收",
    "exception": "物流异常",
}


class LogisticsService:
    """封装订单 / 物流相关的数据库查询"""

    def _connect(self):
        return mysql.connector.connect(
            host=conf.host,
            user=conf.user,
            password=conf.password,
            database=conf.database,
            autocommit=True,  # 只读为主，避免遗留事务持锁
        )

    def _query(self, sql: str, params: list = None):
        conn = None
        cursor = None
        try:
            conn = self._connect()
            cursor = conn.cursor(dictionary=True)
            cursor.execute(sql, params or [])
            rows = cursor.fetchall()
            for row in rows:
                for k, v in row.items():
                    if hasattr(v, "isoformat"):
                        row[k] = default_encoder(v)
            return rows
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    # ---------- 订单 ----------
    def get_order(self, order_no: str) -> dict:
        rows = self._query(
            "SELECT order_no, user_id, product_id, product_name, quantity, unit_price, "
            "total_amount, coupon_code, discount_amount, final_amount, status, created_at "
            "FROM orders WHERE order_no = %s",
            [order_no],
        )
        return rows[0] if rows else None

    def list_user_orders(self, user_id: str = "guest", limit: int = 10) -> str:
        rows = self._query(
            "SELECT order_no, product_name, quantity, final_amount, status, created_at "
            "FROM orders WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
            [user_id, int(limit)],
        )
        return json.dumps(
            {"status": "success", "data": rows} if rows
            else {"status": "no_data", "message": f"用户 {user_id} 暂无订单。"},
            cls=DateEncoder, ensure_ascii=False,
        )

    # ---------- 物流 ----------
    def query_logistics(self, order_no: str) -> str:
        order = self.get_order(order_no)
        if not order:
            return json.dumps(
                {"status": "no_data", "message": f"未找到订单 {order_no}，请确认订单号是否正确。"},
                ensure_ascii=False,
            )

        nodes = self._query(
            "SELECT carrier, tracking_no, status, node_time, node_desc, current_city "
            "FROM logistics WHERE order_no = %s ORDER BY node_time ASC",
            [order_no],
        )
        if not nodes:
            result = {
                "status": "success",
                "data": {
                    "order": order,
                    "tracking": {"latest_status": "pending", "latest_status_cn": "待发货",
                                 "carrier": None, "tracking_no": None,
                                 "latest_desc": "商家尚未发货，仓库处理中", "latest_time": None},
                    "nodes": [],
                },
            }
            return json.dumps(result, cls=DateEncoder, ensure_ascii=False)

        latest = nodes[-1]
        tracking = {
            "carrier": latest["carrier"],
            "tracking_no": latest["tracking_no"],
            "latest_status": latest["status"],
            "latest_status_cn": STATUS_CN.get(latest["status"], latest["status"]),
            "latest_desc": latest["node_desc"],
            "latest_time": latest["node_time"],
        }
        for n in nodes:
            n["status_cn"] = STATUS_CN.get(n["status"], n["status"])

        return json.dumps(
            {"status": "success", "data": {"order": order, "tracking": tracking, "nodes": nodes}},
            cls=DateEncoder, ensure_ascii=False,
        )


# ==================== 创建 MCP 服务器 ====================
def create_logistics_mcp_server():
    logistics_mcp = FastMCP(
        name="LogisticsTools",
        instructions=("电商物流工具，基于 orders / logistics 表。"
                      "支持按订单号查询物流轨迹、查询订单详情、列出用户最近订单。"),
    )
    service = LogisticsService()

    @logistics_mcp.tool(
        name="query_logistics",
        description="按订单号查询物流轨迹，返回订单摘要、最新物流状态与全部物流节点。参数：order_no(订单号,必填)"
    )
    def query_logistics(order_no: str) -> str:
        return service.query_logistics(order_no)

    @logistics_mcp.tool(
        name="query_order",
        description="按订单号查询订单详情（商品、数量、金额、下单时间、状态）。参数：order_no(订单号,必填)"
    )
    def query_order(order_no: str) -> str:
        order = service.get_order(order_no)
        if not order:
            return json.dumps({"status": "no_data", "message": f"未找到订单 {order_no}。"},
                              ensure_ascii=False)
        return json.dumps({"status": "success", "data": order}, cls=DateEncoder, ensure_ascii=False)

    @logistics_mcp.tool(
        name="list_user_orders",
        description="列出某用户的最近订单，用于用户不确定订单号时定位。参数：user_id(默认guest), limit(默认10)"
    )
    def list_user_orders(user_id: str = "guest", limit: int = 10) -> str:
        return service.list_user_orders(user_id, limit)

    logger.info("=== 物流 MCP 服务器信息 ===")
    logger.info(f"名称: {logistics_mcp.name}")

    try:
        print("物流 MCP 服务器已启动，请访问 http://127.0.0.1:8003/mcp")
        logistics_mcp.run(transport="http", host="0.0.0.0", port=8003)
    except Exception as e:
        print(f"服务器启动失败: {e}")


if __name__ == "__main__":
    create_logistics_mcp_server()
