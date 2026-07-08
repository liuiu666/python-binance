# BTC 10分钟正态状态策略目标完成审计

生成时间: 2026-07-03T19:53:41.222253+00:00

结论: 所有目标要求均有当前证据支撑。

## 推荐策略

`BTC_10min_NORMAL_STATE_V11_BANDWALK_2OF5_D5A5`

- 交易数: `33`
- 胜率: `78.79%`
- 盈亏: `13.8U`
- 最大回撤: `-2.0U`
- 训练段: `25` 单, `76.0%`, `9.2U`
- 近期样本外: `8` 单, `87.5%`, `4.6U`
- 拟合风险: `medium_high`
- 风险标记: `sample_under_50_trades;recent_under_15_trades;recent_wilson_low_below_breakeven`

## 逐项审计

### local_strategy_prototype

- 状态: `proven`
- 证据: py/second_backtest/normal_state_v11.py and py/verify_normal_state_v11_strategy.py exist.

### exact_strategy_verification

- 状态: `proven`
- 证据: tmp/normal_state_v11_strategy_verify.json checks={'row_count_match': True, 'sequence_match': True, 'value_mismatch_count': 0, 'passed': True}

### dynamic_market_state

- 状态: `proven`
- 证据: V12 identifies bandwalk>=6 as a bad continuation state and V11 uses bandwalk<6 as the rolling state gate.

### second_minute_orderbook_data

- 状态: `proven`
- 证据: rows_observed=1253041, minute_source=E:\python-binance\tmp\latest_market_pull_20260703_223340\btcusdt_1m.csv, orderbook_sources=13, recommended_orderbook={'available': True, 'strategy_key': 'D5_A5_V6_CONSENSUS_2OF5_UPPER_edge_persistence_lt6', 'n': 33, 'ob_available_n': 10, 'ob_available_pct': 30.3, 'columns': ['ob_available', 'ob_imb20', 'ob_micro_bps']}

### walkforward_oos_validation

- 状态: `proven`
- 证据: V11 train/recent: train_n=25, recent_n=8; V12 walkforward rows=5.

### metrics_output

- 状态: `proven`
- 证据: Recommended metrics n=33, wr=78.79, pnl=13.8, max_dd=-2.0, fit_risk=medium_high.

### overfit_risk_disclosed

- 状态: `proven`
- 证据: fit_risk=medium_high, risk_flags=sample_under_50_trades;recent_under_15_trades;recent_wilson_low_below_breakeven

### final_reports

- 状态: `proven`
- 证据: docs/normal_state_v11_capacity_frontier_report.md and docs/normal_state_v12_walkforward_state_selector_report.md exist.

## 订单薄覆盖

- 推荐策略交易数: `33`
- 订单薄可用交易数: `10`
- 订单薄覆盖率: `30.3%`

说明: 订单薄已经进入特征链路，但覆盖不足，因此当前不作为主过滤条件。
