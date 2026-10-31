-- ============================================================
-- 阶段2：物流 + 售后 数据表（电商多智能体）
-- 库：travel_rag（沿用旧库名）
-- 说明：全部为本地 mock 数据，不依赖真实快递/订单系统
-- ============================================================

USE travel_rag;

-- ----------------------------
-- 物流表：一行 = 一个物流节点
-- ----------------------------
CREATE TABLE IF NOT EXISTS logistics (
  id INT AUTO_INCREMENT PRIMARY KEY,
  order_no VARCHAR(50) NOT NULL COMMENT '订单号',
  carrier VARCHAR(50) DEFAULT NULL COMMENT '承运商',
  tracking_no VARCHAR(50) DEFAULT NULL COMMENT '运单号',
  status ENUM('pending','shipped','in_transit','out_for_delivery','delivered','exception')
         NOT NULL DEFAULT 'pending' COMMENT '节点状态',
  node_time DATETIME DEFAULT NULL COMMENT '节点时间',
  node_desc VARCHAR(255) DEFAULT NULL COMMENT '节点描述',
  current_city VARCHAR(50) DEFAULT NULL COMMENT '所在城市',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_logistics_order (order_no),
  INDEX idx_logistics_time (node_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='物流轨迹（mock）';

-- ----------------------------
-- 退换货规则表
-- ----------------------------
CREATE TABLE IF NOT EXISTS after_sale_rules (
  id INT AUTO_INCREMENT PRIMARY KEY,
  category VARCHAR(50) NOT NULL UNIQUE COMMENT '商品品类',
  return_window_days INT NOT NULL DEFAULT 7 COMMENT '无理由退货天数',
  exchange_window_days INT NOT NULL DEFAULT 15 COMMENT '换货天数',
  refundable TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否支持退货退款',
  exchangeable TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否支持换货',
  conditions VARCHAR(500) DEFAULT NULL COMMENT '前置条件',
  note VARCHAR(255) DEFAULT NULL COMMENT '备注'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='退换货规则（mock）';

-- ----------------------------
-- 售后工单表
-- ----------------------------
CREATE TABLE IF NOT EXISTS after_sale_requests (
  id INT AUTO_INCREMENT PRIMARY KEY,
  ticket_no VARCHAR(50) NOT NULL UNIQUE COMMENT '工单号',
  order_no VARCHAR(50) NOT NULL COMMENT '关联订单号',
  product_id INT DEFAULT NULL,
  product_name VARCHAR(200) DEFAULT NULL,
  request_type ENUM('return','exchange','refund') NOT NULL COMMENT '售后类型',
  reason VARCHAR(255) DEFAULT NULL COMMENT '售后原因',
  status ENUM('created','processing','approved','rejected','completed')
         NOT NULL DEFAULT 'created' COMMENT '工单状态',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_as_order (order_no),
  INDEX idx_as_ticket (ticket_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='售后工单（mock）';

-- ============================================================
-- 演示数据（可重复导入）
-- ============================================================

-- 演示订单（均为 paid；发货状态由 logistics 表表达）
INSERT IGNORE INTO orders
  (order_no, user_id, product_id, product_name, quantity, unit_price,
   total_amount, coupon_code, discount_amount, final_amount, status, created_at)
VALUES
  ('EC20261030001','guest',25,'优衣库 基础款纯棉T恤',1,79.00,79.00,NULL,0.00,79.00,'paid','2026-10-30 09:12:00'),
  ('EC20261030002','guest',5,'倍思 20000mAh 快充充电宝',1,129.00,129.00,'NEW10',10.00,119.00,'paid','2026-10-29 20:05:00'),
  ('EC20261030003','guest',14,'美苏 5L 大容量空气炸锅',1,299.00,299.00,'FULL200',30.00,269.00,'paid','2026-10-28 15:20:00'),
  ('EC20261030004','guest',27,'Nike 飞马跑步鞋',1,799.00,799.00,NULL,0.00,799.00,'paid','2026-10-30 10:40:00');

-- 物流轨迹
DELETE FROM logistics WHERE order_no IN ('EC20261030001','EC20261030002','EC20261030003','EC20261030004');
INSERT INTO logistics (order_no, carrier, tracking_no, status, node_time, node_desc, current_city) VALUES
  -- 订单1：待发货（用于"衣服发货没"→ 未发货 → 退货 的复合场景）
  ('EC20261030001', NULL, NULL, 'pending', '2026-10-30 09:15:00', '订单已支付，仓库备货中', '上海'),
  -- 订单2：运输中
  ('EC20261030002', '顺丰速运', 'SF1402938475621', 'shipped', '2026-10-29 20:30:00', '商家已发货，顺丰速运已揽收', '深圳'),
  ('EC20261030002', '顺丰速运', 'SF1402938475621', 'in_transit', '2026-10-30 03:10:00', '快件已到达【深圳宝安集散中心】', '深圳'),
  ('EC20261030002', '顺丰速运', 'SF1402938475621', 'in_transit', '2026-10-30 19:40:00', '快件已离开【深圳宝安集散中心】，发往【杭州转运中心】', '深圳'),
  -- 订单3：已签收
  ('EC20261030003', '中通快递', 'ZT8827364510', 'shipped', '2026-10-28 16:00:00', '商家已发货，中通快递已揽收', '上海'),
  ('EC20261030003', '中通快递', 'ZT8827364510', 'in_transit', '2026-10-29 08:20:00', '快件已到达【上海转运中心】', '上海'),
  ('EC20261030003', '中通快递', 'ZT8827364510', 'out_for_delivery', '2026-10-30 09:05:00', '快件派送中，快递员：王师傅 138****2233', '上海'),
  ('EC20261030003', '中通快递', 'ZT8827364510', 'delivered', '2026-10-30 14:32:00', '已签收，签收人：本人', '上海'),
  -- 订单4：派送中
  ('EC20261030004', '京东物流', 'JD0039284756', 'shipped', '2026-10-30 11:00:00', '商家已发货，京东物流已揽收', '北京'),
  ('EC20261030004', '京东物流', 'JD0039284756', 'out_for_delivery', '2026-10-31 06:50:00', '快件派送中，预计今日送达', '北京');

-- 退换货规则（按品类）
INSERT INTO after_sale_rules
  (category, return_window_days, exchange_window_days, refundable, exchangeable, conditions, note)
VALUES
  ('数码配件', 7, 15, 1, 1, '商品未激活、配件与包装完好、不影响二次销售', '已激活的手机/耳机仅支持质量问题退换'),
  ('智能家居', 7, 15, 1, 1, '包装完好、不影响二次销售', '大件商品需自行寄回或预约上门取件'),
  ('服饰鞋包', 7, 15, 1, 1, '吊牌完好、未洗涤、无异味', '贴身衣物拆封后不支持无理由退货'),
  ('美妆个护', 7, 0, 1, 0, '仅未拆封可退，拆封后不支持无理由退货', '化妆品拆封即视为使用，不支持换货'),
  ('食品饮料', 0, 0, 0, 0, '不支持无理由退换', '仅质量问题（如变质、临期）可申请退款'),
  ('运动户外', 7, 15, 1, 1, '不影响二次销售、配件齐全', '帐篷等使用后需无污损')
ON DUPLICATE KEY UPDATE
  return_window_days=VALUES(return_window_days),
  exchange_window_days=VALUES(exchange_window_days),
  refundable=VALUES(refundable),
  exchangeable=VALUES(exchangeable),
  conditions=VALUES(conditions),
  note=VALUES(note);
