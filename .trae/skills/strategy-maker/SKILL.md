---
name: "strategy-maker"
description: "根据市场数据制定具体的交易策略，生成买卖信号和止盈止损点位。当完成市场分析后调用。"
---

# Strategy Maker Skill

此技能是交易系统的"大脑"，负责根据技术指标和价格行为 (Price Action) 生成明确的交易信号。

## 功能特性
1. **量价突破策略**：专为小币种设计，检测放量突破高点/低点的行为。
2. **动态风控**：基于 ATR (平均真实波幅) 计算止损位，防止被市场噪音扫损。
3. **盈亏比管理**：默认设置 1.5:1 的盈亏比 (止盈 3ATR, 止损 2ATR)。

## 使用方法

### 代码调用

```python
from strategy.analysis import MarketAnalyzer

analyzer = MarketAnalyzer()

# df: 包含 K 线和技术指标的 DataFrame (由 Market Analysis Skill 生成)
# lookback: 突破周期 (如 20)
# vol_multiplier: 放量倍数 (如 1.5 倍均量)

signal, info = analyzer.check_breakout_strategy(df, lookback=20, vol_multiplier=1.5)

if signal:
    print(f"策略信号: {signal}")
    print(f"开仓理由: {info['reason']}")
    print(f"建议止损: {info['stop_loss']}")
    print(f"建议止盈: {info['take_profit']}")
else:
    print("当前无交易机会")
```

## 策略逻辑详解
- **买入条件**：收盘价 > 过去 20 根 K 线最高价 AND 成交量 > 20 均量 * 1.5
- **卖出条件**：收盘价 < 过去 20 根 K 线最低价 AND 成交量 > 20 均量 * 1.5
- **止损计算**：买入价 - 2 * ATR
