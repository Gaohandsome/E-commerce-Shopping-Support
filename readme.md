# 基于 A2A + MCP 的电商多智能体导购与售后系统（ShopMate 电商版）

> 由原「旅游多智能体」改造而来：保留 A2A 多代理协作与 MCP 工具体系，  
> 业务主体换成 **电商导购 / 比价下单 / 物流查询 / 退换货 / 小红书种草**。

## 1. 总体架构

```
                    ┌──────────────────────────────┐
   浏览器 / 前端 ──▶ │  api_server.py  (8085)       │
                    │  /api/chat  /api/agents ...  │
                    └──────────────┬───────────────┘
                                   │ ChatService
                    意图识别 → 任务规划 → ReAct 执行 → 汇总
                                   │ A2A
 ┌────────────┬────────────┬───────┴────┬────────────┬────────────┐
 │ 导购 5005  │ 订单 5006  │ 物流 5007  │ 售后 5008  │ 种草 5009  │
 └─────┬──────┴─────┬──────┴─────┬──────┴─────┬──────┴─────┬──────┘
       │ MCP        │ MCP        │ MCP        │ MCP        │ MCP + A2A
 ┌─────▼────────────▼────────────▼────────────▼────────────┴──────┐
 │ 商品 8001 │ 推荐 8002 │ 物流 8003 │ 图片 8004 │ 售后 8005        │
 └───────────┴───────────┴───────────┴───────────┴─────────────────┘
       │           │            │            │            │
    MySQL      Milvus        MySQL       360/Bing      MySQL
  (products/  (商品向量)   (logistics)   (商品实拍图)  (退换货规则)
   orders/
   coupons)
```

## 2. 智能体（A2A）

| 智能体                     |   端口 | 职责                                      |
| ----------------------- | ---: | --------------------------------------- |
| RecommendationAssistant | 5005 | 基于 Milvus 向量库的商品／品类语义推荐，承接"推荐、种草素材"     |
| OrderAssistant          | 5006 | 商品比价、优惠券试算/核销、下单（写订单、扣库存）               |
| LogisticsAssistant      | 5007 | 发货 / 在途 / 签收状态查询                        |
| AfterSaleAssistant      | 5008 | 退换货规则判断 + 售后工单创建/查询                     |
| CopywritingAssistant    | 5009 | 小红书开箱测评/种草笔记；内部再调用商品 MCP、导购/订单代理与图片 MCP |

## 3. MCP 工具服务

| 服务     |   端口 | 说明                                  |
| ------ | ---: | ----------------------------------- |
| 商品 MCP | 8001 | 商品检索、同品类比价、商品详情、优惠券查询与试算、下单         |
| 推荐 MCP | 8002 | Qwen Embedding + Milvus 向量搜索（余弦相似度） |
| 物流 MCP | 8003 | 物流轨迹与订单发货状态（本地模拟数据）                 |
| 图片 MCP | 8004 | 商品实拍图检索，返回图片 URL、原图页与授权说明           |
| 售后 MCP | 8005 | 退换货规则引擎、工单创建与查询                     |

> 所有 MCP 只访问本地 MySQL / Milvus / 公开图片检索，不接真实订单与快递系统，课堂演示零风险。

### 图片 MCP 的检索策略

按 `IMAGE_PROVIDER_ORDER` 依次尝试图源，命中即返回：默认 `so360,bing`（无 Key、中文命中率高）；  
有图库 Key 时可用 `unsplash,pexels,pixabay`，能访问开源图库时可用 `openverse,wikimedia`。

检索词三级降级：`品牌+商品名` → `商品名` → `品类通用词`。  
系统会按图片标题与商品的真实关系给出 `match_level`：

| match_level      | 含义      | 文案中的标注           |
| ---------------- | ------- | ---------------- |
| `exact` / `name` | 命中了该型号  | 商品实拍             |
| `brand`          | 同品牌其他型号 | 品牌同系列参考图（非该型号实拍） |
| `generic`        | 只有同类商品  | 同类商品参考图          |

## 4. 数据层

MySQL 库名沿用 `travel_rag`：

| 表                                                         | 说明                                 |
| --------------------------------------------------------- | ---------------------------------- |
| `products`                                                | 商品（SKU、品类、品牌、价格、原价、库存、评分、specs、卖点） |
| `orders`                                                  | 订单（订单号、用户、商品、数量、金额、优惠、状态）          |
| `coupons`                                                 | 优惠券（券码、类型、门槛、面值、适用范围）              |
| `logistics`                                               | 物流节点（订单号、承运商、运单号、状态、时间、描述）         |
| `after_sale_rules`                                        | 按品类的退换货规则（退货/换货天数、是否支持、前置条件）       |
| `after_sale_requests`                                     | 售后工单                               |
| `user_profiles` / `query_history` / `short_term_messages` | 对话记忆                               |

Milvus 集合 `products`（默认库）：`product_id/name/category/brand/price/original_price/stock/rating/sales_volume/description/specs/embedding`。

### 初始化

```bash
# 1) 建表 + 演示商品/优惠券
mysql -uroot -p < sql/create_ecommerce_tables.sql
mysql -uroot -p < sql/demo_product_data.sql
# 2) 物流 / 售后规则 + 演示订单
mysql -uroot -p < sql/create_logistics_aftersale_tables.sql
# 3) 记忆表
mysql -uroot -p < sql/create_memory_tables.sql
# 4) 商品向量库（Qwen Embedding + Milvus）
python sql/init_product_rag.py
```

## 5. 启动服务

前置：MySQL 8.0、Milvus 2.4、可用的 `API_KEY / BASE_URL / LLM_MODEL`。

第一步先准备环境变量（仓库里只提供示例文件，`.env` 已被 `.gitignore` 排除）：

```bash
cp .env.example .env     # Windows: copy .env.example .env
# 然后编辑 .env，填入你自己的 DashScope API Key、MySQL 密码与 Milvus 地址
```

```bash
# 一键启动 11 个进程（5 MCP + 5 A2A + API）
bash run_stack.sh
```

或手动按顺序启动：

```bash
python mcp_server/mcp_product_server.py        # 8001
python mcp_server/mcp_recommendation_server.py # 8002
python mcp_server/mcp_logistics_server.py      # 8003
python mcp_server/mcp_image_server.py          # 8004
python mcp_server/mcp_aftersale_server.py      # 8005

python a2a_server/recommendation_server.py     # 5005
python a2a_server/order_server.py              # 5006
python a2a_server/logistics_server.py          # 5007
python a2a_server/aftersale_server.py          # 5008
python a2a_server/copywriting_server.py        # 5009（依赖商品/图片 MCP 与 5005/5006）

python api_server.py                           # 8085
```

打开 `http://127.0.0.1:8085` 即可对话；`/api/agents` 查看在线状态，`/api/memory/clear` 清空记忆。

## 6. 单元测试

```bash
python -m unittest tests.test_mcp_servers tests.test_agent_servers -v
```

覆盖：格式化工具、商品/订单/物流/售后 MCP 的真实读写、图片 MCP 检索降级策略、  
5 个 Agent Card 定义与实例化。集成测试会写库（下单、建工单），跑完建议重置演示基线。

## 7. 多智能体评估

```bash
python tests/eval_multi_agent.py                 # 全量 10 条用例
python tests/eval_multi_agent.py --limit 3       # 只跑前 3 条
python tests/eval_multi_agent.py --case copywriting_product_note
```

评估集 `tests/eval_multi_agent_dataset.json` 共 10 条用例，覆盖单意图（推荐/比价/下单/物流/售后）、  
复合意图（物流+售后）、种草文案（7 段格式 + 真实商品图 + 价格事实）、边界拒答。

| 指标                                 | 说明                              |
| ---------------------------------- | ------------------------------- |
| Task Success Rate (TSR)            | 关键词、格式、禁用词等约束全部满足的用例比例          |
| Routing Accuracy / Intent Micro-F1 | 意图识别与期望意图的一致性                   |
| Agent Selection Accuracy           | 路由到的顶层代理集合是否与期望一致               |
| Keyword Coverage                   | 答案对必需信息的覆盖程度                    |
| Format Compliance                  | 种草笔记 7 段固定段落是否齐全（含【商品信息】【商品实拍】） |
| Factual Grounding                  | 文案是否引用了真实商品价格（如 ¥299 / 原价 ¥399） |
| Image Embedding Compliance         | 是否输出可渲染的真实图片 Markdown           |
| Error Rate                         | 超时、服务异常等错误比例                    |
| Collaboration Breadth              | 单个请求平均参与的顶层代理数                  |
| Latency                            | 平均 / P95 / 最大端到端延迟              |

详细结果写入 `tests/eval_results.json`。

## 8. 种草文案的防幻觉设计

电商文案最容易"编参数"，本项目做了三道闸门：

1. **提示词铁律**：只允许引用工具返回的 `specs/description`；禁止编造实测数据、跨品牌对比、反向推断缺点。
2. **确定性重写【商品信息】**：从 Agent 的工具调用记录里抽出真实商品 JSON，直接生成价格/原价/评分/库存/规格/券后价，  
   不交给 LLM 复述。
3. **生成后事实校对**：用事实清单对全文做二次校对，删掉无依据的数字、"实测 xx%"、伪造来源与"没有某功能"这类反向断言。

图片同理：`match_level` 决定文案里写"商品实拍"还是"品牌同系列参考图"，不把别家的图谎称成本商品的实拍。

## 9. docker 部署

已提供 `Dockerfile` 与 `docker-compose.yml`（11 个服务）。

```bash
docker compose up -d --build
```
