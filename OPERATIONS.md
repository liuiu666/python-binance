# 运行手册 (Operations Guide)

本项目当前生产形态：**A4 多期 EMA 共振策略** + 实时信号推送 + 订单流数据录制。

> **2026-05-23 代码修复**：本次更新修复了 8 个已确认问题（见 §8），
> 并通过 `a4_backtest.py` 完成了 A4 策略的首次独立 OOS 回测验证。

---

## 0. 环境准备 (一次性)

```powershell
# 1. 创建虚拟环境
python -m venv .venv

# 2. 激活
.venv\Scripts\Activate.ps1

# 3. 安装依赖
pip install -r requirements.txt
# 关键库: pandas, numpy, requests, aiohttp, scikit-learn, pyarrow, freqtrade

# 4. 验证
.venv\Scripts\python -c "import pandas, aiohttp, sklearn; print('ok')"
```

代理（如本机走 Clash/Mihomo 7897 端口）：
- `live_signal_runner.py` 内置 `PROXIES = {"http": "http://127.0.0.1:7897", ...}`
- `flow_data_recorder.py` 读环境变量 `FLOW_PROXY`，默认 `http://127.0.0.1:7897`
- 不需要代理时改成 `None` 或 `""`

---

## 1. 数据下载（历史回测用）

### 1.1 标准 1 年 1m OHLCV + Taker Buy Volume

```powershell
.venv\Scripts\python -u user_data/notebooks/download_extended_klines.py
```

输出：`user_data/data/binance/futures/BTC_USDT_USDT-1m-futures-extended.feather`  
约 534,000 行，包含 `taker_buy_base / taker_buy_quote / num_trades / quote_volume`。

---

## 2. 回测与研究

所有研究脚本都在 `user_data/notebooks/`，独立可跑，输入都是上面那个 feather。

| 脚本 | 用途 |
|---|---|
| `walkforward_combo_research.py` | A3 信号 walk-forward 验证 |
| `feature_lab.py` | OHLCV 候选特征单调性 + Wilson LB |
| `taker_flow_feature_lab.py` | 主动买卖盘订单流特征 |
| `a3_takerflow_interaction.py` | A3 × Taker Flow 交互效应 |
| `mean_reversion_research.py` | 2m 网格 A3 发现 |
| `mean_reversion_1m_research.py` | 1m 网格 A3 + ATR 过滤 |
| `regime_conditional_research.py` | 4 轴 regime 切片 |
| `regime_validation.py` | H1/H2 严格时间外验证 |
| `adaptive_regime_research.py` | 自适应 cell 选择 |
| `position_sizing_research.py` | 动态仓位实验 |
| `frequency_tradeoff_research.py` | 触发频率 vs WR 曲线 |
| `march2026_diagnosis.py` | 月度回撤诊断 |
| `binary_option_backtest.py` | 二元期权专用回测器（1.8x 赔付 / 10m 到期） |

运行例：

```powershell
.venv\Scripts\python -u user_data/notebooks/binary_option_backtest.py
.venv\Scripts\python -u user_data/notebooks/taker_flow_feature_lab.py
```

---

## 3. 实盘（Paper Trading + DingTalk 推送）

### 3.0 当前策略：A4 多期共振 + 信号分级

| 信号类型 | 触发条件 | 预期 WR | 预期 LB | 频率 | 仓位建议 |
|---|---|---|---|---|---|
| **高质 CALL** | 4 个 EMA (30/60/120/240) 偏离 全部 q≤0.05 | 59% | 57.3% | 9/天 | **10U** |
| **高质 PUT** | ≥2 个 EMA 偏离 q≥0.95 | 58% | 57.1% | 19/天 | **10U** |
| **普通 CALL** | 4 个 EMA 偏离 全部 q≤0.10 且 未达 HQ | 58% | 56.8% | 10/天 | **5U** |
| **普通 PUT** | ≥3 个 EMA 偏离 q≥0.90 且 未达 HQ | 57% | 56.3% | 5/天 | **5U** |
| 公共过滤 | `vol_z40 > 1.0` (放量) 且 `\|rv_z\| < 1.0` (波动正常) | | | | |

预期年化：**+2,773U / 5U 同资金量**（1.8x 赔付，纯实盘其实能到 +5,500U/年差异仓位）。

### 3.0 A4 策略回测验证结果（2026-05-23）

| 指标 | A3（历史） | A4（本次验证） | 对比 |
|------|-----------|--------------|------|
| 总笔数/年 | 6,614 | 5,180 | -1,434 |
| 胜率 | 57.02% | **58.13%** | +1.11% |
| Wilson 95% LB | 55.82% | **56.78%** | +0.96% |
| 累计 PnL | +869 U | **+1,199 U** | +330 U |
| 最大回撤 | 204 U | **160 U** | -44 U |
| 最大连败 | 13 | **8** | -5 |
| Calmar | 4.26 | **7.49** | +3.23 |
| 单笔 EV | +0.131 U | **+0.231 U** | +0.10 U |

**高质 (HQ) vs 普通 (NORM) 分解**：

| 分级 | 笔数 | 胜率 | Wilson LB | 结论 |
|------|------|------|-----------|------|
| HQ | 2,626 | 58.91% | **57.02%** | ✅ 通过 55.56% |
| NORM | 2,554 | 57.32% | 55.39% | ⚠️ 未过 Wilson 门槛 |

**结论**：A4 整体显著优于 A3，但 NORM 信号 Wilson LB = 55.39% < 55.56%，
建议实盘**仅跟单 HQ 信号**（或最多 5U 跟 NORM）。

> `live_signal_runner.py` 已在本次修复后重新部署，钉钉推送时区分 HQ/NORM。

---

### 3.1 启动 Runner（标准方式）

```powershell
.\start_runner.ps1
```

这个脚本会自动处理中文编码（UTF-8）、写控制台 + `logs\runner_a4.log`。关窗口即停；要后台常驻：

```powershell
Start-Process powershell -ArgumentList '-NoExit','-ExecutionPolicy','Bypass','-File','start_runner.ps1'
```

### 3.2 停止 Runner

```powershell
.\stop_runner.ps1
```

会查找所有 PowerShell 壳 + venv launcher + 真 Python 全部杀掉，并清理 PID 文件。

### 3.3 Runner 行为

- 每 60s 拉 Binance fapi `/fapi/v1/klines` 的 BTCUSDT 1m K 线
- 计算 4 期 EMA 偏离 + 14 天滚动分位 + vol_z + rv_z
- 触发即 DingTalk 推送，标 `[A4 HQ]` 或 `[A4 NORM]`
- 10 分钟后推送结算通知（胜/负 + PnL）
- 单实例锁：`user_data/notebooks/live_signal_runner.pid`
- 持仓快照：`user_data/notebooks/active_trades.json`（崩溃恢复用）

### 3.4 配置 DingTalk

在 `live_signal_runner.py` 顶部：

```python
DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=..."
KEYWORD = "666"   # 机器人安全设置-自定义关键词
```

### 3.5 控制台输出示例

每分钟一行，实时看距离触发还差几个 EMA：

```
[02:50:08] 2026-05-23 02:49 价=76800.0  e30=-3.21(q=0.04) e60=-3.45(q=0.06) e120=-3.67(q=0.08) e240=-2.89(q=0.12)  vz=+1.42[放量OK] rvz=+0.65[OK]  => CALL[高质差1EMA/普通差0EMA] PUT[高质差2EMA/普通差4EMA]
```

---

## 4. 订单流数据录制（30 天后用于扩展特征）

### 4.1 启动 Recorder

```powershell
.venv\Scripts\python -u user_data/notebooks/flow_data_recorder.py
```

订阅四路并落盘 `user_data/data/flow/`：

| 文件 | 来源 | 频率 |
|---|---|---|
| `liquidations.YYYY-MM-DD.parquet` | WS `btcusdt@forceOrder` | 事件触发 |
| `markprice.YYYY-MM-DD.parquet` | WS `btcusdt@markPrice@1s` | 1Hz |
| `oi.YYYY-MM-DD.parquet` | REST `openInterestHist` 5m | 60s 轮询 |
| `ratios.YYYY-MM-DD.parquet` | REST `topLongShortPositionRatio` + `topLongShortAccountRatio` + `globalLongShortAccountRatio` + `takerlongshortRatio` | 60s 轮询 |

- 按 **UTC 日期**滚动文件
- 每 30s 强制 flush 一次盘
- 断线自动重连（5s 退避）
- Ctrl+C 优雅退出并 flush

### 4.2 后台常驻（Windows）

```powershell
Start-Process -WindowStyle Hidden -FilePath ".venv\Scripts\python.exe" `
  -ArgumentList "-u","user_data/notebooks/flow_data_recorder.py" `
  -RedirectStandardOutput "logs\flow.log" -RedirectStandardError "logs\flow.err"
```

---

## 5. 同时跑 Runner + Recorder（推荐）

最简：开两个 PowerShell 窗口分别运行 §3.1 和 §4.1。

或一行后台启动：

```powershell
mkdir -Force logs | Out-Null
Start-Process -WindowStyle Hidden .venv\Scripts\python.exe -ArgumentList "-u","user_data/notebooks/live_signal_runner.py" -RedirectStandardOutput logs\runner.log -RedirectStandardError logs\runner.err
Start-Process -WindowStyle Hidden .venv\Scripts\python.exe -ArgumentList "-u","user_data/notebooks/flow_data_recorder.py" -RedirectStandardOutput logs\flow.log -RedirectStandardError logs\flow.err
```

检查是否在跑：

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'live_signal_runner|flow_data_recorder' } |
  Select-Object ProcessId, CommandLine
```

查看最新数据时间：

```powershell
.venv\Scripts\python -c "import pandas as pd, glob; [print(p, len(pd.read_parquet(p))) for p in sorted(glob.glob('user_data/data/flow/*.parquet'))]"
```

---

## 6. 30 天后下一步

录够 30 天数据后跑（待写）：

```powershell
.venv\Scripts\python -u user_data/notebooks/flow_feature_lab.py        # 待建
.venv\Scripts\python -u user_data/notebooks/a3_flow_interaction.py     # 待建
```

期望验证：在 A3 触发条件上叠加 **爆仓爆发 / OI 急变 / 散户极端多空** 是否能把 Wilson LB 推过 55.6%（盈亏平衡）甚至 60%。

---

## 7. 常见问题

**Q1. DingTalk 没收到推送？**
- 钉钉机器人安全设置必须包含 `KEYWORD` 字符串
- 看 `logs\runner.err`，常见是代理不通

**Q2. WS 老掉线？**
- Binance fstream 强制 24h 断一次，脚本会自动重连
- 如果是网络问题，检查 `FLOW_PROXY` 环境变量

**Q3. 数据有缺口？**
- REST 轮询 dedupe by timestamp，断网期间数据丢失是正常的
- 如果丢得多，建议 VPS 部署而不是本机

**Q4. 怎么停？**
- 前台：Ctrl+C
- 后台：找到 PID 后 `Stop-Process -Id <pid> -Force`，runner 还会自动清 `*.pid` 文件

---

## 8. 代码修复记录 (2026-05-23)

本次全面修复了以下 8 个问题，**强烈建议重新部署**。

### FIX-1: True Range ATR（严重）
**问题**：ATR 只算了 `H-L`，漏掉了前日收盘价与今日高低之间的缺口。
**修复**：改用标准 True Range `max(H-L, |H-prev_C|, |L-prev_C|)` + Wilder's EMA 平滑。
**影响文件**：`live_signal_runner.py`、`MeanReversion10mStrategy.py`

### FIX-2: 除零崩溃（严重）
**问题**：`vol_z40`、`rv_z` 在 rolling std=0 时产生 `inf`/`NaN`，导致信号逻辑失效。
**修复**：
- `atr` / `vstd40` / `rv_std` 用 `.replace(0, np.nan)` 保护
- `rv_z` 用 `.replace([np.inf, -np.inf], np.nan)` 清理
**影响文件**：`live_signal_runner.py`、`MeanReversion10mStrategy.py`

### FIX-3: 轮询效率（轻微 → 中）
**问题**：每 15 秒重复拉最后 100 根 K 线（含大量冗余）。
**修复**：记住 `last_fetch_time`，只用 `startTime` 取新增 K 线，避免重复传输。
**影响文件**：`live_signal_runner.py`

### FIX-4: 钉钉重试队列（中等）
**问题**：推送失败只打 warn 日志，信号正常发出但用户收不到通知。
**修复**：持久化 JSON 队列 (`dingtalk_queue.json`)，最多重试 5 次，每次间隔 30 秒。
**影响文件**：`live_signal_runner.py`

### FIX-5: 结算价查找（轻微）
**问题**：`expiry_time` 时间戳精确匹配 cache，如果网络抖动丢了一根 1m K 线就走 fallback。
**修复**：改为找最近一根 bar 做价格，若时间差过大打 warn。
**影响文件**：`live_signal_runner.py`

### FIX-6: 单实例锁（轻微）
**问题**：用 `wmic` + `taskkill` 在 Windows GBK 环境容易乱码，且强杀进程可能丢 active_trades。
**修复**：优先用 `psutil` 判断旧进程是否为本脚本再用 `terminate()` 优雅退出；无 psutil 时才降级为 `taskkill`。
**影响文件**：`live_signal_runner.py`

### FIX-7: 优雅退出（轻微）
**问题**：Ctrl+C 直接 break，来不及保存 active_trades。
**修复**：注册 `signal` handler + `atexit`，退出前强制保存 pending trades 和钉钉队列。
**影响文件**：`live_signal_runner.py`

### FIX-8: rv_z 基线窗口稳定性（中等）
**问题**：策略文件 rv_z baseline 的 `rolling(1440)` 默认无 `min_periods`，在 warmup 前 1440 根是全 NaN。
**修复**：统一设 `min_periods=RV_BASELINE_WIN`（1440），明确 warmup 边界。
**影响文件**：`MeanReversion10mStrategy.py`

### FIX-9: A4 策略首次 OOS 回测验证（新增）
**问题**：实盘跑的 A4 策略从未经过回测验证。
**修复**：新增 `user_data/notebooks/a4_backtest.py`，首次完整回测 A4。结果：A4 WilsonLB=56.78%（通过 55.56% 门槛），但 NORM 子策略 WilsonLB=55.39%（未通过）。

### FIX-10: 回测月报时区（轻微）
**问题**：月度分组用 `dt.to_period('M')` 时丢失 UTC 时区，边界 bar 可能划入错误月份。
**修复**：月分组前先将 UTC 时间戳转换为 `Period` 时保留 UTC 时区信息。
**影响文件**：`binary_option_backtest.py`

---

### 重新部署步骤

```powershell
# 1. 拉取最新代码（或复制覆盖以下文件）
# 覆盖文件列表：
#   - user_data/notebooks/live_signal_runner.py
#   - user_data/strategies/MeanReversion10mStrategy.py
#   - user_data/notebooks/binary_option_backtest.py
#   - user_data/notebooks/a4_backtest.py       (新增)
#   - OPERATIONS.md

# 2. 安装 psutil（如未安装，runner 会自动降级到 taskkill）
.venv\Scripts\pip install psutil

# 3. 验证 ATR 修复（可选）
.venv\Scripts\python -u user_data/notebooks/binary_option_backtest.py

# 4. 重新启动 runner
.\start_runner.ps1

# 5. 观察日志确认无 "inf" / "nan" 在信号字段中
```
