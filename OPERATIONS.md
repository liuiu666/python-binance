# 运行手册 (Operations Guide)

本项目当前生产形态：A3 均值回归 10 分钟二元期权策略 + 实时信号推送 + 订单流数据录制。

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
