# 2026-07-28 V9/V13 前向否决审计

生成时间：上海 `2026-07-28 22:18:47`
冻结证据起点：`2026-07-15T11:09:37+00:00`；服务器别名新采样起点：`2026-07-28T13:59:23.041000+00:00`。

## 结论

- 状态：`REJECTED_FORWARD_FAIL`
- 是否可上线：`false`
- 执行动作：V9/V13 不允许实盘上线；冻结 V13 仅保持影子运行并继续收集前向样本。

拒绝原因：
- cumulative raw shadow forward is 0/3
- cumulative deduped independent forward is 0/2
- V9/V13 emitted the same-time same-direction idea, so strategy-family independence failed
- new frozen-alias forward sample is 0/20
- local high-frequency replay files do not fully cover the cumulative forward window

## 冻结后累计前向结果

- 原始 shadow：0/3，胜率 `0.0%`，PnL `-15.0U`，最大连亏 `3`。
- 去重后独立机会：0/2，胜率 `0.0%`，PnL `-10.0U`，最大连亏 `2`。
- 本次参数校验后的新冻结影子：0/0，胜率 `N/A`；晋级门槛按这一栏累计到 20 单。
- 去重口径：同一方向、同一期限、同一 actionable/signal 秒级时间，只算一个独立机会。

冻结后累计样本中发现 `1` 组 V9/V13 同秒同方向重复信号；同族重复只算一个独立机会。

## 历史候选不能再直接采用

- 历史报告：48/66，胜率 `72.73%`，PnL `102.0U`。
- 历史最大回撤 `11.0U`，历史最大连亏 `2`，日均 `6.0` 单。
- 这些历史数字只能说明它曾经是候选；2026-07-17 的首次前向失败后，它不能作为可上线策略。

## 本地数据覆盖

- `1m`：最新上海时间 `2026-07-17 16:33:00`，文件 `E:\python-binance\data\server_latest\btcusdt_1m.csv`。
- `1s_trades`：最新上海时间 `2026-07-16 23:21:36`，文件 `E:\python-binance\data\server_latest\btcusdt_1s_trades.csv`。
- `orderbook_1s`：最新上海时间 `2026-07-16 23:24:31`，文件 `E:\python-binance\data\server_latest\btcusdt_orderbook_1s.csv`。

本地 `data/server_latest` 没有覆盖全部冻结后窗口，因此线上真实 shadow 结果与本地固定延迟回放必须分开报告。

## 当前信号快照

- `BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V13_FREQ`：signal `None`，reason `liq_normal_not_ready`，时间 `2026/07/28 22:18:39`，tradeEnabled `False`。
- 当前服务器实际信号策略：`BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V13_FREQ, BTC_30min_SHADOW_CANDIDATE`。

## 后续验收标准

- 不再用历史胜率单独决定上线。
- 实盘前至少要有 20 笔新的前向 shadow 独立机会。
- 前向胜率至少 63%，最大前向连亏不超过 2。
- V9/V13 这类同族重复信号必须去重统计，不能把重复信号当频率。
- 本地秒级成交和订单簿必须覆盖被验证日期，否则只能写“线上前向统计”，不能写“本地完整回放”。
