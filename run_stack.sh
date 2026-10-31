#!/bin/bash
# 电商多智能体本地启动脚本（阶段1+2+3）
# MCP: product 8001, recommend 8002, logistics 8003, image 8004, aftersale 8005
# A2A: recommend 5005, order 5006, logistics 5007, aftersale 5008, copywriting 5009
# API: 8085
cd "$(dirname "$0")" || exit 1
# 优先用项目内虚拟环境，找不到再退回系统 python
if [ -x ".venv/Scripts/python.exe" ]; then
  PY=".venv/Scripts/python.exe"   # Windows (Git Bash)
elif [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"           # macOS / Linux
else
  PY="python3"
fi
mkdir -p logs

echo "[stack] starting MCP services" > logs/stack.log
$PY mcp_server/mcp_product_server.py        > logs/mcp_product.out 2>&1 &      P1=$!
$PY mcp_server/mcp_recommendation_server.py > logs/mcp_recommend.out 2>&1 &    P2=$!
$PY mcp_server/mcp_logistics_server.py      > logs/mcp_logistics.out 2>&1 &    P3=$!
$PY mcp_server/mcp_image_server.py          > logs/mcp_image.out 2>&1 &        P4=$!
$PY mcp_server/mcp_aftersale_server.py      > logs/mcp_aftersale.out 2>&1 &    P5=$!

sleep 4

echo "[stack] starting A2A agents" >> logs/stack.log
$PY a2a_server/recommendation_server.py     > logs/a2a_recommend.out 2>&1 &    P6=$!
$PY a2a_server/order_server.py              > logs/a2a_order.out 2>&1 &        P7=$!
$PY a2a_server/logistics_server.py          > logs/a2a_logistics.out 2>&1 &    P8=$!
$PY a2a_server/aftersale_server.py          > logs/a2a_aftersale.out 2>&1 &    P9=$!

# 文案代理要调用导购/订单/图片，按文档要求最后启动（等下游就绪）
sleep 3
$PY a2a_server/copywriting_server.py        > logs/a2a_copywriting.out 2>&1 &  P10=$!

sleep 3

echo "[stack] starting API" >> logs/stack.log
$PY api_server.py                           > logs/api_server.out 2>&1 &      P11=$!

echo "[stack] all launched: $P1 $P2 $P3 $P4 $P5 $P6 $P7 $P8 $P9 $P10 $P11" >> logs/stack.log
wait $P1 $P2 $P3 $P4 $P5 $P6 $P7 $P8 $P9 $P10 $P11
