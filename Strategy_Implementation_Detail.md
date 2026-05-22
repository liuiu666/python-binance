# BTC 10 分钟二元期权：均值回归策略 (A3) 实施手册

本文档记录了项目当前生产策略 **A3 Mean-Reversion** 的研发过程、关键 BUG 修复、回测验证、以及所有相关代码与运行说明。

旧版「裸 K 线序列 (RawSequence)」策略经 1 年走样验证已被证明为统计噪声，相关文档归档到 `RawSequenceStrategy_Implementation_Detail.legacy.md`。

---

## 1. 策略核心思想

10 分钟二元期权的盈亏平衡门槛（按 1.8x 赔付计算）为 **55.56% 胜率**，每笔投 5U 时赢 +4U / 输 -5U。

A3 策略的假设是：**当价格相对中期均线被拉得极远，且伴随异常放量、且整体市场不处于剧烈混乱状态时，10 分钟内大概率发生均值回归**。

三个必要条件全部成立才下单：

| 条件 | 含义 | 阈值 |
|---|---|---|
| `ema120_dev` 进入极端十分位 | 收盘价距 EMA(120) 的距离按 ATR(120) 归一化 | 14 天滚动 10% / 90% 分位 (≈ ±3.4) |
| `vol_z40 > 1` | 当前成交量相对 40 分钟中位数显著放大 | 1 个标准差以上 |
| `rv_z ∈ (-1, +1)` | 60 分钟实现波动率相对 24 小时基线不离谱 | -1 到 +1 标准差区间 |

下单方向：

- `ema120_dev ≤ q10` → **CALL**（被打到极低，预期反弹）
- `ema120_dev ≥ q90` → **PUT**（被推到极高，预期回落）

每笔信号生成后**锁定 10 个 1m K 线**（10 分钟），期间不再触发新信号。

---

## 2. 数据与回测口径

| 项目 | 配置 |
|---|---|
| 交易标的 | `BTC/USDT:USDT`（USDT-M 永续合约） |
| 数据源 | Binance fapi `https://fapi.binance.com/fapi/v1/klines` |
| 时间帧 | 1 分钟 |
| 历史数据 | 1 年（约 525,600 根 1m K 线） |
| 期权时长 | 10 分钟（= 10 个 1m K 线） |
| 单笔投注 | 5 USDT |
| 赔付 | 1.8x（赢 +4U / 输 -5U） |
| 盈亏平衡门槛 | WR ≥ 55.56% |

---

## 3. 走样验证结果（24-fold expanding walk-forward）

| 指标 | 值 |
|---|---|
| OOS 笔数 | 5,513 / 年 |
| OOS 胜率 | **57.97%** |
| Wilson 95% 下界 | **56.66%**（显著高于 55.56% 门槛） |
| 累计 PnL（5U 投注） | **+1,199 U** |
| 最大回撤 | 145 U |
| 最大连败 | 11 |
| Calmar 比 | **8.27** |
| 月度盈利占比 | 10/13 月正收益 |

二元期权专用回测器（消费策略文件作为单一信源）独立验证：

| 指标 | 值 |
|---|---|
| 笔数 | 6,614 |
| WR | 57.02% |
| Wilson LB | 55.82% |
| PnL | +869 U |
| MDD | 204 U |
| Calmar | 4.26 |

两条独立路径都通过盈亏平衡门槛。

---

## 4. 已被证伪的优化方向

研发过程中尝试并被严格 H1/H2 时序拆分**否决**的方向：

1. **裸 K 线方向序列 (RawSequence)** — 1 年 OOS 仅 +12U / 24 折，且原 13 个精英 combos 在 OOS 上 0/13 通过 Wilson 检验。
2. **Regime-conditional cell selection** — 单年表面看 Calmar 14，但 H1 选出的 cell 在 H2 上 Wilson LB 跌至 50.72%，0 个 cell 在两半都通过门槛。
3. **HistGradientBoosting** — 在同样特征上严重过拟合，p_thr=0.55 时年损失 -2824U。线性 logreg 反而表现更好。

---

## 5. 关键 BUG 修复（已修复）

研发过程中发现并修复的真实 BUG：

| # | 位置 | 问题 | 修复 |
|---|---|---|---|
| 1 | `live_signal_runner.py` 数据源 | 之前使用现货 `api.binance.com/api/v3/klines`，与回测的永续合约数据不匹配 | 改为 `fapi.binance.com/fapi/v1/klines` |
| 2 | `RawSequence2mOptimalStrategy.py` 时间偏移 | 2m bar 信号 +2min shift 后 merge 到 1m，导致 freqtrade 进场价比信号晚 2 分钟 | 改为 +1min shift（无前视，进场价 ≈ 2m bar close） |
| 3 | freqtrade ccxt 代理 | 默认配置 ccxt 异步层不识别 HTTP_PROXY 环境变量 | 在 `user_data/config_backtest.json` 的 `ccxt_async_config` 加 `aiohttp_proxy` |
| 4 | RawSequence 指标口径 | 实盘用 rolling 20 / ×1.4，但精英 combos 是用 rolling 15 / ×1.0 选出的 | 整体放弃 RawSequence，改用 A3 |

---

## 6. 项目代码结构

### 6.1 策略文件（单一指标信源）

`@e:\量化\bxm40\user_data\strategies\MeanReversion10mStrategy.py`

freqtrade `IStrategy` 实现。所有指标计算和信号定义都在这一份文件，下游回测器和实盘信号机都消费它。

### 6.2 回测器

`@e:\量化\bxm40\user_data\notebooks\binary_option_backtest.py`

直接 import 策略类，调用 `populate_indicators` + `populate_entry_trend`，然后用 1.8x 赔付 + 10 分钟锁定来模拟二元期权 PnL，输出月度收益分布。

> 不要用 `freqtrade backtesting` 子命令评估二元期权策略 —— 它使用线性合约 + 手续费模型，与二元期权赔付结构不兼容。同样信号在那里会显示亏损。

### 6.3 实盘信号机

`@e:\量化\bxm40\user_data\notebooks\live_signal_runner.py`

- 启动时 bootstrap 14 天（约 20,360 根）1m 历史数据
- 每分钟轮询新闭合 K 线
- 复用与策略文件相同的指标定义
- 信号触发时通过钉钉 webhook 推送下单通知
- 10 分钟后通过 K 线收盘价结算并推送结算通知
- 进程通过 PID 文件保证单实例
- `active_trades.json` 持久化未结算订单，重启后自动恢复

### 6.4 研究脚本（可复现的研发过程）

| 脚本 | 用途 |
|---|---|
| `walkforward_combo_research.py` | RawSequence walk-forward 验证（结果：原策略不可用） |
| `feature_lab.py` | 21 个候选特征逐分位前向 WR 扫描 |
| `mean_reversion_research.py` | A3 在 2m grid 上的初次发现 |
| `mean_reversion_1m_research.py` | A3 升级到 1m grid + ATR 过滤（最终配置） |
| `regime_conditional_research.py` | 4 轴 regime cell 切片探索（结果：过拟合） |
| `regime_validation.py` | 严格 H1/H2 时序拆分否决 regime 方法 |

---

## 7. 运行指南

### 7.1 数据下载

```powershell
.venv\Scripts\freqtrade download-data `
    --config user_data/config_backtest.json `
    --pairs "BTC/USDT:USDT" `
    --timeframes 1m `
    --trading-mode futures `
    --timerange 20250520-20260522 `
    --erase
```

> 已在 `user_data/config_backtest.json` 中通过 `ccxt_async_config.aiohttp_proxy` 配置好 Clash 7897 代理。

### 7.2 回测

```powershell
.venv\Scripts\python -u user_data/notebooks/binary_option_backtest.py
```

期望输出：WR ~57%，Wilson LB ≥ 55.6%，PnL > 0，月度多数为正。

### 7.3 实盘信号机

```powershell
.venv\Scripts\python -u user_data/notebooks/live_signal_runner.py
```

启动后会：
1. 拉取 14 天历史 bootstrap 缓存
2. 每分钟检查新 K 线，输出 `ema_dev / vol_z / rv_z` 实时值
3. 满足条件自动推送钉钉，并加入 `active_trades.json` 等待 10 分钟后结算

终止：`Ctrl+C` 或 `taskkill /F /PID <pid>`。新启动会自动检测到旧进程并杀掉。

---

## 8. 钉钉机器人配置

1. 钉钉群聊：群设置 → 智能群助手 → 添加机器人 → 自定义机器人
2. 安全设置：自定义关键词 `666`
3. 复制 Webhook URL 替换 `live_signal_runner.py` 顶部的 `DINGTALK_WEBHOOK`

---

## 9. 风险声明

- **回测结论建立在 2025-05-20 至 2026-05-22 这 1 年的 BTCUSDT 永续数据上**。市场结构改变时，A3 假设可能失效——建议每季度重跑 `binary_option_backtest.py` 检查近 90 天 WR 是否仍 ≥ 55.6%。
- **二元期权平台的实际赔付不一定真是 1.8x**。如果赔付低于 1.8x（例如 1.7x，对应 BE WR = 58.8%），A3 当前 WR 接近边际，需要重新评估。
- 最大连败 11 次意味着实盘可能连续亏 55U；需要至少 5 倍以上的资金缓冲（≥ 275U）才有信心穿越回撤。
