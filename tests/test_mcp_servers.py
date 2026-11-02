"""
MCP 服务器测试模块（电商版）

测试内容：
1. 格式编码工具（纯函数测试，零依赖）
2. 商品/订单 MCP 集成测试（需要 MySQL + 演示数据）
3. 推荐 MCP 配置测试（纯导入，不依赖 Milvus / Embedding 网络）

运行方式：
    cd ShopMate
    python -m pytest tests/test_mcp_servers.py -v
前置：先执行 sql/create_ecommerce_tables.sql 与 sql/demo_product_data.sql
"""

import unittest
import json
import sys
import os
from datetime import date, datetime, timedelta
from decimal import Decimal

# 确保能导入 ShopMate 模块
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ==================== 1. 格式编码工具测试（纯函数，零依赖） ====================

class TestFormatEncoder(unittest.TestCase):
    """测试 default_encoder 函数和 DateEncoder 类"""

    def setUp(self):
        from utils.format import default_encoder, DateEncoder
        self.default_encoder_func = default_encoder
        self.date_encoder_cls = DateEncoder

    def test_encode_datetime(self):
        dt = datetime(2025, 7, 30, 14, 30, 0)
        self.assertEqual(self.default_encoder_func(dt), '2025-07-30 14:30:00')

    def test_encode_date(self):
        d = date(2025, 7, 30)
        self.assertEqual(self.default_encoder_func(d), '2025-07-30')

    def test_encode_timedelta(self):
        td = timedelta(hours=5, minutes=30)
        self.assertEqual(self.default_encoder_func(td), '5:30:00')

    def test_encode_decimal(self):
        dec = Decimal('123.45')
        result = self.default_encoder_func(dec)
        self.assertIsInstance(result, float)
        self.assertAlmostEqual(result, 123.45)

    def test_encode_unknown(self):
        obj = "hello"
        self.assertEqual(self.default_encoder_func(obj), "hello")

    def test_date_encoder_json_dumps(self):
        data = {
            "date": date(2025, 7, 30),
            "datetime": datetime(2025, 7,30, 14, 30, 0),
            "value": Decimal('99.99')
        }
        result = json.dumps(data, cls=self.date_encoder_cls)
        parsed = json.loads(result)
        self.assertEqual(parsed["date"], "2025-07-30")
        self.assertEqual(parsed["datetime"], "2025-07-30 14:30:00")
        self.assertEqual(parsed["value"], 99.99)


# ==================== 2. 商品/订单 MCP 集成测试（需要 MySQL） ====================

class TestProductMCPIntegration(unittest.TestCase):
    """集成测试：商品/订单 MCP 服务（ProductService）"""

    @classmethod
    def setUpClass(cls):
        from mcp_server.mcp_product_server import ProductService
        cls.service = ProductService()

    def test_search_products_by_keyword(self):
        """按关键词搜索商品"""
        result = self.service.search_products(keyword="蓝牙耳机")
        parsed = json.loads(result)
        self.assertEqual(parsed["status"], "success")
        self.assertGreater(len(parsed["data"]), 0)
        for item in parsed["data"]:
            self.assertIn("耳机", item["name"])

    def test_search_products_by_category(self):
        """按品类筛选"""
        result = self.service.search_products(category="美妆个护")
        parsed = json.loads(result)
        self.assertEqual(parsed["status"], "success")
        for item in parsed["data"]:
            self.assertEqual(item["category"], "美妆个护")

    def test_compare_price_sorted_asc(self):
        """比价：按价格升序返回"""
        result = self.service.compare_price("蓝牙耳机")
        parsed = json.loads(result)
        self.assertEqual(parsed["status"], "success")
        prices = [float(p["price"]) for p in parsed["data"]]
        self.assertEqual(prices, sorted(prices))

    def test_get_product(self):
        """按 ID 获取商品详情"""
        result = self.service.get_product(1)
        parsed = json.loads(result)
        self.assertEqual(parsed["status"], "success")
        self.assertEqual(parsed["data"][0]["id"], 1)

    def test_list_coupons(self):
        """列出可用优惠券"""
        result = self.service.list_coupons()
        parsed = json.loads(result)
        self.assertEqual(parsed["status"], "success")
        self.assertGreater(len(parsed["data"]), 0)
        for c in parsed["data"]:
            self.assertIn("code", c)

    def test_calculate_coupon_full_reduction_ok(self):
        """满减券可用场景"""
        cv = self.service.validate_coupon("FULL200", 300.0)
        self.assertTrue(cv["valid"])
        self.assertAlmostEqual(cv["discount"], 30.0)

    def test_calculate_coupon_full_reduction_below_threshold(self):
        """满减券未达门槛"""
        cv = self.service.validate_coupon("FULL200", 100.0)
        self.assertFalse(cv["valid"])

    def test_calculate_coupon_discount_type(self):
        """折扣券：8折算优惠"""
        cv = self.service.validate_coupon("BEAUTY20", 100.0)
        self.assertTrue(cv["valid"])
        self.assertAlmostEqual(cv["discount"], 20.0)

    def test_create_order_without_coupon(self):
        """无优惠券下单"""
        result = self.service.create_order(product_id=1, quantity=1)
        parsed = json.loads(result)
        self.assertEqual(parsed["status"], "success")
        self.assertAlmostEqual(parsed["data"]["final_amount"], parsed["data"]["total_amount"])

    def test_create_order_with_coupon(self):
        """使用满减券下单"""
        result = self.service.create_order(product_id=1, quantity=2, coupon_code="FULL200")
        parsed = json.loads(result)
        self.assertEqual(parsed["status"], "success")
        # 2 件 * 单价 - 30 优惠
        expected = round(parsed["data"]["total_amount"] - 30.0, 2)
        self.assertAlmostEqual(parsed["data"]["final_amount"], expected)
        self.assertAlmostEqual(parsed["data"]["discount_amount"], 30.0)

    def test_create_order_insufficient_stock(self):
        """库存不足下单应失败"""
        # 取一个库存较小的商品（如华为手表 stock=90，下单 999 应失败）
        result = self.service.create_order(product_id=11, quantity=999)
        parsed = json.loads(result)
        self.assertEqual(parsed["status"], "error")


# ==================== 2b. 物流 MCP 集成测试（需要 MySQL + 演示物流数据） ====================

class TestLogisticsMCPIntegration(unittest.TestCase):
    """集成测试：物流 MCP 服务（LogisticsService）"""

    @classmethod
    def setUpClass(cls):
        from mcp_server.mcp_logistics_server import LogisticsService
        cls.service = LogisticsService()

    def test_query_logistics_in_transit(self):
        """运输中订单：返回节点与最新状态"""
        parsed = json.loads(self.service.query_logistics("EC20261030002"))
        self.assertEqual(parsed["status"], "success")
        self.assertGreater(len(parsed["data"]["nodes"]), 0)
        self.assertEqual(parsed["data"]["tracking"]["latest_status"], "in_transit")

    def test_query_logistics_pending(self):
        """待发货订单：最新状态 pending"""
        parsed = json.loads(self.service.query_logistics("EC20261030001"))
        self.assertEqual(parsed["status"], "success")
        self.assertEqual(parsed["data"]["tracking"]["latest_status"], "pending")

    def test_query_logistics_not_found(self):
        """不存在的订单：no_data"""
        parsed = json.loads(self.service.query_logistics("EC00000000000"))
        self.assertEqual(parsed["status"], "no_data")

    def test_list_user_orders(self):
        """列出用户订单"""
        parsed = json.loads(self.service.list_user_orders("guest"))
        self.assertEqual(parsed["status"], "success")
        self.assertGreaterEqual(len(parsed["data"]), 4)


# ==================== 2c. 售后 MCP 集成测试（需要 MySQL + 规则数据） ====================

class TestAfterSaleMCPIntegration(unittest.TestCase):
    """集成测试：售后 MCP 服务（AfterSaleService 规则引擎 + 工单）"""

    @classmethod
    def setUpClass(cls):
        from mcp_server.mcp_aftersale_server import AfterSaleService
        cls.service = AfterSaleService()

    def test_judge_return_eligible_pending_order(self):
        """待发货订单（服饰）在 7 天窗口内可退货"""
        j = self.service.judge_after_sale("EC20261030001", "return")
        self.assertTrue(j["eligible"])
        self.assertEqual(j["category"], "服饰鞋包")
        self.assertEqual(j["window_days"], 7)

    def test_judge_return_eligible_delivered_order(self):
        """已签收订单在时效内可退货，起算日取签收日"""
        j = self.service.judge_after_sale("EC20261030003", "return")
        self.assertTrue(j["eligible"])
        self.assertIsNotNone(j["deadline"])

    def test_judge_unsupported_category(self):
        """食品饮料不支持无理由退货"""
        rule = self.service.get_rule("食品饮料")
        self.assertIsNotNone(rule)
        self.assertEqual(rule["refundable"], 0)

    def test_judge_order_not_found(self):
        """不存在订单不可售后"""
        j = self.service.judge_after_sale("EC00000000000", "return")
        self.assertFalse(j["eligible"])

    def test_create_and_query_ticket(self):
        """创建售后工单并可按订单号查回"""
        res = self.service.create_after_sale_ticket(
            "EC20261030002", "return", "测试：不想要了"
        )
        self.assertEqual(res["status"], "success")
        self.assertTrue(res["data"]["ticket_no"].startswith("AS"))

        parsed = json.loads(self.service.query_after_sale_ticket(order_no="EC20261030002"))
        self.assertEqual(parsed["status"], "success")
        self.assertGreaterEqual(len(parsed["data"]), 1)


# ==================== 3. 推荐 MCP 配置测试（纯导入，不依赖网络） ====================

class TestRecommendationMCPConfig(unittest.TestCase):
    """推荐 MCP 服务器配置测试（不实际启动服务器 / 不调用 Milvus / Embedding）"""

    def test_module_importable(self):
        # 导入即代表模块与依赖（fastmcp / pymilvus / config）可用
        from mcp_server import mcp_recommendation_server as m
        self.assertTrue(hasattr(m, "create_recommendation_mcp_server"))
        self.assertTrue(hasattr(m, "search_products_in_milvus"))
        self.assertTrue(hasattr(m, "get_embedding"))

    def test_constants(self):
        from mcp_server import mcp_recommendation_server as m
        self.assertEqual(m.COLLECTION_NAME, "products")
        # 电商业务使用独立库，避免与旅游期集合混用
        self.assertEqual(m.DB_NAME, "ecommerce")
        self.assertEqual(m.EMBEDDING_DIM, 1024)


# ==================== 4. 图片 MCP 配置测试（纯函数，不发起网络请求） ====================

class TestImageMCPConfig(unittest.TestCase):
    """商品实拍图 MCP 的检索计划与降级策略测试"""

    def test_module_importable(self):
        from mcp_server import mcp_image_server as m
        self.assertTrue(hasattr(m, "create_image_mcp_server"))
        self.assertTrue(hasattr(m, "search_product_photos_impl"))

    def test_generic_term_extraction(self):
        from mcp_server.mcp_image_server import _generic_term
        self.assertEqual(_generic_term("声海 X1 真无线蓝牙耳机", "数码配件"), "真无线蓝牙耳机")
        self.assertEqual(_generic_term("倍思 20000mAh 快充充电宝", ""), "快充充电宝")
        # 识别不出品类词时退回传入的品类
        self.assertEqual(_generic_term("神秘商品A", "智能家居"), "智能家居")

    def test_query_plan_levels(self):
        from mcp_server.mcp_image_server import _query_plan
        plan = _query_plan("声海 X1 真无线蓝牙耳机", "声海", "数码配件")
        levels = [level for _, level in plan]
        self.assertEqual(levels[0], "exact")
        self.assertIn("generic", levels)
        # 第一级必须是完整商品名，保证先找商品本身
        self.assertEqual(plan[0][0], "声海 X1 真无线蓝牙耳机")

    def test_empty_product_rejected(self):
        from mcp_server.mcp_image_server import search_product_photos_impl
        result = search_product_photos_impl("")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["images"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
