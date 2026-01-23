---
name: "coin-selector"
description: "扫描市场筛选趋势币，结合趋势强度与流动性排序。当需要生成趋势候选池时调用。"
---

# 趋势选币技能

此技能用于从市场中筛选趋势明确且流动性合格的币种，作为趋势交易候选池。

## 选币原则
1. 趋势优先：4h 与 1h 同向，5m 只做节奏确认
2. 流动性合格：24h 成交额、盘口价差
3. 波动适中：过度暴涨暴跌的币种降低优先级
4. 资金支持：净流入与持仓量增长

## 需要的数据
- 5m、1h 与 4h 的 MA20/50/200 与收盘价
- 24h 成交额、振幅、涨跌幅
- 盘口价差
- 资金费率、持仓量变化、主动买卖量

## 输出结构
必须输出 JSON，字段如下：
- candidates: 列表，包含 symbol、trend_bias、trend_strength、liquidity_score、risk_note
- summary: 简短结论

## 提示词模板
请以专业币圈交易员身份筛选趋势币并按优先级排序，只输出 JSON：
{
  "candidates": [
    {"symbol": "XXXUSDT", "trend_bias": "BUY_ONLY/SELL_ONLY", "trend_strength": 0-100, "liquidity_score": 0-100, "risk_note": "一句话风险", "timing_bias": "BUY/SELL/NONE"},
    {"symbol": "YYYUSDT", "trend_bias": "BUY_ONLY/SELL_ONLY", "trend_strength": 0-100, "liquidity_score": 0-100, "risk_note": "一句话风险", "timing_bias": "BUY/SELL/NONE"}
  ],
  "summary": "简短结论"
}
