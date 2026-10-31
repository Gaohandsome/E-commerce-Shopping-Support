-- =============================================================
-- 电商导购与售后客服系统 —— 数据表初始化
-- 说明：在已有的 travel_rag 数据库中新增电商相关表，
--       采用 CREATE TABLE IF NOT EXISTS，不删除原有旅游数据。
-- 执行方式（在 travel_rag 库下）：
--   mysql -u root -p travel_rag < sql/create_ecommerce_tables.sql
-- =============================================================

-- ===================== 商品表 =====================
CREATE TABLE IF NOT EXISTS products (
    id              INT          NOT NULL AUTO_INCREMENT COMMENT '商品ID',
    name            VARCHAR(200) NOT NULL                COMMENT '商品名称',
    category        VARCHAR(50)  NOT NULL                COMMENT '品类（数码配件/智能家居/美妆个护/食品饮料/服饰鞋包/运动户外）',
    brand           VARCHAR(50)  DEFAULT ''              COMMENT '品牌',
    price           DECIMAL(10,2) NOT NULL               COMMENT '现价（元）',
    original_price  DECIMAL(10,2) DEFAULT NULL           COMMENT '原价（元，可选）',
    stock           INT          DEFAULT 0               COMMENT '库存数量',
    rating          DECIMAL(2,1) DEFAULT 5.0             COMMENT '评分（0.0-5.0）',
    sales_volume    INT          DEFAULT 0               COMMENT '销量',
    description     TEXT                                 COMMENT '商品描述',
    specs           JSON                                 COMMENT '规格参数（JSON）',
    tags            JSON                                 COMMENT '标签（JSON 数组）',
    created_at      DATETIME     DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    INDEX idx_category (category),
    INDEX idx_brand (brand),
    INDEX idx_price (price)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='电商商品表（MySQL 结构化查询）';


-- ===================== 优惠券表 =====================
CREATE TABLE IF NOT EXISTS coupons (
    id           INT          NOT NULL AUTO_INCREMENT COMMENT '优惠券ID',
    code         VARCHAR(50)  NOT NULL                COMMENT '优惠券码（唯一）',
    name         VARCHAR(100) NOT NULL                COMMENT '优惠券名称',
    coupon_type  ENUM('full_reduction','discount') NOT NULL COMMENT '类型：full_reduction=满减，discount=折扣',
    threshold    DECIMAL(10,2) DEFAULT 0              COMMENT '使用门槛（满 threshold 元可用；0 表示无门槛）',
    value        DECIMAL(10,2) NOT NULL               COMMENT '满减金额（元）或折扣率（0.8 表示 8 折）',
    scope        VARCHAR(50)  DEFAULT 'all'           COMMENT '适用范围：all=全场，或具体品类名',
    valid_from   DATE         DEFAULT NULL            COMMENT '生效日期',
    valid_to     DATE         DEFAULT NULL            COMMENT '失效日期',
    total_count  INT          DEFAULT 1000            COMMENT '发放总量',
    used_count   INT          DEFAULT 0               COMMENT '已使用量',
    created_at   DATETIME     DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='优惠券表';


-- ===================== 订单表 =====================
CREATE TABLE IF NOT EXISTS orders (
    id              INT          NOT NULL AUTO_INCREMENT COMMENT '订单ID',
    order_no        VARCHAR(50)  NOT NULL                COMMENT '订单号（唯一）',
    user_id         VARCHAR(50)  DEFAULT 'guest'         COMMENT '用户标识',
    product_id      INT          NOT NULL                COMMENT '商品ID',
    product_name    VARCHAR(200) DEFAULT ''              COMMENT '商品名称快照',
    quantity        INT          NOT NULL DEFAULT 1      COMMENT '购买数量',
    unit_price      DECIMAL(10,2) NOT NULL               COMMENT '下单时单价',
    total_amount    DECIMAL(10,2) NOT NULL               COMMENT '商品总额（unit_price * quantity）',
    coupon_code     VARCHAR(50)  DEFAULT NULL            COMMENT '使用的优惠券码',
    discount_amount DECIMAL(10,2) DEFAULT 0              COMMENT '优惠金额',
    final_amount    DECIMAL(10,2) NOT NULL               COMMENT '实付金额',
    status          ENUM('pending','paid','cancelled') DEFAULT 'paid' COMMENT '订单状态',
    created_at      DATETIME     DEFAULT CURRENT_TIMESTAMP COMMENT '下单时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_order_no (order_no),
    INDEX idx_user_id (user_id),
    INDEX idx_product_id (product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单表（下单时写入，优惠券在此核销）';
