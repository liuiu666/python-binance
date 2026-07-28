# 秒级正态反转冻结审计（2026-07-28）

## 结论

- 状态：`REJECTED_FORWARD_GATES`。
- 真实交易：禁止。当前证据只允许继续研究，尚不允许晋级实盘。
- 本地冻结后共 `5` 单；与 7 月 17 日线上真实影子合并后为 `7` 单，胜率 `71.43%`。
- 晋级只统计本次参数校验后的新冻结影子：当前 `0/20`；本地回放和旧家族证据不得抵扣。

## 固定方法

- 策略：`BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V13_SHADOW`。
- 参数冻结时间：`2026-07-15T11:09:37+00:00`。
- 未搜索、未调整参数；直接读取冻结 V13 影子配置和线上共享因果核心。
- 代表性快照按 `time + signal + branch` 去重，再执行全策略 600 秒冷却。
- 去重后数据覆盖 `223.961` 小时，其中冻结后本地未见段 `28.03` 小时。
- 盈亏按每单 5U、盈利 +4U、亏损 -5U。

## 冻结后本地固定延迟

| 入场延迟 | 单数 | 胜率 | PnL | 最大回撤 | 最大连亏 |
|---:|---:|---:|---:|---:|---:|
| 0s | 5 | 100.0% | 20.0U | 0.0U | 0 |
| 5s | 5 | 100.0% | 20.0U | 0.0U | 0 |
| 6s | 5 | 100.0% | 20.0U | 0.0U | 0 |
| 10s | 5 | 100.0% | 20.0U | 0.0U | 0 |

这 5 单在四种延迟下均获胜，但样本不足；其中 4 单来自衰竭订单簿补充分支，不能据此宣称策略稳定。

## 合并前向证据

- 本地固定回放：5/5。
- 2026-07-17 线上真实影子：0/2。
- 合并：5/7，胜率 `71.43%`，PnL `10.0U`，最大连续亏损 `2`。
- 线上两单使用真实 actionable/open 时间，不能伪装成某一个固定延迟档；因此只进入综合前向证据，不进入 0/5/6/10 秒表格。

## 新冻结影子采样纪元

- 独立单数：`0/20`。
- 胜率：`N/A%`。
- PnL：`0U`；最大回撤：`0.0U`；最大连亏：`0`。

## 验收门槛

| 门槛 | 要求 | 当前 | 通过 |
|---|---:|---:|:---:|
| minimumNewFrozenAliasTrades | 20 | 0 | 否 |
| minimumNewFrozenAliasWinRatePct | 63.0 | None | 否 |
| maximumNewFrozenAliasDrawdownU | 20.0 | 0.0 | 是 |
| maximumNewFrozenAliasLossStreak | 2 | 0 | 是 |
| allDelaysProfitable | True | True | 是 |

## 复现

```powershell
python py/research_second_normal_reversal_frozen.py
```

- 配置 SHA-256：`671730c8af079680092068de5001f4c3ebdd696a13ec933ac05471151f26193c`
- 冻结策略 SHA-256：`0fc6233686632b48a36a5fd556b9c3165baa9affae61cb013cedc8d463fa936a`
- 共享核心 SHA-256：`98f6928a41de83207f40a6bdc467d6dd413a5f6b52b8d107237637b1372dc174`
- 交易明细：`tmp/second_normal_reversal_frozen_trades.csv`
- 机器报告：`tmp/second_normal_reversal_frozen_audit.json`

## 下一步

保持 `tradeEnabled=false`。服务器使用清单中登记的 V13 别名运行冻结参数；新采样纪元从 `2026-07-28T13:59:23.041Z` 开始，旧参数产生的历史交易不进入冻结验收。

运行 `python py/manage_frozen_second_normal_shadow.py` 必须得到 `ready=true`；当前信号服务为 `shadow_only=true`、`trade_enabled=false`。累计至少 20 个去重后的新前向机会后再按同一冻结规则复核，期间不得依据输赢调整参数。
