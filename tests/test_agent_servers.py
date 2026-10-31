"""
Agent 服务器测试模块（电商版）

测试内容：
1. AgentCard 定义验证（纯属性检查，零依赖，导入即可）
2. Agent 服务器实例化验证

说明：涉及 LLM + MCP 的端到端集成测试需要对应 MCP 服务运行且 LLM API 可访问，
     此处只做无需外部依赖的卡片/实例化校验，保证 pytest 轻量可重复。

运行方式：
    cd SmartVoyage
    python -m pytest tests/test_agent_servers.py -v
"""

import unittest
import sys
import os

# 确保能导入 SmartVoyage 模块
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ==================== 1. AgentCard 定义测试（纯属性检查，零依赖） ====================

class TestRecommendationAgentCard(unittest.TestCase):
    """测试导购推荐 Agent 的代理卡片定义"""

    def test_agent_card_basic(self):
        from a2a_server.recommendation_server import agent_card
        self.assertEqual(agent_card.name, "RecommendationAssistant")
        self.assertIn("导购", agent_card.description)
        self.assertIn(agent_card.url, ["http://localhost:5005", "http://127.0.0.1:5005"])
        self.assertEqual(len(agent_card.skills), 1)

    def test_skill_examples(self):
        from a2a_server.recommendation_server import agent_card
        skill = agent_card.skills[0]
        self.assertIn("recommend", skill.name.lower())
        self.assertIsNotNone(skill.description)
        self.assertGreater(len(skill.examples), 0)


class TestOrderAgentCard(unittest.TestCase):
    """测试订单 Agent 的代理卡片定义"""

    def test_agent_card_basic(self):
        from a2a_server.order_server import agent_card
        self.assertEqual(agent_card.name, "OrderAssistant")
        self.assertIn(agent_card.url, ["http://localhost:5006", "http://127.0.0.1:5006"])

    def test_has_three_skills(self):
        from a2a_server.order_server import agent_card
        skill_names = [s.name.lower() for s in agent_card.skills]
        self.assertTrue(any('compare' in n for n in skill_names), "缺少比价技能")
        self.assertTrue(any('coupon' in n for n in skill_names), "缺少优惠券技能")
        self.assertTrue(any('order' in n or 'place' in n for n in skill_names), "缺少下单技能")


class TestCopywritingAgentCard(unittest.TestCase):
    """测试小红书种草文案 Agent 的代理卡片定义（沿用原有）"""

    def test_agent_card_skills(self):
        from a2a_server.copywriting_server import agent_card
        self.assertEqual(agent_card.name, "CopywritingAssistant")
        self.assertIn(agent_card.url, ["http://localhost:5009", "http://127.0.0.1:5009"])
        self.assertEqual(len(agent_card.skills), 3)

    def test_has_copywriting_and_fact_collection_skills(self):
        from a2a_server.copywriting_server import agent_card
        skill_names = [s.name.lower() for s in agent_card.skills]
        self.assertTrue(any('xiaohongshu' in n for n in skill_names), "缺少小红书文案技能")
        self.assertTrue(any('product' in n for n in skill_names), "缺少商品种草技能")
        self.assertTrue(any('collect' in n for n in skill_names), "缺少素材收集技能")

    def test_downstream_agents_are_ecommerce(self):
        """文案代理只应向导购/订单代理取素材，不再依赖已删除的旅游代理"""
        from a2a_server.copywriting_server import AGENT_URLS
        self.assertEqual(
            set(AGENT_URLS.keys()),
            {"RecommendationAssistant", "OrderAssistant"},
        )


class TestLogisticsAgentCard(unittest.TestCase):
    """测试物流 Agent 的代理卡片定义"""

    def test_agent_card_basic(self):
        from a2a_server.logistics_server import agent_card
        self.assertEqual(agent_card.name, "LogisticsAssistant")
        self.assertIn(agent_card.url, ["http://localhost:5007", "http://127.0.0.1:5007"])

    def test_has_three_skills(self):
        from a2a_server.logistics_server import agent_card
        skill_names = [s.name.lower() for s in agent_card.skills]
        self.assertTrue(any('logistics' in n or 'track' in n for n in skill_names), "缺物流查询技能")
        self.assertTrue(any('order' in n for n in skill_names), "缺订单查询技能")


class TestAfterSaleAgentCard(unittest.TestCase):
    """测试售后 Agent 的代理卡片定义"""

    def test_agent_card_basic(self):
        from a2a_server.aftersale_server import agent_card
        self.assertEqual(agent_card.name, "AfterSaleAssistant")
        self.assertIn(agent_card.url, ["http://localhost:5008", "http://127.0.0.1:5008"])

    def test_has_three_skills(self):
        from a2a_server.aftersale_server import agent_card
        self.assertEqual(len(agent_card.skills), 3)
        skill_names = [s.name.lower() for s in agent_card.skills]
        self.assertTrue(any('judge' in n for n in skill_names), "缺规则判断技能")
        self.assertTrue(any('create' in n for n in skill_names), "缺工单创建技能")
        self.assertTrue(any('query' in n for n in skill_names), "缺工单查询技能")


# ==================== 2. Agent 服务器实例化测试（零外部依赖） ====================

class TestAgentInstantiation(unittest.TestCase):
    """验证各 A2A 服务器可正常实例化（仅构造对象，不启动端口）"""

    def test_recommendation_server_instance(self):
        from a2a_server.recommendation_server import RecommendationServer
        server = RecommendationServer()
        self.assertEqual(server.agent_card.name, "RecommendationAssistant")

    def test_order_server_instance(self):
        from a2a_server.order_server import OrderServer
        server = OrderServer()
        self.assertEqual(server.agent_card.name, "OrderAssistant")

    def test_logistics_server_instance(self):
        from a2a_server.logistics_server import LogisticsServer
        server = LogisticsServer()
        self.assertEqual(server.agent_card.name, "LogisticsAssistant")

    def test_aftersale_server_instance(self):
        from a2a_server.aftersale_server import AfterSaleServer
        server = AfterSaleServer()
        self.assertEqual(server.agent_card.name, "AfterSaleAssistant")

    def test_copywriting_server_instance(self):
        from a2a_server.copywriting_server import CopywritingServer
        server = CopywritingServer()
        self.assertEqual(server.agent_card.name, "CopywritingAssistant")


class TestCopywritingFactGuards(unittest.TestCase):
    """种草文案的防幻觉闸门（纯函数，不调用大模型）"""

    PRODUCT = {
        "id": 1,
        "name": "声海 X1 真无线蓝牙耳机",
        "brand": "声海",
        "category": "数码配件",
        "price": 299.0,
        "original_price": 399.0,
        "stock": 120,
        "rating": 4.6,
        "sales_volume": 5000,
        "description": "半入耳设计，蓝牙5.3，单次续航6小时，通透模式自然。",
        "specs": '{"续航": "6h+24h", "蓝牙": "5.3", "佩戴方式": "半入耳"}',
    }

    def test_render_product_info_uses_exact_numbers(self):
        """【商品信息】必须用工具返回的真实价格/库存，不能由 LLM 复述"""
        from a2a_server.copywriting_server import _render_product_info
        coupon = {"code": "FULL200", "name": "全场满200减30",
                  "coupon_type": "full_reduction", "threshold": 200, "value": 30,
                  "scope": "all"}
        text = _render_product_info(self.PRODUCT, coupon, 269.0)
        self.assertIn("¥299.00", text)
        self.assertIn("原价 ¥399.00", text)
        self.assertIn("库存：120 件", text)
        self.assertIn("券后 ¥269.00", text)
        self.assertIn("FULL200", text)

    def test_render_product_info_without_coupon(self):
        from a2a_server.copywriting_server import _render_product_info
        text = _render_product_info(self.PRODUCT, None, 299.0)
        self.assertIn("未查询到该商品可用优惠券", text)

    def test_best_coupon_picks_max_discount_within_scope(self):
        from a2a_server.copywriting_server import _best_coupon
        coupons = {
            "NEW10": {"code": "NEW10", "coupon_type": "full_reduction",
                      "threshold": 0, "value": 10, "scope": "all"},
            "FULL200": {"code": "FULL200", "coupon_type": "full_reduction",
                        "threshold": 200, "value": 30, "scope": "all"},
            "FULL500": {"code": "FULL500", "coupon_type": "full_reduction",
                        "threshold": 500, "value": 80, "scope": "all"},
            "BEAUTY20": {"code": "BEAUTY20", "coupon_type": "discount",
                         "threshold": 0, "value": 0.8, "scope": "美妆个护"},
        }
        coupon, final = _best_coupon(coupons, self.PRODUCT)
        self.assertEqual(coupon["code"], "FULL200")   # 满500不满足门槛，美妆券不适用
        self.assertAlmostEqual(final, 269.0, places=2)

    def test_scrub_removes_unsupported_hardware_details(self):
        """没有依据的包装/配件/指示灯/触控细节必须被删除"""
        from a2a_server.copywriting_server import scrub_unsupported_details
        text = (
            "【正文】\n戴上去很舒服。附赠三对硅胶耳翼（S/M/L）。耳机仓带呼吸灯。触控灵敏。\n\n"
            "【商品信息】\n- 商品：声海 X1\n"
        )
        out = scrub_unsupported_details(text, self.PRODUCT)
        self.assertIn("戴上去很舒服", out)
        self.assertNotIn("硅胶耳翼", out)
        self.assertNotIn("呼吸灯", out)
        self.assertNotIn("触控", out)
        self.assertIn("【商品信息】", out)

    def test_scrub_keeps_details_that_tools_returned(self):
        """工具确实返回过的细节（描述里写了"磨砂"）不应被误删"""
        from a2a_server.copywriting_server import scrub_unsupported_details
        product = dict(self.PRODUCT, description="磨砂机身，半入耳设计，蓝牙5.3。")
        text = "【正文】\n机身是磨砂质感，握着很舒服。\n\n【商品信息】\n- 商品：x\n"
        out = scrub_unsupported_details(text, product)
        self.assertIn("磨砂", out)

    def test_collect_facts_from_tool_observations(self):
        """从 AgentExecutor 的工具调用记录中抽取商品/优惠券事实"""
        import json
        from a2a_server.copywriting_server import _collect_facts

        class _Action:
            tool = "search_products"

        product_obs = json.dumps({"status": "success", "data": [self.PRODUCT]},
                                 ensure_ascii=False)
        coupon_obs = json.dumps({"status": "success", "data": [
            {"code": "NEW10", "coupon_type": "full_reduction",
             "threshold": 0, "value": 10, "scope": "all"}
        ]}, ensure_ascii=False)

        products, coupons = _collect_facts([
            (_Action(), product_obs),
            (_Action(), coupon_obs),
        ])
        self.assertEqual(len(products), 1)
        self.assertIn("NEW10", coupons)


if __name__ == "__main__":
    unittest.main(verbosity=2)
