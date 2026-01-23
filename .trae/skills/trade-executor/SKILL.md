---
name: "trade-executor"
description: "执行交易并挂止盈止损与调仓订单。当确定开仓、平仓或加减仓时调用。"
---

# 交易执行技能

此技能负责把交易信号落地为订单，并确保滑点、风控与仓位调整可控。

## 执行规则
1. 限价优先，避免不必要滑点
2. 下单前检查最小成交额与步进规则
3. 开仓必须同时挂止损与分批止盈
4. 加减仓只在趋势方向未破坏时触发

## 需要的数据
- 盘口价格与价差
- 交易规则：最小数量、步进、最小成交额
- 账户可用余额与仓位信息
- 止盈止损与调仓规则

## 输出结构
必须输出 JSON，字段如下：
- action: OPEN 或 CLOSE 或 INCREASE 或 REDUCE
- order_type: LIMIT 或 MARKET
- quantity: 数值
- price: 数值
- stop_loss: 数值
- take_profit: 数值
- notes: 风控说明

## 提示词模板
请以专业币圈交易员身份给出执行指令，只输出 JSON：
{
  "action": "OPEN/CLOSE/INCREASE/REDUCE",
  "order_type": "LIMIT/MARKET",
  "quantity": "数值",
  "price": "数值",
  "stop_loss": "数值",
  "take_profit": "数值",
  "notes": "风控说明"
}
