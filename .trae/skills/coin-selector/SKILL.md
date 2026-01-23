---
name: "coin-selector"
description: "扫描加密货币市场，根据波动率、成交量和买卖价差筛选潜在交易标的。当用户想要寻找交易机会、筛选币种或查看市场波动榜时调用。"
---

# Coin Selector Skill

此技能用于从 Binance 市场中筛选出符合高波动策略的潜在交易对。

## 功能特性
1. **波动率扫描**：筛选 24小时振幅最大的币种。
2. **流动性过滤**：默认过滤 24小时成交额 < 1000万 USDT 的币种。
3. **风险风控**：过滤盘口价差 (Spread) > 0.5% 的币种，防止滑点风险。

## 使用方法

### 方式 1: 直接运行扫描脚本 (推荐)
在终端运行以下命令，查看当前市场波动榜：

```bash
python strategy/scanner.py
```

### 方式 2: 代码调用
在 Python 代码中调用 `MarketScanner` 类：

```python
from strategy.scanner import MarketScanner

scanner = MarketScanner()
# min_volume: 最小成交额 (默认 1000万)
# max_spread: 最大价差率 (默认 0.5%)
# top_n: 返回数量
top_coins = scanner.scan_market(min_volume=10000000, max_spread=0.005, top_n=5)

if top_coins is not None and not top_coins.empty:
    best_coin = top_coins.iloc[0]['symbol']
    print(f"选中币种: {best_coin}")
```

## 输出解读
返回的 DataFrame 包含以下关键字段：
- `symbol`: 交易对名称
- `change_pct`: 24h 涨跌幅
- `amplitude_pct`: 24h 振幅 (波动率核心指标)
- `spread_pct`: 盘口价差率 (越低流动性越好)
