---
name: "market-analysis"
description: "对特定币种进行深度市场分析，包括 K 线形态、技术指标 (MA, RSI, ATR) 和资金流向。当选定目标币种后调用。"
---

# Market Analysis Skill

此技能用于对单一币种进行全方位的技术面和资金面分析，为交易决策提供数据支持。

## 功能特性
1. **技术指标计算**：自动计算 MA (均线), RSI (强弱指标), MACD, ATR (波动率)。
2. **资金流向分析**：分析 1小时/4小时 的主动买入卖出量，判断主力意图。
3. **K 线数据获取**：获取最近 100 根 K 线数据。

## 使用方法

### 代码调用

```python
from handlers.binance_client import BinanceClient
from strategy.analysis import MarketAnalyzer

client = BinanceClient()
analyzer = MarketAnalyzer()
symbol = 'BTCUSDT'

# 1. 获取 K 线数据
df = client.get_klines(symbol, '1h', limit=100)

# 2. 计算技术指标
df_indicators = analyzer.calculate_indicators(df)
print(df_indicators.tail())

# 3. 获取资金流向
flow_data = client.get_money_flow(symbol, '1h')
flow_analysis = analyzer.analyze_money_flow(flow_data)
print(flow_analysis)
```

## 输出解读
- **ATR**: 用于设定止损位 (通常 2倍 ATR)。
- **RSI**: >70 超买 (可能回调), <30 超卖 (可能反弹)。
- **资金流向**: "净流入 > 0" 且 "买卖比 > 1" 通常表示多头强势。
