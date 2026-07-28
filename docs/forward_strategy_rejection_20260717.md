# 2026-07-17 V9/V13 前向否决审计

生成时间：上海 `2026-07-17 18:24:37`

## 结论

- 状态：`REJECTED_FORWARD_FAIL`
- 是否可上线：`false`
- 执行动作：V9/V13 不允许实盘上线；继续禁用，只保留失败样本和影子观察。

拒绝原因：
- today raw shadow forward is 0/3
- today deduped independent forward is 0/2
- V9/V13 emitted the same-time same-direction idea, so strategy-family independence failed
- local high-frequency replay files do not fully cover today's forward window

## 今天前向结果

- 原始 shadow：0/3，胜率 `0.0%`，PnL `-15.0U`，最大连亏 `3`。
- 去重后独立机会：0/2，胜率 `0.0%`，PnL `-10.0U`，最大连亏 `2`。
- 去重口径：同一方向、同一期限、同一 actionable/signal 秒级时间，只算一个独立机会。

今天的 3 笔原始亏损里，有一组 V9/V13 同秒同方向 DOWN 重复信号；这不是两条独立策略同时验证成功，而是同一交易想法被重复记录。

## 历史候选不能再直接采用

- 历史报告：48/66，胜率 `72.73%`，PnL `102.0U`。
- 历史最大回撤 `11.0U`，历史最大连亏 `2`，日均 `6.0` 单。
- 这些历史数字只能说明它曾经是候选；今天前向 0% 后，它不能作为可上线策略。

## 本地数据覆盖

- `1m`：最新上海时间 `2026-07-17 16:33:00`，文件 `E:\python-binance\data\server_latest\btcusdt_1m.csv`。
- `1s_trades`：最新上海时间 `2026-07-16 23:21:36`，文件 `E:\python-binance\data\server_latest\btcusdt_1s_trades.csv`。
- `orderbook_1s`：最新上海时间 `2026-07-16 23:24:31`，文件 `E:\python-binance\data\server_latest\btcusdt_orderbook_1s.csv`。

本地 `data/server_latest` 的秒级成交和订单簿文件没有完整覆盖今天实时窗口，因此不能宣称“今天已用本地原始高频数据完整回放通过”。当前只能使用线上只读接口确认今天真实 shadow 前向结果。

## 当前信号快照

- `BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V9`：signal `None`，reason `liq_normal_not_ready`，时间 `2026/07/17 18:24:34`，tradeEnabled `False`。
- `BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V13_SHADOW`：signal `None`，reason `liq_normal_not_ready`，时间 `2026/07/17 18:24:34`，tradeEnabled `False`。

## 后续验收标准

- 不再用历史胜率单独决定上线。
- 实盘前至少要有 20 笔新的前向 shadow 独立机会。
- 前向胜率至少 63%，最大前向连亏不超过 2。
- V9/V13 这类同族重复信号必须去重统计，不能把重复信号当频率。
- 本地秒级成交和订单簿必须覆盖被验证日期，否则只能写“线上前向统计”，不能写“本地完整回放”。
