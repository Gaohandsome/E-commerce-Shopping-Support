"""
需求：实现基于 MCP 的商品 / 订单服务器（电商核心数据层）
端口：8001
提供工具：
    - search_products      按关键词/品类/品牌/价格区间筛选商品，支持按价格/销量/评分排序（比价）
    - compare_price        按需求关键词比价，返回同品类低价商品（降序 cheapest first）
    - get_product          按商品ID获取详情
    - list_coupons         列出可用优惠券
    - calculate_coupon     计算某金额使用某优惠券后的优惠（不下单，仅试算）
    - create_order         下单：校验库存、核销优惠券、写入 orders 表并扣减库存

数据源：MySQL travel_rag 库的 products / coupons / orders 表
架构：每次查询独立建连，避免并发跨线程共享连接
"""

import json
import os
import re
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

import mysql.connector
from fastmcp import FastMCP

# 确保项目根目录在 sys.path 中（支持直接以脚本方式运行 python mcp_server/xxx.py）
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from create_logger import logger
from utils.format import DateEncoder, default_encoder

conf = Config()


# ==================== 商品 / 订单服务类 ====================
class ProductService:
    """封装商品、优惠券、订单相关的数据库操作"""

    def _connect(self):
        return mysql.connector.connect(
            host=conf.host,
            user=conf.user,
            password=conf.password,
            database=conf.database,
            autocommit=False  # 下单需要事务
        )

    def _execute_query(self, sql: str, params: list = None) -> str:
        """只读查询，返回 JSON 字符串"""
        conn = None
        cursor = None
        try:
            conn = self._connect()
            cursor = conn.cursor(dictionary=True)
            cursor.execute(sql, params or [])
            results = cursor.fetchall()
            for result in results:
                for key, value in result.items():
                    if isinstance(value, (date, datetime, timedelta, Decimal)):
                        result[key] = default_encoder(value)
            return json.dumps(
                {"status": "success", "data": results} if results
                else {"status": "no_data", "message": "未找到相关商品，请调整筛选条件。"},
                cls=DateEncoder, ensure_ascii=False
            )
        except Exception as e:
            logger.error(f"商品查询错误: {str(e)}")
            return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    # ========== 商品查询 ==========
    def _resolve_category(self, category: str):
        """
        校验并纠正 category 参数。

        LLM 有时会把"商品类型词"（如 充电宝 / 耳机 / 手机）误填到 category，
        而真实品类只有 数码配件 / 智能家居 / 美妆个护 / 服饰鞋包 / 食品饮料 / 运动户外。
        若 category 不是合法品类，则返回 (None, False)，由调用方把它并入 keyword 检索，
        避免 category='充电宝' 这类硬过滤把真实商品全部排除。

        返回：(resolved_category, is_valid)
        """
        conn = None
        cursor = None
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT category FROM products WHERE category IS NOT NULL")
            cats = [r[0] for r in cursor.fetchall() if r[0]]
            if category in cats:
                return category, True
            # 模糊匹配：如 "数码" -> "数码配件"
            fuzzy = [c for c in cats if category in c or c in category]
            if len(fuzzy) == 1:
                return fuzzy[0], True
            return category, False
        except Exception as e:
            logger.error(f"品类校验错误: {str(e)}")
            # 校验失败时按原样使用，避免阻断正常检索
            return category, True
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def search_products(self, keyword: str = None, category: str = None, brand: str = None,
                        min_price: float = None, max_price: float = None,
                        sort_by: str = "price_asc") -> str:
        """
        筛选商品。sort_by 可选：price_asc(价格升序) / price_desc / sales_desc / rating_desc
        """
        # 容错：非法品类（如把"充电宝"当品类）退回关键词检索
        if category:
            resolved, ok = self._resolve_category(category)
            if ok:
                category = resolved
            else:
                if not keyword:
                    keyword = category
                category = None

        sql = ("SELECT id, name, category, brand, price, original_price, stock, "
               "rating, sales_volume, description, specs, tags FROM products WHERE 1=1")
        params = []
        if keyword:
            # 支持多词关键词：按空白（含全角空格）拆分，每个词都需命中（AND）。
            # 例：kw="倍思 充电宝" 时，若按整串 LIKE 会漏掉"倍思 20000mAh 快充充电宝"，
            # 拆分后变成 (倍思) AND (充电宝)，即可正确命中。
            tokens = [t for t in re.split(r"[\s\u3000]+", str(keyword).strip()) if t]
            if not tokens:
                tokens = [str(keyword)]
            for tok in tokens:
                sql += " AND (name LIKE %s OR description LIKE %s OR tags LIKE %s)"
                like = f"%{tok}%"
                params.extend([like, like, like])
        if category:
            sql += " AND category = %s"
            params.append(category)
        if brand:
            sql += " AND brand = %s"
            params.append(brand)
        if min_price is not None:
            sql += " AND price >= %s"
            params.append(min_price)
        if max_price is not None:
            sql += " AND price <= %s"
            params.append(max_price)

        order_map = {
            "price_asc": "price ASC",
            "price_desc": "price DESC",
            "sales_desc": "sales_volume DESC",
            "rating_desc": "rating DESC",
        }
        sql += f" ORDER BY {order_map.get(sort_by, 'price ASC')} LIMIT 50"
        logger.info(f"查询商品: kw={keyword}, cat={category}, brand={brand}, "
                    f"min={min_price}, max={max_price}, sort={sort_by}")
        return self._execute_query(sql, params)

    def compare_price(self, keyword: str, category: str = None) -> str:
        """比价：按需求关键词搜商品，价格升序返回（最便宜在前）"""
        return self.search_products(keyword=keyword, category=category, sort_by="price_asc")

    def get_product(self, product_id: int) -> str:
        sql = ("SELECT id, name, category, brand, price, original_price, stock, "
               "rating, sales_volume, description, specs, tags FROM products WHERE id = %s")
        result = self._execute_query(sql, [product_id])
        return result

    # ========== 优惠券 ==========
    def list_coupons(self) -> str:
        sql = ("SELECT code, name, coupon_type, threshold, value, scope, "
               "valid_from, valid_to, total_count, used_count FROM coupons "
               "WHERE valid_from <= CURDATE() AND valid_to >= CURDATE() "
               "AND used_count < total_count ORDER BY threshold ASC")
        return self._execute_query(sql, [])

    def validate_coupon(self, code: str, amount: float) -> dict:
        """
        校验优惠券并计算优惠金额（不下单）

        返回：
            {"valid": bool, "discount": float, "message": str,
             "coupon_type": str, "threshold": float, "value": float, "scope": str}
        """
        conn = None
        cursor = None
        try:
            conn = self._connect()
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                "SELECT code, name, coupon_type, threshold, value, scope, "
                "valid_from, valid_to, total_count, used_count FROM coupons WHERE code = %s",
                [code]
            )
            row = cursor.fetchone()
            if not row:
                return {"valid": False, "discount": 0.0, "message": f"优惠券 {code} 不存在。"}
            today = date.today()
            if row["valid_from"] and row["valid_from"] > today:
                return {"valid": False, "discount": 0.0,
                        "message": f"优惠券 {code} 尚未生效（{row['valid_from']} 起）。"}
            if row["valid_to"] and row["valid_to"] < today:
                return {"valid": False, "discount": 0.0,
                        "message": f"优惠券 {code} 已过期（{row['valid_to']} 止）。"}
            if row["total_count"] - row["used_count"] <= 0:
                return {"valid": False, "discount": 0.0, "message": f"优惠券 {code} 已被领完。"}

            threshold = float(row["threshold"] or 0)
            if threshold > 0 and amount < threshold:
                return {"valid": False, "discount": 0.0,
                        "message": f"优惠券 {code} 需满 {threshold:.0f} 元可用，当前金额 {amount:.2f} 元不足。"}

            if row["coupon_type"] == "full_reduction":
                discount = float(row["value"])
            else:  # discount 折扣
                discount = round(amount * (1 - float(row["value"])), 2)

            # 折扣后的优惠不得超过订单金额
            discount = min(discount, amount)
            return {
                "valid": True,
                "discount": discount,
                "message": f"优惠券可用，可优惠 {discount:.2f} 元。",
                "coupon_type": row["coupon_type"],
                "threshold": threshold,
                "value": float(row["value"]),
                "scope": row["scope"],
            }
        except Exception as e:
            logger.error(f"优惠券校验错误: {str(e)}")
            return {"valid": False, "discount": 0.0, "message": f"优惠券校验出错：{str(e)}"}
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    # ========== 下单 ==========
    def create_order(self, product_id: int, quantity: int = 1,
                     coupon_code: str = None, user_id: str = "guest") -> str:
        """
        下单：校验库存 → 计算优惠 → 写 orders → 扣库存 → 增 used_count（事务）
        """
        conn = None
        cursor = None
        try:
            conn = self._connect()
            cursor = conn.cursor(dictionary=True)

            # 1) 校验商品与库存
            cursor.execute(
                "SELECT id, name, price, stock FROM products WHERE id = %s FOR UPDATE",
                [product_id]
            )
            product = cursor.fetchone()
            if not product:
                return json.dumps({"status": "error", "message": f"商品ID {product_id} 不存在。"},
                                  ensure_ascii=False)
            if product["stock"] < quantity:
                return json.dumps(
                    {"status": "error",
                     "message": f"库存不足：{product['name']} 仅剩 {product['stock']} 件，无法购买 {quantity} 件。"},
                    ensure_ascii=False)

            unit_price = float(product["price"])
            total_amount = round(unit_price * quantity, 2)

            # 2) 优惠券核销（可选）
            discount_amount = 0.0
            if coupon_code:
                cv = self.validate_coupon(coupon_code, total_amount)
                if not cv["valid"]:
                    return json.dumps({"status": "error", "message": cv["message"]}, ensure_ascii=False)
                discount_amount = cv["discount"]
                # 占用一张券
                cursor.execute(
                    "UPDATE coupons SET used_count = used_count + 1 WHERE code = %s",
                    [coupon_code]
                )
            final_amount = round(total_amount - discount_amount, 2)

            # 3) 写入订单
            order_no = "EC" + datetime.now().strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:6].upper()
            cursor.execute(
                """INSERT INTO orders
                   (order_no, user_id, product_id, product_name, quantity, unit_price,
                    total_amount, coupon_code, discount_amount, final_amount, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'paid')""",
                [order_no, user_id, product_id, product["name"], quantity, unit_price,
                 total_amount, coupon_code, discount_amount, final_amount]
            )

            # 4) 扣减库存
            cursor.execute(
                "UPDATE products SET stock = stock - %s WHERE id = %s",
                [quantity, product_id]
            )

            conn.commit()
            logger.info(f"下单成功: order_no={order_no}, product={product['name']}, "
                        f"final={final_amount}")
            return json.dumps({
                "status": "success",
                "data": {
                    "order_no": order_no,
                    "product_name": product["name"],
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "total_amount": total_amount,
                    "coupon_code": coupon_code,
                    "discount_amount": discount_amount,
                    "final_amount": final_amount,
                    "message": f"下单成功！共 {quantity} 件 {product['name']}，"
                               f"应付 ¥{final_amount:.2f}。"
                }
            }, ensure_ascii=False)
        except Exception as e:
            if conn is not None:
                conn.rollback()
            logger.error(f"下单失败: {str(e)}")
            return json.dumps({"status": "error", "message": f"下单失败：{str(e)}"}, ensure_ascii=False)
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()


# ==================== 创建 MCP 服务器 ====================
def create_product_mcp_server():
    product_mcp = FastMCP(
        name="ProductTools",
        instructions=("电商商品与订单工具，基于 products / coupons / orders 表。"
                      "支持商品筛选与比价、优惠券查询与试算、下单。"),
    )
    service = ProductService()

    @product_mcp.tool(
        name="search_products",
        description="筛选商品。参数：keyword(关键词,可选), category(品类,可选), brand(品牌,可选), "
                    "min_price(最低价,可选), max_price(最高价,可选), sort_by(排序: price_asc/price_desc/sales_desc/rating_desc,默认price_asc)"
    )
    def search_products(keyword: str = None, category: str = None, brand: str = None,
                        min_price: float = None, max_price: float = None,
                        sort_by: str = "price_asc") -> str:
        return service.search_products(keyword, category, brand, min_price, max_price, sort_by)

    @product_mcp.tool(
        name="compare_price",
        description="商品比价：按需求关键词搜索同品类商品并按价格升序返回（最便宜的排在最前），参数：keyword(需求关键词,必填), category(品类,可选)"
    )
    def compare_price(keyword: str, category: str = None) -> str:
        return service.compare_price(keyword, category)

    @product_mcp.tool(
        name="get_product",
        description="按商品ID获取商品详情，参数：product_id(商品ID,必填)"
    )
    def get_product(product_id: int) -> str:
        return service.get_product(product_id)

    @product_mcp.tool(
        name="list_coupons",
        description="列出当前可用的优惠券（在有效期且未领完）"
    )
    def list_coupons() -> str:
        return service.list_coupons()

    @product_mcp.tool(
        name="calculate_coupon",
        description="试算某金额使用某优惠券可优惠多少（不下单）。参数：code(券码,必填), amount(订单金额,必填)"
    )
    def calculate_coupon(code: str, amount: float) -> str:
        cv = service.validate_coupon(code, amount)
        return json.dumps(cv, ensure_ascii=False)

    @product_mcp.tool(
        name="create_order",
        description="下单购买商品。参数：product_id(商品ID,必填), quantity(数量,默认1), "
                    "coupon_code(优惠券码,可选), user_id(用户标识,可选)"
    )
    def create_order(product_id: int, quantity: int = 1,
                     coupon_code: str = None, user_id: str = "guest") -> str:
        return service.create_order(product_id, quantity, coupon_code, user_id)

    logger.info("=== 商品/订单 MCP 服务器信息 ===")
    logger.info(f"名称: {product_mcp.name}")
    logger.info(f"描述: {product_mcp.instructions}")

    try:
        print("服务器已启动，请访问 http://127.0.0.1:8001/mcp")
        product_mcp.run(transport="http", host="0.0.0.0", port=8001)
    except Exception as e:
        print(f"服务器启动失败: {e}")


if __name__ == '__main__':
    create_product_mcp_server()
