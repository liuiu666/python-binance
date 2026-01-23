---
name: "strategy-maker"
description: "根据趋势数据生成入场信号与止盈止损、调仓规则。当趋势已确认后调用。"
---

# 趋势策略生成技能

此技能负责把趋势判断转化为可执行的入场、止损止盈、加减仓与持仓规则。

## 策略框架
1. 趋势过滤：4h 定方向，1h 执行，5m 做入场节奏
2. 入场触发：趋势内回踩 MA20 后 5m 重回均线
3. 量能确认：成交量不低于均量
4. 风控：ATR 止损与分批止盈
5. 调仓：盈利扩展时加仓，趋势走弱时减仓

## 需要的数据
- 4h、1h 与 5m 的 MA20/50/200 与收盘价
- ATR、成交量与均量
- 资金费率、持仓量变化、资金流向

## 输出结构
必须输出 JSON，字段如下：
- signal: BUY 或 SELL 或 NONE
- stop_loss: 数值
- take_profit: 数值
- reason: 一句话理由
- add_rule: 加仓条件
- reduce_rule: 减仓条件
- hold_rule: 持仓与离场规则

## 提示词模板
请以专业币圈交易员身份输出趋势策略，只输出 JSON：
{
  "signal": "BUY/SELL/NONE",
  "stop_loss": "数值",
  "take_profit": "数值",
  "reason": "一句话理由",
  "add_rule": "浮盈达到 2.5ATR 且趋势未破位时加仓 30%",
  "reduce_rule": "浮盈达到 4ATR 或趋势动能衰减时减仓 30%",
  "hold_rule": "价格跌破 MA20 且 MA20 下拐或资金流向反转则离场"
}
