"""
需求：实现基于 MCP 的售后服务端（退换货规则引擎 + 售后工单，本地 mock）
端口：8005
提供工具：
    - get_order                按订单号查询订单（含商品品类）
    - get_after_sale_rule      按品类查询退换货规则
    - judge_after_sale         规则引擎：判断某订单能否退货/换货/退款，给出时效与条件
    - create_after_sale_ticket 创建售后工单（内部会先做规则校验）
    - query_after_sale_ticket  查询售后工单状态

数据源：MySQL travel_rag 库的 orders / products / logistics / after_sale_rules / after_sale_requests
架构：与 mcp_product_server.py 一致（每次操作独立建连）
"""

import json
import os
import uuid
from datetime import datetime, date

import mysql.connector
import pytz
from fastmcp import FastMCP

# 确保项目根目录在 sys.path 中（支持直接以脚本方式运行 python mcp_server/xxx.py）
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from create_logger import logger
from utils.format import DateEncoder, default_encoder

conf = Config()

TYPE_CN = {"return": "退货退款", "exchange": "换货", "refund": "仅退款"}
STATUS_CN = {"created": "已创建", "processing": "处理中",
             "approved": "审核通过", "rejected": "已驳回", "completed": "已完成"}
SHIP_CN = {"pending": "待发货", "shipped": "已发货", "in_transit": "运输中",
           "out_for_delivery": "派送中", "delivered": "已签收", "exception": "物流异常"}


class AfterSaleService:
    """退换货规则引擎 + 售后工单"""

    def _connect(self):
        return mysql.connector.connect(
            host=conf.host,
            user=conf.user,
            password=conf.password,
            database=conf.database,
            autocommit=True,
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

    def _today(self) -> date:
        return datetime.now(pytz.timezone("Asia/Shanghai")).date()

    # ---------- 订单 / 规则 ----------
    def get_order(self, order_no: str):
        rows = self._query(
            "SELECT o.order_no, o.user_id, o.product_id, o.product_name, o.quantity, "
            "o.unit_price, o.total_amount, o.coupon_code, o.discount_amount, o.final_amount, "
            "o.status, o.created_at, p.category, "
            "(SELECT l.status FROM logistics l WHERE l.order_no = o.order_no "
            " ORDER BY l.node_time DESC LIMIT 1) AS shipping_status "
            "FROM orders o LEFT JOIN products p ON o.product_id = p.id "
            "WHERE o.order_no = %s",
            [order_no],
        )
        if rows:
            row = rows[0]
            ss = row.get("shipping_status")
            row["shipping_status_cn"] = SHIP_CN.get(ss, "未发货") if ss else "未发货"
        return rows[0] if rows else None

    def get_rule(self, category: str):
        rows = self._query(
            "SELECT category, return_window_days, exchange_window_days, refundable, "
            "exchangeable, conditions, note FROM after_sale_rules WHERE category = %s",
            [category],
        )
        return rows[0] if rows else None

    def _base_date(self, order_no: str, order: dict):
        """计算售后时效起算日：优先取物流"已签收"节点，否则取下单日"""
        rows = self._query(
            "SELECT node_time FROM logistics WHERE order_no = %s AND status = 'delivered' "
            "ORDER BY node_time DESC LIMIT 1",
            [order_no],
        )
        if rows and rows[0]["node_time"]:
            v = rows[0]["node_time"]
            if isinstance(v, datetime):
                return v.date()
            if isinstance(v, date):
                return v
            try:
                return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
            except Exception:
                pass
        created = order.get("created_at")
        if isinstance(created, (datetime, date)):
            return created.date() if isinstance(created, datetime) else created
        try:
            return datetime.strptime(str(created)[:10], "%Y-%m-%d").date()
        except Exception:
            return self._today()

    # ---------- 规则引擎 ----------
    def judge_after_sale(self, order_no: str, request_type: str = "return") -> dict:
        order = self.get_order(order_no)
        if not order:
            return {"eligible": False, "message": f"未找到订单 {order_no}，请确认订单号。"}

        category = order.get("category")
        if not category:
            return {"eligible": False, "message": f"订单 {order_no} 的商品品类缺失，无法匹配售后规则。"}

        rule = self.get_rule(category)
        if not rule:
            return {"eligible": False, "message": f"暂未配置「{category}」品类的售后规则。"}

        request_type = (request_type or "return").lower()
        if request_type not in TYPE_CN:
            return {"eligible": False, "message": f"不支持的售后类型：{request_type}（可选 return/exchange/refund）。"}

        # 退款/退货看 refundable，换货看 exchangeable
        if request_type in ("return", "refund") and not rule["refundable"]:
            return {"eligible": False, "request_type": request_type, "category": category,
                    "message": f"「{category}」品类不支持无理由退货退款。{rule.get('note') or ''}".strip(),
                    "conditions": rule.get("conditions")}
        if request_type == "exchange" and not rule["exchangeable"]:
            return {"eligible": False, "request_type": request_type, "category": category,
                    "message": f"「{category}」品类不支持换货。{rule.get('note') or ''}".strip(),
                    "conditions": rule.get("conditions")}

        window = rule["exchange_window_days"] if request_type == "exchange" else rule["return_window_days"]
        base = self._base_date(order_no, order)
        today = self._today()
        elapsed = (today - base).days
        deadline = None
        try:
            from datetime import timedelta
            deadline = (base + timedelta(days=int(window))).isoformat()
        except Exception:
            pass

        if window <= 0:
            return {"eligible": False, "request_type": request_type, "category": category,
                    "message": f"「{category}」不支持{TYPE_CN[request_type]}。", "conditions": rule.get("conditions")}

        if elapsed > window:
            return {"eligible": False, "request_type": request_type, "category": category,
                    "days_elapsed": elapsed, "window_days": window, "deadline": deadline,
                    "message": f"已超出{TYPE_CN[request_type]}时效：自 {base.isoformat()} 起已 {elapsed} 天，"
                               f"超过 {window} 天窗口。",
                    "conditions": rule.get("conditions")}

        return {
            "eligible": True,
            "request_type": request_type,
            "request_type_cn": TYPE_CN[request_type],
            "category": category,
            "days_elapsed": elapsed,
            "window_days": window,
            "base_date": base.isoformat(),
            "deadline": deadline,
            "conditions": rule.get("conditions"),
            "note": rule.get("note"),
            "message": f"符合{TYPE_CN[request_type]}条件：自 {base.isoformat()} 起 {elapsed} 天，"
                       f"仍在 {window} 天窗口内（截止 {deadline}）。",
        }

    # ---------- 工单 ----------
    def create_after_sale_ticket(self, order_no: str, request_type: str = "return",
                                 reason: str = None) -> dict:
        judge = self.judge_after_sale(order_no, request_type)
        if not judge.get("eligible"):
            return {"status": "error", "message": judge.get("message", "不符合售后条件。"),
                    "judge": judge}

        order = self.get_order(order_no)
        ticket_no = "AS" + datetime.now(pytz.timezone("Asia/Shanghai")).strftime("%Y%m%d%H%M%S") \
                    + uuid.uuid4().hex[:4].upper()
        conn = None
        cursor = None
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO after_sale_requests "
                "(ticket_no, order_no, product_id, product_name, request_type, reason, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'created')",
                [ticket_no, order_no, order.get("product_id"), order.get("product_name"),
                 request_type, reason or "用户申请售后"],
            )
            conn.commit()
            logger.info(f"售后工单创建: {ticket_no} order={order_no} type={request_type}")
            return {
                "status": "success",
                "data": {
                    "ticket_no": ticket_no,
                    "order_no": order_no,
                    "product_name": order.get("product_name"),
                    "request_type": request_type,
                    "request_type_cn": TYPE_CN.get(request_type, request_type),
                    "reason": reason or "用户申请售后",
                    "status": "created",
                    "status_cn": STATUS_CN["created"],
                    "message": f"售后工单已创建：{ticket_no}（{TYPE_CN.get(request_type, request_type)}）。"
                               f"我们将在 24 小时内审核处理。",
                },
            }
        except Exception as e:
            if conn is not None:
                conn.rollback()
            logger.error(f"创建售后工单失败: {str(e)}")
            return {"status": "error", "message": f"创建售后工单失败：{str(e)}"}
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def query_after_sale_ticket(self, ticket_no: str = None, order_no: str = None) -> str:
        if ticket_no:
            rows = self._query(
                "SELECT ticket_no, order_no, product_name, request_type, reason, status, created_at "
                "FROM after_sale_requests WHERE ticket_no = %s", [ticket_no])
        elif order_no:
            rows = self._query(
                "SELECT ticket_no, order_no, product_name, request_type, reason, status, created_at "
                "FROM after_sale_requests WHERE order_no = %s ORDER BY created_at DESC", [order_no])
        else:
            return json.dumps({"status": "error", "message": "请提供 ticket_no 或 order_no。"},
                              ensure_ascii=False)
        for r in rows:
            r["request_type_cn"] = TYPE_CN.get(r["request_type"], r["request_type"])
            r["status_cn"] = STATUS_CN.get(r["status"], r["status"])
        return json.dumps(
            {"status": "success", "data": rows} if rows
            else {"status": "no_data", "message": "未找到相关售后工单。"},
            cls=DateEncoder, ensure_ascii=False,
        )


# ==================== 创建 MCP 服务器 ====================
def create_aftersale_mcp_server():
    aftersale_mcp = FastMCP(
        name="AfterSaleTools",
        instructions=("电商售后工具，基于 orders / products / logistics / after_sale_rules / "
                      "after_sale_requests 表。支持退换货规则判断与售后工单创建。"),
    )
    service = AfterSaleService()

    @aftersale_mcp.tool(name="get_order",
                        description="按订单号查询订单详情（含商品品类、发货状态 shipping_status_cn）。参数：order_no(订单号,必填)")
    def get_order(order_no: str) -> str:
        order = service.get_order(order_no)
        if not order:
            return json.dumps({"status": "no_data", "message": f"未找到订单 {order_no}。"},
                              ensure_ascii=False)
        return json.dumps({"status": "success", "data": order}, cls=DateEncoder, ensure_ascii=False)

    @aftersale_mcp.tool(name="get_after_sale_rule",
                        description="按商品品类查询退换货规则。参数：category(品类,必填)")
    def get_after_sale_rule(category: str) -> str:
        rule = service.get_rule(category)
        return json.dumps(
            {"status": "success", "data": rule} if rule
            else {"status": "no_data", "message": f"暂未配置「{category}」品类的售后规则。"},
            cls=DateEncoder, ensure_ascii=False)

    @aftersale_mcp.tool(name="judge_after_sale",
                        description="规则引擎：判断某订单是否可退货/换货/退款，返回时效窗口与条件。"
                                    "参数：order_no(订单号,必填), request_type(return/exchange/refund,默认return)")
    def judge_after_sale(order_no: str, request_type: str = "return") -> str:
        return json.dumps(service.judge_after_sale(order_no, request_type), ensure_ascii=False)

    @aftersale_mcp.tool(name="create_after_sale_ticket",
                        description="创建售后工单（内部先做规则校验，不符合条件会被拒绝）。"
                                    "参数：order_no(订单号,必填), request_type(return/exchange/refund,默认return), reason(原因,可选)")
    def create_after_sale_ticket(order_no: str, request_type: str = "return", reason: str = None) -> str:
        return json.dumps(service.create_after_sale_ticket(order_no, request_type, reason),
                          ensure_ascii=False)

    @aftersale_mcp.tool(name="query_after_sale_ticket",
                        description="查询售后工单状态。参数：ticket_no(工单号,可选) 或 order_no(订单号,可选)，二者其一")
    def query_after_sale_ticket(ticket_no: str = None, order_no: str = None) -> str:
        return service.query_after_sale_ticket(ticket_no, order_no)

    logger.info("=== 售后 MCP 服务器信息 ===")
    logger.info(f"名称: {aftersale_mcp.name}")

    try:
        print("售后 MCP 服务器已启动，请访问 http://127.0.0.1:8005/mcp")
        aftersale_mcp.run(transport="http", host="0.0.0.0", port=8005)
    except Exception as e:
        print(f"服务器启动失败: {e}")


if __name__ == "__main__":
    create_aftersale_mcp_server()
