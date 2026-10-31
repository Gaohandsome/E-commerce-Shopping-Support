-- ==================== 记忆层数据表（对话记忆 / 用户偏好持久化）====================
-- 由 memory.py (ConversationMemory) 使用；与电商业务无直接耦合，但系统是“有记忆的助手”所必需的。
-- 使用 CREATE TABLE IF NOT EXISTS，可重复执行，不会覆盖已有数据。
-- 数据库名沿用项目约定：travel_rag

-- ==================== 用户偏好表 ====================
CREATE TABLE IF NOT EXISTS user_profiles (
    profile_key VARCHAR(50) NOT NULL COMMENT '偏好键名',
    profile_value VARCHAR(200) NOT NULL COMMENT '偏好值',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (profile_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户偏好';

-- ==================== 查询历史表 ====================
CREATE TABLE IF NOT EXISTS query_history (
    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键',
    intent_type VARCHAR(30) NOT NULL COMMENT '意图类型：recommend/order/logistics/after_sale/copywriting等',
    query_content TEXT NOT NULL COMMENT '查询内容JSON',
    query_time DATETIME NOT NULL COMMENT '查询时间',
    INDEX idx_time (query_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='查询历史';

-- ==================== 短期对话表 ====================
CREATE TABLE IF NOT EXISTS short_term_messages (
    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键',
    role VARCHAR(10) NOT NULL COMMENT '消息角色：user 或 assistant',
    content TEXT NOT NULL COMMENT '消息内容',
    message_time VARCHAR(10) NOT NULL COMMENT '时间戳 HH:MM:SS',
    message_order INT NOT NULL COMMENT '消息顺序号',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='短期对话';
