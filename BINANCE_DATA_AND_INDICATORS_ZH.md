# 币安数据采集与指标计算指南

本文档聚焦三个问题：
1. 做加密货币短周期预测，需要哪些数据
2. 币安能提供哪些数据
3. 指标怎么计算、怎么放进提示词

---

## 一、做预测需要哪些数据

短周期（如未来 5-30 分钟）方向判断，数据重要性从高到低：

| 优先级 | 数据类别 | 为什么需要 | 对应变量示例 |
|---|---|---|---|
| P0 | K 线 OHLCV | 所有技术指标的计算基础 | `{BTC_klines_5m}` |
| P0 | 主动买卖成交（订单流） | 短周期方向的核心驱动力 | `{BTC_CVD_5m}` `{BTC_TAKER_5m}` |
| P0 | 持仓量 OI 变化 | 判断资金流入流出、趋势可信度 | `{BTC_OI_DELTA_5m}` |
| P1 | 盘口深度 / 失衡 | 即时买卖压力 | `{BTC_DEPTH_5m}` `{BTC_IMBALANCE_5m}` |
| P1 | RSI / MACD | 动量和超买超卖 | `{BTC_RSI14_5m}` `{BTC_MACD_5m}` |
| P1 | 波动率 ATR | 仓位 sizing 和止损距离 | `{BTC_ATR14_5m}` |
| P2 | 资金费率 | 拥挤度反向信号 | `{BTC_FUNDING_5m}` |
| P2 | 均线 / 布林带 | 趋势背景和支撑阻力 | `{BTC_EMA_5m}` `{BTC_BOLL_5m}` |
| P2 | VWAP / OBV | 价值基准和量价确认 | `{BTC_VWAP_5m}` `{BTC_OBV_5m}` |
| P3 | 新闻情绪 | 仅作背景参考 | `{BTC_news_sentiment}` |
| P3 | 账户 / 持仓 | 风控和仓位管理 | `{positions_detail}` `{available_balance}` |

> 核心原则：短周期预测中，订单流 > 价格动量 > OI > 技术指标 > 新闻。新闻和长周期均线只提供背景。

---

## 二、币安能提供什么数据

项目已对接的全部币安 API 端点：

### 2.1 行情与市场数据（无需签名）

| # | API 端点 | 数据 | 写入数据库 | 采集频率 |
|---|---|---|---|---|
| 1 | `/fapi/v1/klines` | K线 OHLCV + 主动买卖量 | `crypto_klines` + `market_trades_aggregated` | 60 秒 |
| 2 | `/fapi/v1/ticker/24hr` | 24h 涨跌幅、成交量 | 否（实时缓存） | 按需 |
| 3 | `/fapi/v1/ticker/price` | 最新价格 | 否（价格缓存） | 按需 |
| 4 | `/fapi/v1/depth` | 订单簿深度（前 10 档） | `market_orderbook_snapshots` | 15 秒 |
| 5 | `/fapi/v1/openInterest` | 实时持仓量 | `market_asset_metrics` | 60 秒 |
| 6 | `/futures/data/openInterestHist` | 历史 OI（5m 粒度） | 已禁用（粒度不匹配） | - |
| 7 | `/fapi/v1/premiumIndex` | 标记价格 + 资金费率 | `market_asset_metrics` | 60 秒 |
| 8 | `/fapi/v1/fundingRate` | 历史资金费率（365 天） | `market_asset_metrics` | 回补时 |
| 9 | `/futures/data/topLongShortPositionRatio` | 多空账户比 | `market_sentiment_metrics` | 300 秒 |
| 10 | `wss://fstream.binance.com/ws` @aggTrade | 逐笔聚合成交（WS 实时） | `market_trades_aggregated` | 实时（15 秒聚合） |
| 11 | `/fapi/v1/exchangeInfo` | 交易品种元数据 | `system_configs` | 启动时 |

### 2.2 账户与交易数据（需要 HMAC 签名）

| # | API 端点 | 数据 | 写入数据库 |
|---|---|---|---|
| 12 | `/fapi/v3/account` | 余额、保证金、未实现盈亏 | 否 |
| 13 | `/fapi/v3/positionRisk` | 持仓方向、数量、开仓价、强平价 | 否 |
| 14 | `/fapi/v1/leverageBracket` | 杠杆档位（最大杠杆） | 否 |
| 15 | `/fapi/v1/openOrders` | 当前挂单 | 否 |
| 16 | `/fapi/v1/openAlgoOrders` | 条件单（TP/SL） | 否 |
| 17 | `/fapi/v1/userTrades` | 历史成交 | 否 |
| 18 | `/fapi/v1/income` | 收益明细（已实现盈亏、手续费、资金费） | 否 |

### 2.3 K 线支持的周期

```text
1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 8h, 12h, 1d, 3d, 1w, 1M
```

- 单次最多 1500 根。
- 提示词生成时固定拉取 500 根用于计算，保证长周期指标稳定。
- 项目没有原生 `10m` 周期。预测 10 分钟方向建议组合 `1m` + `5m` + `15m`。

### 2.4 K 线每根包含的字段

从 `/fapi/v1/klines` 原始数组解析（[binance_adapter.py:108-147](file:///e:/量化/Hyper-Alpha-Arena/backend/services/exchanges/binance_adapter.py#L108-L147)）：

| 索引 | 字段 | 说明 |
|---|---|---|
| [0] | openTime | 开盘时间（转秒） |
| [1-4] | open / high / low / close | OHLC 价格 |
| [5] | volume | 基础货币成交量（如 BTC 数量） |
| [7] | quoteVolume | 计价货币成交额（USDT） |
| [8] | trades | 成交笔数 |
| [9] | takerBuyBase | 主动买入量（基础货币） |
| [10] | takerBuyQuote | 主动买入额（USDT） |
| 推导 | takerSell = volume - takerBuyBase | 主动卖出量 |

### 2.5 数据库表一览

| 表名 | 存储内容 | 数据来源 |
|---|---|---|
| `crypto_klines` | K 线 OHLCV | `/fapi/v1/klines` |
| `market_trades_aggregated` | 15 秒聚合的主动买卖成交 | WS aggTrade + K 线备份 |
| `market_orderbook_snapshots` | 订单簿深度快照 | `/fapi/v1/depth` |
| `market_asset_metrics` | OI + 资金费率 + 标记价格 | 多个 REST 端点 |
| `market_sentiment_metrics` | 多空账户比 | `/futures/data/topLongShortPositionRatio` |

---

## 三、指标怎么计算、怎么放进提示词

### 3.1 原理：按需加载

系统不会把所有指标都塞给模型。它扫描模板中的 `{变量名}`，只计算出现的指标。

例如模板中写了：

```text
=== TECHNICAL ANALYSIS ===
{BTC_RSI14_5m}
{BTC_MACD_5m}
{BTC_CVD_5m}
```

后端会：
1. 正则匹配出 `(BTC, 5m, [RSI14, MACD])` 和 `(BTC, 5m, [CVD])`。
2. 对 `(BTC, 5m)` 只拉取一次 K 线（500 根）。
3. 从同一批 K 线计算 RSI14 和 MACD。
4. 从数据库读取 5m 窗口的订单流数据计算 CVD。
5. 格式化成文本后替换到模板中。

对应代码：[变量解析](file:///e:/量化/Hyper-Alpha-Arena/backend/services/ai_decision_service.py#L2717-L2833)、[数据加载](file:///e:/量化/Hyper-Alpha-Arena/backend/services/ai_decision_service.py#L3421-L3569)。

### 3.2 变量命名规则

```text
{SYMBOL_指标名_周期}
```

| 变量类型 | 写法 | 示例 |
|---|---|---|
| K 线 | `{SYMBOL_klines_PERIOD}(数量)` | `{BTC_klines_5m}(100)` |
| 技术指标 | `{SYMBOL_指标_PERIOD}` | `{BTC_RSI14_5m}` |
| 订单流 | `{SYMBOL_流量指标_PERIOD}` | `{BTC_CVD_5m}` |
| 行情快照 | `{SYMBOL_market_data}` | `{BTC_market_data}` |
| 市场状态 | `{SYMBOL_market_regime_PERIOD}` | `{BTC_market_regime_5m}` |

> `SYMBOL` 必须替换为实际币种（BTC、ETH、SOL），不能留 `SYMBOL` 占位符。

K 线数量写法：`{BTC_klines_5m}(100)` 表示只向模型展示最近 100 根；不写数量默认展示 500 根。

---

## 四、传统技术指标详解

所有指标通过 `pandas-ta` 计算，代码在 [technical_indicators.py](file:///e:/量化/Hyper-Alpha-Arena/backend/services/technical_indicators.py)。

### 4.1 RSI（相对强弱指数）

**变量**：`{BTC_RSI14_5m}` 或 `{BTC_RSI7_1m}`

**公式**：

```text
RS = 平均上涨幅度 / 平均下跌幅度
RSI = 100 - 100 / (1 + RS)
```

**pandas-ta 调用**：`ta.rsi(close, length=14)`

**最少 K 线**：14 根（RSI14）或 7 根（RSI7）

**放进提示词的文本**：

```text
RSI14: 72.35 (Overbought)
RSI14 last 5: 58.10, 61.40, 65.20, 69.80, 72.35
```

解释规则：`>70` 超买，`<30` 超卖，其他中性。

**代码**：[计算](file:///e:/量化/Hyper-Alpha-Arena/backend/services/technical_indicators.py#L123-L126)、[格式化](file:///e:/量化/Hyper-Alpha-Arena/backend/services/ai_decision_service.py#L2961-L2982)

---

### 4.2 MACD

**变量**：`{BTC_MACD_5m}`

**公式**：

```text
MACD Line = EMA(12) - EMA(26)
Signal Line = EMA(9) of MACD Line
Histogram = MACD Line - Signal Line
```

**pandas-ta 调用**：`ta.macd(close)`（默认 12/26/9）

**最少 K 线**：35 根

**放进提示词的文本**：

```text
MACD Line: 125.4300
Signal Line: 98.2100
Histogram: 27.2200 (Bullish momentum)
Histogram last 5: -10.5000, 5.2000, 15.8000, 22.1000, 27.2200
```

解释规则：柱状图 `>0` 看多，`<=0` 看空。

**代码**：[计算](file:///e:/量化/Hyper-Alpha-Arena/backend/services/technical_indicators.py#L105-L120)、[格式化](file:///e:/量化/Hyper-Alpha-Arena/backend/services/ai_decision_service.py#L2984-L3007)

---

### 4.3 EMA（指数移动平均）

**变量**：`{BTC_EMA_5m}`（自动展开 EMA20/50/100）或单独 `{BTC_EMA20_5m}`

**公式**：

```text
EMA_t = α × Close_t + (1 - α) × EMA_(t-1)
α = 2 / (period + 1)
```

**pandas-ta 调用**：`ta.ema(close, length=20)`

**最少 K 线**：20 / 50 / 100 根

**放进提示词的文本**：

```text
EMA20: 94521.33
EMA20 last 5: 94100.12, 94250.50, 94380.77, 94450.00, 94521.33
```

**代码**：[计算](file:///e:/量化/Hyper-Alpha-Arena/backend/services/technical_indicators.py#L89-L94)、[格式化](file:///e:/量化/Hyper-Alpha-Arena/backend/services/ai_decision_service.py#L3009-L3022)

---

### 4.4 MA（简单移动平均）

**变量**：`{BTC_MA_5m}`（自动展开 MA5/10/20）

**公式**：

```text
MA(n) = 最近 n 根收盘价之和 / n
```

**pandas-ta 调用**：`ta.sma(close, length=20)`

**最少 K 线**：5 / 10 / 20 根

**放进提示词的文本**：

```text
MA20: 94010.55
MA20 last 5: 93800.00, 93850.30, 93920.10, 93960.45, 94010.55
```

**代码**：[计算](file:///e:/量化/Hyper-Alpha-Arena/backend/services/technical_indicators.py#L97-L102)

---

### 4.5 BOLL（布林带）

**变量**：`{BTC_BOLL_5m}`

**公式**：

```text
Middle = MA(20)
Upper  = MA(20) + 2 × 标准差
Lower  = MA(20) - 2 × 标准差
Width  = Upper - Lower
```

**pandas-ta 调用**：`ta.bbands(close, length=20, std=2)`

**最少 K 线**：20 根

**放进提示词的文本**：

```text
Upper Band: 95200.50
Middle Band: 94100.25
Lower Band: 93000.00
Band Width: 2200.50
```

**代码**：[计算](file:///e:/量化/Hyper-Alpha-Arena/backend/services/technical_indicators.py#L129-L190)、[格式化](file:///e:/量化/Hyper-Alpha-Arena/backend/services/ai_decision_service.py#L3024-L3039)

---

### 4.6 ATR（平均真实波幅）

**变量**：`{BTC_ATR14_5m}`

**公式**：

```text
TR = max(
  High - Low,
  abs(High - PreviousClose),
  abs(Low - PreviousClose)
)
ATR = TR 的 14 期平滑平均
```

**pandas-ta 调用**：`ta.atr(high, low, close, length=14)`

**最少 K 线**：14 根

**放进提示词的文本**：

```text
ATR14: 850.25 (High volatility)
20-period average: 620.10
```

解释规则：当前 ATR `>` 近 20 根均值 `× 1.2` 为高波动，否则为正常波动。

**代码**：[计算](file:///e:/量化/Hyper-Alpha-Arena/backend/services/technical_indicators.py#L193-L198)、[格式化](file:///e:/量化/Hyper-Alpha-Arena/backend/services/ai_decision_service.py#L3041-L3056)

---

### 4.7 VWAP（成交量加权平均价）

**变量**：`{BTC_VWAP_5m}`

**公式**：

```text
Typical Price = (High + Low + Close) / 3
VWAP = Σ(Typical Price × Volume) / ΣVolume
```

**pandas-ta 调用**：`ta.vwap(high, low, close, volume)`

**最少 K 线**：1 根（但越长越有意义）

**放进提示词的文本**：

```text
VWAP: 94350.88
VWAP last 5: 94120.00, 94200.50, 94280.30, 94310.00, 94350.88
Note: Price above VWAP suggests bullish sentiment, below suggests bearish
```

**代码**：[计算](file:///e:/量化/Hyper-Alpha-Arena/backend/services/technical_indicators.py#L201-L215)、[格式化](file:///e:/量化/Hyper-Alpha-Arena/backend/services/ai_decision_service.py#L3085-L3099)

---

### 4.8 STOCH（随机震荡指标）

**变量**：`{BTC_STOCH_5m}`

**pandas-ta 调用**：`ta.stoch(high, low, close, k=14, d=3)`

**最少 K 线**：14 根

**放进提示词的文本**：

```text
%K Line: 85.40 (Overbought)
%D Line: 78.20
%K last 5: 60.10, 68.50, 72.30, 80.00, 85.40
```

解释规则：`%K >80` 超买，`<20` 超卖。

**代码**：[计算](file:///e:/量化/Hyper-Alpha-Arena/backend/services/technical_indicators.py#L218-L231)、[格式化](file:///e:/量化/Hyper-Alpha-Arena/backend/services/ai_decision_service.py#L3058-L3083)

---

### 4.9 OBV（能量潮）

**变量**：`{BTC_OBV_5m}`

**公式**：

```text
收盘上涨：OBV += Volume
收盘下跌：OBV -= Volume
收盘持平：OBV 不变
```

**pandas-ta 调用**：`ta.obv(close, volume)`

**最少 K 线**：1 根

**放进提示词的文本**：

```text
OBV: 1250000 (Rising)
OBV last 5: 1100000, 1180000, 1210000, 1230000, 1250000
```

**代码**：[计算](file:///e:/量化/Hyper-Alpha-Arena/backend/services/technical_indicators.py#L234-L239)、[格式化](file:///e:/量化/Hyper-Alpha-Arena/backend/services/ai_decision_service.py#L3101-L3120)

---

### 传统技术指标速查表

| 指标 | 变量写法 | pandas-ta | 最少K线 | 核心含义 |
|---|---|---|---|---|
| RSI7/14 | `{BTC_RSI14_5m}` | `ta.rsi(close, 14)` | 14 | 超买超卖 |
| MACD | `{BTC_MACD_5m}` | `ta.macd(close)` | 35 | 动量方向 |
| EMA20/50/100 | `{BTC_EMA_5m}` | `ta.ema(close, 20)` | 20 | 趋势基准 |
| MA5/10/20 | `{BTC_MA_5m}` | `ta.sma(close, 20)` | 20 | 趋势基准 |
| BOLL | `{BTC_BOLL_5m}` | `ta.bbands(close, 20, 2)` | 20 | 波动通道 |
| ATR14 | `{BTC_ATR14_5m}` | `ta.atr(h, l, c, 14)` | 14 | 波动率 |
| VWAP | `{BTC_VWAP_5m}` | `ta.vwap(h, l, c, v)` | 1 | 价值基准 |
| STOCH | `{BTC_STOCH_5m}` | `ta.stoch(h, l, c, 14, 3)` | 14 | 超买超卖 |
| OBV | `{BTC_OBV_5m}` | `ta.obv(c, v)` | 1 | 量价趋势 |

---

## 五、订单流指标详解

订单流数据来自 WebSocket 实时采集的 `@aggTrade`（15 秒聚合），存储在 `market_trades_aggregated` 表。计算代码在 [market_flow_indicators.py](file:///e:/量化/Hyper-Alpha-Arena/backend/services/market_flow_indicators.py)。

所有订单流指标共用同一套查询逻辑：
- 回溯最近 10 个周期（桶）。
- "当前值"取最近一个桶。
- "last 5"取最近 5 个桶。

### 5.1 CVD（累计成交量差）

**变量**：`{BTC_CVD_5m}`

**公式**：

```text
每桶 delta = 主动买入额 - 主动卖出额
CVD = Σ delta（累计求和）
```

**放进提示词的文本**：

```text
CVD (5m): +$12.50M
CVD last 5: -$2.30M, +$5.10M, -$1.20M, +$8.40M, +$12.50M
Cumulative: +$22.50M
```

正值表示买方主导，负值表示卖方主导。

**代码**：[计算](file:///e:/量化/Hyper-Alpha-Arena/backend/services/market_flow_indicators.py#L234-L301)、[格式化](file:///e:/量化/Hyper-Alpha-Arena/backend/services/ai_decision_service.py#L3149-L3159)

---

### 5.2 TAKER（主动买卖量）

**变量**：`{BTC_TAKER_5m}`

**公式**：

```text
ratio = 主动买入额 / 主动卖出额
log_ratio = ln(ratio)
```

**放进提示词的文本**：

```text
Taker Buy: +$45.20M | Taker Sell: -$38.10M
Buy/Sell Ratio: 1.19x (log: +0.17)
Ratio last 5: 0.85x, 0.95x, 1.05x, 1.12x, 1.19x
Volume last 5: +$70.00M, +$75.00M, +$80.00M, +$82.00M, +$83.30M
```

log_ratio 含义：`+0.69` = 买方是卖方的 2 倍；`0` = 平衡；`-0.69` = 卖方是买方的 2 倍。

**代码**：[计算](file:///e:/量化/Hyper-Alpha-Arena/backend/services/market_flow_indicators.py#L304-L371)、[格式化](file:///e:/量化/Hyper-Alpha-Arena/backend/services/ai_decision_service.py#L3161-L3178)

---

### 5.3 OI_DELTA（持仓量变化百分比）

**变量**：`{BTC_OI_DELTA_5m}`

**公式**：

```text
OI Delta = (当前OI - 上一周期OI) / 上一周期OI × 100%
```

**放进提示词的文本**：

```text
OI Delta (5m): +1.85%
OI Delta last 5: -0.50%, +0.20%, +0.80%, +1.20%, +1.85%
```

| 价格 | OI | 含义 |
|---|---|---|
| 上涨 | 增加 | 新多头进场，趋势可信 |
| 上涨 | 减少 | 空头平仓推动，可能不持续 |
| 下跌 | 增加 | 新空头进场 |
| 下跌 | 减少 | 多头止损/被平仓 |

**代码**：[计算](file:///e:/量化/Hyper-Alpha-Arena/backend/services/market_flow_indicators.py#L475-L545)、[格式化](file:///e:/量化/Hyper-Alpha-Arena/backend/services/ai_decision_service.py#L3192-L3202)

---

### 5.4 FUNDING（资金费率）

**变量**：`{BTC_FUNDING_5m}`

**公式**：

```text
显示值 = 原始费率 × 10000（基点）
百分比 = 原始费率 × 100
年化  = 百分比 × 3 × 365（假设 8h 结算、每日 3 次）
变化  = 当前 - 上一周期
```

**放进提示词的文本**：

```text
Funding Rate: 12.5 (0.0013%)
Funding Change: +2.1 (+0.0002%)
Annualized: 1.40%
Funding last 5: 8.2, 9.5, 10.1, 11.8, 12.5
```

极高正费率 = 多头拥挤；极低或负费率 = 空头拥挤。不能单独作为反转信号。

**代码**：[计算](file:///e:/量化/Hyper-Alpha-Arena/backend/services/market_flow_indicators.py#L548-L621)、[格式化](file:///e:/量化/Hyper-Alpha-Arena/backend/services/ai_decision_service.py#L3204-L3223)

---

### 5.5 DEPTH（盘口深度比）

**变量**：`{BTC_DEPTH_5m}`

**公式**：

```text
Depth Ratio = bid_depth_5 / ask_depth_5
Spread = best_ask - best_bid
```

**放进提示词的文本**：

```text
Bid Depth: +$2.50M | Ask Depth: +$1.80M
Depth Ratio (Bid/Ask): 1.39
Ratio last 5: 0.95, 1.05, 1.20, 1.31, 1.39
Spread: 0.0025
```

`>1` 买盘深度占优，`<1` 卖盘深度占优。

**代码**：[计算](file:///e:/量化/Hyper-Alpha-Arena/backend/services/market_flow_indicators.py#L624-L689)、[格式化](file:///e:/量化/Hyper-Alpha-Arena/backend/services/ai_decision_service.py#L3225-L3239)

---

### 5.6 IMBALANCE（盘口失衡度）

**变量**：`{BTC_IMBALANCE_5m}`

**公式**：

```text
Imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)
```

范围 `-1 ~ +1`。

**放进提示词的文本**：

```text
Order Imbalance: +0.326
Imbalance last 5: -0.050, +0.120, +0.200, +0.280, +0.326
```

接近 `+1` = 强买盘压力；接近 `-1` = 强卖盘压力；`~0` = 平衡。

**代码**：[计算](file:///e:/量化/Hyper-Alpha-Arena/backend/services/market_flow_indicators.py#L692-L746)、[格式化](file:///e:/量化/Hyper-Alpha-Arena/backend/services/ai_decision_service.py#L3241-L3249)

---

### 5.7 PRICE_CHANGE（价格变动百分比）

**变量**：`{BTC_PRICE_CHANGE_5m}`

**公式**：

```text
Price Change = (当前桶末价 - 上一桶末价) / 上一桶末价 × 100%
```

**放进提示词的文本**：

```text
Price Change: +0.352% ($+332.00)
Price: $94,200.00 -> $94,532.00
Change last 5: -0.120%, +0.050%, +0.180%, +0.250%, +0.352%
```

**代码**：[计算](file:///e:/量化/Hyper-Alpha-Arena/backend/services/market_flow_indicators.py#L749-L830)、[格式化](file:///e:/量化/Hyper-Alpha-Arena/backend/services/ai_decision_service.py#L3251-L3267)

---

### 5.8 VOLATILITY（波动率）

**变量**：`{BTC_VOLATILITY_5m}`

**公式**：

```text
Volatility = (窗口最高价 - 窗口最低价) / 窗口最低价 × 100%
```

**放进提示词的文本**：

```text
Volatility: 0.580% ($548.00)
Range: $94,180.00 - $94,728.00
Volatility last 5: 0.210%, 0.320%, 0.400%, 0.510%, 0.580%
```

**代码**：[计算](file:///e:/量化/Hyper-Alpha-Arena/backend/services/market_flow_indicators.py#L833-L913)、[格式化](file:///e:/量化/Hyper-Alpha-Arena/backend/services/ai_decision_service.py#L3269-L3285)

---

### 订单流指标速查表

| 指标 | 变量写法 | 核心公式 | 最少桶数 | 核心含义 |
|---|---|---|---|---|
| CVD | `{BTC_CVD_5m}` | `Σ(主动买-主动卖)` | 1 | 净买卖压力 |
| TAKER | `{BTC_TAKER_5m}` | `主动买/主动卖` | 1 | 主动方向倾向 |
| OI_DELTA | `{BTC_OI_DELTA_5m}` | `(OI变化/OI前值)×100%` | 2 | 资金流入流出 |
| FUNDING | `{BTC_FUNDING_5m}` | 费率×10000 + 年化 | 1 | 拥挤度 |
| DEPTH | `{BTC_DEPTH_5m}` | `bid_depth/ask_depth` | 1 | 即时买卖压力 |
| IMBALANCE | `{BTC_IMBALANCE_5m}` | `(bid-ask)/(bid+ask)` | 1 | 盘口失衡 |
| PRICE_CHANGE | `{BTC_PRICE_CHANGE_5m}` | `(末价-前价)/前价×100%` | 2 | 动量 |
| VOLATILITY | `{BTC_VOLATILITY_5m}` | `(高-低)/低×100%` | 1 | 波动幅度 |

---

## 六、完整提示词模板示例

以下是一个面向短周期预测的完整模板，可直接复制使用：

```text
=== PREDICTION OBJECTIVE ===
Predict BTC price direction for the next 10 minutes.
- UP: future price >= current price * 1.001
- DOWN: future price <= current price * 0.999
- FLAT: between -0.1% and +0.1%
If signals conflict or confidence is low, choose hold.

=== MARKET DATA ===
{BTC_market_data}

=== K-LINE DATA ===
{BTC_klines_1m}(60)
{BTC_klines_5m}(100)

=== MOMENTUM ===
{BTC_RSI7_1m}
{BTC_RSI14_5m}
{BTC_MACD_1m}
{BTC_MACD_5m}

=== ORDER FLOW ===
{BTC_CVD_1m}
{BTC_CVD_5m}
{BTC_TAKER_1m}
{BTC_TAKER_5m}
{BTC_OI_DELTA_5m}

=== ORDER BOOK ===
{BTC_DEPTH_5m}
{BTC_IMBALANCE_5m}

=== VOLATILITY ===
{BTC_ATR14_5m}
{BTC_VOLATILITY_5m}

=== TREND BACKGROUND ===
{BTC_EMA_5m}
{BTC_BOLL_5m}
{BTC_VWAP_5m}
{BTC_OBV_5m}

=== FUNDING ===
{BTC_FUNDING_5m}

=== MARKET REGIME ===
{market_regime_description}
{BTC_market_regime_1m}
{BTC_market_regime_5m}

=== OUTPUT FORMAT ===
{output_format}
```

> 注意：变量越多，Prompt 越长，模型推理越慢。建议根据实际效果逐步增减，不要一次性全加。
