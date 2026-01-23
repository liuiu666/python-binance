---
name: "ai-strategy"
description: "用大模型结合趋势与资金面给出交易决策与调仓建议。当需要智能决策替代规则时调用。"
---

# 智能策略技能

此技能使用大模型综合趋势、资金与量价信息，输出清晰的交易决策与持仓规则。

## 决策重点
1. 趋势一致性高于震荡指标
2. 资金流向与持仓量必须支持趋势
3. 资金费率过高视为风险
4. 只给出可执行的止盈止损与调仓规则

## 需要的数据
- 5m、1h 与 4h 的 MA20/50/200、ATR、成交量与均量
- 资金流向、资金费率、持仓量变化
- 盘口价差与流动性
- 当前价格与关键支撑压力位

## 输出结构
必须输出 JSON，字段如下：
- signal: BUY 或 SELL 或 HOLD
- confidence: 0-100
- stop_loss: 数值
- take_profit: 数值
- add_rule: 加仓条件
- reduce_rule: 减仓条件
- hold_rule: 持仓与离场规则
- reason: 一句话理由

## 提示词模板
请以专业币圈交易员身份输出，严格 JSON：
{
  "signal": "BUY/SELL/HOLD",
  "confidence": 0-100,
  "stop_loss": "数值",
  "take_profit": "数值",
  "add_rule": "浮盈达到 2.5ATR 且趋势未破位时加仓 30%",
  "reduce_rule": "浮盈达到 4ATR 或资金流向反转时减仓 30%",
  "hold_rule": "价格跌破 MA20 且 MA20 下拐或 MA50/MA200 趋势反转则离场",
  "reason": "一句话理由"
}
