---
name: "market-sentiment"
description: "监控大盘(如BTC)走势，判断市场整体风险。当需要进行多头交易前调用，若大盘暴跌则发出熔断信号。"
---

# Market Sentiment Skill

此技能用于"看天吃饭"。在对小币种进行操作前，先检查大盘 (BTC/ETH) 的脸色。如果大盘正在跳水，任何小币种的做多信号都应被忽略。

## 功能特性
1. **暴跌熔断**：检测 BTC 1小时内的实时跌幅。如果跌幅超过阈值 (如 -1%)，则判定为"危险"。
2. **趋势确认**：(可选) 结合 4h 趋势判断大环境。

## 使用方法

### 代码调用

```python
from strategy.sentiment import SentimentAnalyzer

sentiment = SentimentAnalyzer()

# crash_threshold: 暴跌阈值 (默认 -0.01 即 -1%)
is_safe, reason = sentiment.check_market_sentiment(symbol='BTCUSDT', crash_threshold=-0.01)

if not is_safe:
    print(f"熔断触发: {reason}")
    # 停止开仓，甚至清仓
else:
    print("大盘环境安全，允许交易")
```

## 参数建议
- **保守型**: 阈值设为 -0.005 (-0.5%)，稍微有点风吹草动就停止。
- **激进型**: 阈值设为 -0.02 (-2%)，除非崩盘否则继续干。
