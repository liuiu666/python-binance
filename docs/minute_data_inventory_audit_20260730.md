# BTC 分钟历史数据盘点（2026-07-30）

## 结论

- 原生 1 分钟 K 线来自 Binance **现货** `/api/v3/klines`。以 `data/btcusdt_1m_180d.csv` 衔接较新的 `data/server_latest/btcusdt_1m.csv`，按时间戳去重且让新快照覆盖旧快照后，得到 `2025-12-12 15:42:00 UTC` 至 `2026-07-17 08:33:00 UTC` 的 **312,052 个连续分钟**，时间轴无缺口。
- 原生分钟目录实际有 19 个路径，但绝大多数是同一现货历史的快照、前缀或切片；若把它们直接拼接，会从 312,052 个真实分钟膨胀到 3,184,126 行。
- 所有可识别的原始秒 K 线均标记为 **期货 futures**。排除 pytest 临时样本后，共发现 95 个松散 CSV 路径、55 份不同的松散文件内容；压缩包内另有 13 个秒 CSV 成员，其中 12 个与松散文件完全相同，1 个是独有归档快照。因此总计按 56 份不同内容参与审计。先在文件内按秒去重，再跨快照按 UTC 秒去重，得到 **2,450,461 个唯一秒**；独有归档快照没有增加新的时间戳覆盖。
- 期货秒数据可聚合出 **41,767 个有记录分钟**：29,034 个分钟有 60/60 秒行，12,733 个分钟为稀疏秒行。稀疏不一定等于行情丢失（可能是无成交秒未补行），但在未核对聚合成交 ID 连续性前必须保留质量标记。
- 原生现货分钟和期货秒聚合分钟不能混为同一个市场。现货长历史适合开发波动分层；策略最终评估应优先使用期货分钟，并在二者重叠的 38,275 个分钟上检查波动分层一致性。

## 原生现货分钟文件

所有文件字段均为 `open_time, open, high, low, close, volume`，单文件内均无重复时间戳、无分钟缺口。

| 代表路径 | 起止时间（UTC） | 行数 | 关系/用途 |
|---|---:|---:|---|
| `data/btcusdt_1m_180d.csv` | 2025-12-12 15:42 → 2026-06-10 15:48 | 259,207 | 最长的早期历史 |
| `data/btc_1m_90d.csv` | 2026-03-08 10:40 → 2026-06-06 10:42 | 129,603 | 被 180d 文件覆盖；末行是未完成旧 K 线 |
| `data/server_latest/btcusdt_1m.csv` | 2026-03-08 12:24 → 2026-07-17 08:33 | 188,410 | 最新、优先级最高的后段现货分钟 |
| `data/btcusdt_1m.csv` | 2026-03-08 12:24 → 2026-07-05 15:42 | 171,559 | `server_latest` 的完整前缀 |
| `data/btcusdt_1m.csv.tmp` | 2026-03-08 12:24 → 2026-06-08 18:48 | 132,865 | 未完成 `.tmp`，不应作为研究源 |
| `tmp/latest_pull_20260706_2130/data/btcusdt_1m.csv` | 2026-03-08 12:24 → 2026-07-06 13:31 | 172,868 | `server_latest` 的完整前缀 |
| `tmp/latest_pull_20260708_204204/data/btcusdt_1m.csv` | 2026-03-08 12:24 → 2026-07-08 12:45 | 175,702 | `server_latest` 的完整前缀 |
| `tmp/latest_pull_20260710_203217/data/btcusdt_1m.csv` | 2026-03-08 12:24 → 2026-07-10 12:31 | 178,568 | `server_latest` 的完整前缀 |
| `tmp/signal_profile_20260711/btcusdt_1m.csv` | 2026-03-08 12:24 → 2026-07-11 08:32 | 179,769 | `server_latest` 的完整前缀 |
| `tmp/latest_pull_20260712_migration_fix/extracted/data/btcusdt_1m.csv` | 2026-03-08 12:24 → 2026-07-12 12:36 | 181,453 | `server_latest` 的完整前缀 |
| `tmp/latest_pull_20260712_migration_fix/data/btcusdt_1m.csv` | 2026-03-08 12:24 → 2026-07-12 12:46 | 181,463 | `server_latest` 的完整前缀 |
| `tmp/latest_server_tail_20260716/btcusdt_1m.csv` | 2026-06-25 12:10 → 2026-07-16 15:26 | 30,437 | `server_latest` 的完整切片 |

以下 7 个路径 SHA-256 完全相同（171,559 行），只可计一次：

- `data/btcusdt_1m.csv`
- `tmp/latest_live_pull_20260709_101331/data/btcusdt_1m.csv`
- `tmp/latest_live_pull_20260709_220219/data/btcusdt_1m.csv`
- `tmp/latest_live_pull_20260709_220453/data/btcusdt_1m.csv`
- `tmp/latest_live_pull_20260709_220453/data_clean/btcusdt_1m.csv`
- `tmp/latest_live_pull_20260709_220834/data/btcusdt_1m.csv`
- `tmp/signal_once_v2/btcusdt_1m.csv`

`tmp/liq_live_smoke/btcusdt_1m.csv` 与 `tmp/latest_pull_20260706_2130/data/btcusdt_1m.csv` 也完全相同，smoke 路径不增加覆盖。

三个重叠分钟存在值冲突：

- `2026-06-06 10:42 UTC`：90d 文件的末行是未收盘版本；用较新文件值。
- `2026-06-10 15:48 UTC`：180d 文件的末行是未收盘版本；用较新 `server_latest` 值。
- `2026-06-06 12:26 UTC`：180d 与后续快照值不一致；按冻结的数据源优先级使用后续 `server_latest` 值并保留冲突审计。

## 期货秒数据及可聚合分钟

55 份不同内容使用同一字段结构：

`timestamp, symbol, market, open, high, low, close, volume, quote_volume, trades, taker_buy_volume, taker_sell_volume, taker_buy_quote, taker_sell_quote, taker_buy_sell_ratio, first_trade_time, last_trade_time, first_agg_trade_id, last_agg_trade_id`

去重事实：

- 95 个原始 CSV 路径中有 40 个是其他文件的逐字节副本。
- 55 份松散文件内容加 1 份独有归档内容，合计贡献 4,646,240 个“文件内唯一秒”；跨快照再次去重后为 2,450,461 个唯一 UTC 秒。
- 2,195,779 个贡献行属于跨文件重复；1,082,541 个 UTC 秒出现在至少两份不同内容中，同一秒最多出现在 7 份不同内容中。
- 14 份不同内容内部存在同秒多行，合计 26,876 行重复。聚合前必须按 `last_trade_time`、原始时间戳、文件行序确定性保留最后一行。
- 一个 0 字节 `tmp/latest_live_pull_20260709_101313/data/btcusdt_1s_trades.csv.gz` 无效；另一个 gzip 文件只是对应未压缩 CSV 的内容副本。

### 有记录分钟的连续覆盖块

| # | 起止时间（UTC） | 有记录分钟 |
|---:|---|---:|
| 1 | 2026-06-13 13:33 → 2026-06-20 14:35 | 10,143 |
| 2 | 2026-06-23 13:36 → 2026-06-23 23:59 | 624 |
| 3 | 2026-06-25 18:50 → 2026-06-25 23:59 | 310 |
| 4 | 2026-06-27 06:43 → 2026-06-27 15:10 | 508 |
| 5 | 2026-06-27 15:12 → 2026-06-30 08:38 | 3,927 |
| 6 | 2026-06-30 08:47 → 2026-06-30 08:54 | 8 |
| 7 | 2026-06-30 08:58 → 2026-07-04 17:44 | 6,287 |
| 8 | 2026-07-05 04:49 → 2026-07-05 05:52 | 64 |
| 9 | 2026-07-05 06:06 → 2026-07-16 15:29 | 16,404 |
| 10 | 2026-07-27 00:00 → 2026-07-29 10:11 | 3,492 |

明确的整分钟空洞共 9 段：

| 起止时间（UTC） | 缺失分钟 |
|---|---:|
| 2026-06-20 14:36 → 2026-06-23 13:35 | 4,260 |
| 2026-06-24 00:00 → 2026-06-25 18:49 | 2,570 |
| 2026-06-26 00:00 → 2026-06-27 06:42 | 1,843 |
| 2026-06-27 15:11 | 1 |
| 2026-06-30 08:39 → 08:46 | 8 |
| 2026-06-30 08:55 → 08:57 | 3 |
| 2026-07-04 17:45 → 2026-07-05 04:48 | 664 |
| 2026-07-05 05:53 → 06:05 | 13 |
| 2026-07-16 15:30 → 2026-07-26 23:59 | 14,910 |

主要路径关系：

- `tmp/latest_pull_20260706_2130/data/second/BTCUSDT/futures/`、`tmp/server_second_shards_latest/second/BTCUSDT/futures/`、`tmp/server_second_shards_scan/second/BTCUSDT/futures/` 下的 2026-06-13 至 2026-07-04 日分片大多是三份完全相同副本。
- `data/btcusdt_1s_trades.csv` 与 `data/btcusdt_1s_trades_server.csv` 覆盖 2026-06-13 至 06-17，并与上述日分片重叠。
- 2026-07-05 至 07-13 有多批 `latest_pull`、`live_*`、`signal_*` 和 `v2_exact_snapshot` 快照，相互高度重叠。
- `data/server_latest/btcusdt_1s_trades.csv` 与其 07-15、07-16 日分片覆盖后段；`tmp/latest_server_tail_20260716` 是重叠尾部。
- `tmp/stable_winrate_local/today_20260728/2026-07-27.csv` 和 `2026-07-28.csv` 提供 07-27/28；`tmp/v14_forward_20260729/btcusdt_1s_trades.csv` 提供 07-28 至 07-29 10:11，二者在 07-28 重叠。
- `tmp/latest_live_pull_20260709_220453/btc_latest_pull.tar.gz::btc_latest_pull/btcusdt_1s_trades.csv` 是唯一只存在于 tar 包内的不同秒文件，覆盖 2026-07-08 00:00 至 07-09 14:04:52 UTC（137,077 行，1 个无效时间戳）；其 137,076 个有效秒均已被其他快照覆盖。

另外，`data/codex.db` 的 `price_ticks` 表有 7,787 行、7,758 个不同毫秒时间戳（2026-06-09 至 2026-07-05），但没有 symbol、market、OHLCV 或成交 ID 来源字段，不能可靠判定为现货或期货，也不能当作本次规范分钟源。auction 的逐笔事件文件属于另一套事件级数据，且没有增加上述分钟外沿；若后续使用，应单独做成交 ID 连续性审计。

完整逐路径、SHA-256、起止、行数、文件内重复、时间缺口和别名关系见：

- `tmp/minute_native_inventory_raw_20260730.json`
- `tmp/minute_second_inventory_raw_20260730.json`

## 给波动分层研究的使用约束

1. 长历史开发层：用去重后的现货 312,052 分钟探索波动状态，但不要把现货收益直接当期货成交收益。
2. 期货验证层：把唯一秒聚合为期货分钟；`open=首秒 open`、`high=max(high)`、`low=min(low)`、`close=末秒 close`，成交量、成交额、成交笔数和 taker 字段求和。
3. 每个聚合分钟保留 `seconds_observed`、`dense_60s`、`source_count`、`agg_trade_id_contiguous`。稀疏分钟只有在确认聚合成交 ID 连续后才可视作“无成交秒”，否则标为不完整。
4. 九段整分钟空洞必须切开滚动窗口；波动率、分位数和正态回归状态不能跨空洞继承。
5. 波动状态必须只由信号时刻之前已收盘的分钟计算。若信号发生在分钟内部，不得使用该分钟最终 high/low/close。
