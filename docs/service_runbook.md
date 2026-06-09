# 服务启动、关闭与检测

适用项目目录：`E:\codex`

## 1. 服务结构

这个项目的实盘服务是分层跑的：

- `server.js`
  - Node 主服务
  - 默认端口 `3000`
  - 对外提供网页、API、AutoJS 脚本下载
- `py/signal_btc.py`
  - 由 `server.js` 自动拉起的信号进程
  - 负责读模型、读 policy、生成 `data/live_signals.json`
- `py/update_live_data.py`
  - 由 `server.js` 定时触发
  - 负责更新 1m / taker / lsratio / funding 数据
- 平板 AutoJS
  - 从服务端拉取 `auto_btc.js` 或 loader
  - 通过 `/api/trade-audit` 回传心跳、下单、成交事件

重要说明：

- 启动 `server.js` 时，会自动启动 `signal_btc.py`
- 关闭 `server.js` 时，会自动停止它管理的 `signal_btc.py`
- 如果 `signal_btc.py` 意外退出，`server.js` 会在约 5 秒后自动重启它
- `signal_btc.py` 启动时才会读取 `prod_*_policy.json`，改 policy 后需要重启信号进程才会生效

## 2. 启动服务

### 2.1 前台启动

在 PowerShell 里执行：

```powershell
Set-Location E:\codex
npm start
```

等价命令：

```powershell
Set-Location E:\codex
node server.js
```

启动后预期行为：

- 本地服务监听 `http://127.0.0.1:3000`
- 自动触发一次数据更新
- 自动启动 `py/signal_btc.py`
- 自动刷新轻量报告

### 2.2 后台启动

如果想把服务放到后台跑：

```powershell
Start-Process -FilePath node -ArgumentList "server.js" -WorkingDirectory "E:\codex" -PassThru
```

返回的 `Id` 就是 Node 主进程 PID。

## 3. 关闭服务

### 3.1 关闭整个服务

如果是前台启动，直接在窗口按：

```text
Ctrl + C
```

如果是后台启动，执行：

```powershell
Get-CimInstance Win32_Process -Filter "name='node.exe'" |
  Where-Object { $_.CommandLine -like '*server.js*' } |
  Select-Object ProcessId, CommandLine
```

确认 PID 后关闭：

```powershell
Stop-Process -Id <PID> -Force
```

### 3.2 只重启信号进程

只想重启 `signal_btc.py`，不动 `server.js`：

```powershell
Get-CimInstance Win32_Process -Filter "name='python.exe'" |
  Where-Object { $_.CommandLine -like '*signal_btc.py*' } |
  Select-Object ProcessId, ParentProcessId, CommandLine
```

然后关闭这些 `python` 进程：

```powershell
Stop-Process -Id <PID1>,<PID2> -Force
```

`server.js` 会在约 5 秒后自动拉起新的 `signal_btc.py`。

### 3.3 不关服务，只暂停自动下单

保留网页、信号、数据更新，只把自动下单关掉：

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:3000/api/config `
  -ContentType "application/json" `
  -Body '{"autoTrade":false}'
```

重新打开：

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:3000/api/config `
  -ContentType "application/json" `
  -Body '{"autoTrade":true}'
```

## 4. 检测服务是否正常

建议按下面 4 层检查。

### 4.1 进程层

检查 Node 主服务：

```powershell
Get-CimInstance Win32_Process -Filter "name='node.exe'" |
  Where-Object { $_.CommandLine -like '*server.js*' } |
  Select-Object ProcessId, CreationDate, CommandLine
```

检查信号进程：

```powershell
Get-CimInstance Win32_Process -Filter "name='python.exe'" |
  Where-Object { $_.CommandLine -like '*signal_btc.py*' } |
  Select-Object ProcessId, ParentProcessId, CreationDate, CommandLine
```

### 4.2 端口层

检查本地 3000 端口：

```powershell
Test-NetConnection 127.0.0.1 -Port 3000
```

`TcpTestSucceeded` 为 `True` 说明端口已监听。

### 4.3 API 层

#### 运行时信息

```powershell
Invoke-RestMethod http://127.0.0.1:3000/api/runtime | ConvertTo-Json -Depth 6
```

重点看：

- `port`
- `urls`
- `tabletUrl`
- `scriptUrl`
- `loaderUrl`

#### 信号服务状态

```powershell
Invoke-RestMethod http://127.0.0.1:3000/api/signal-service | ConvertTo-Json -Depth 6
```

重点看：

- `running` 应为 `true`
- `pid` 不应为空
- `dataUpdate.running` 长时间不应卡住

#### 当前信号

```powershell
Invoke-RestMethod http://127.0.0.1:3000/api/signal | ConvertTo-Json -Depth 8
```

重点看：

- `BTC_10min.live_model` 是否为 `true`
- `BTC_10min.policy_name` 是否是预期 policy
- `BTC_10min.data_health_blocked` 是否为 `false`

#### 数据健康

```powershell
Invoke-RestMethod http://127.0.0.1:3000/api/data-health | ConvertTo-Json -Depth 8
```

重点看：

- `overall` 最好是 `ok`
- 各数据源是否出现过期或 gap

#### 自动下单状态

```powershell
Invoke-RestMethod http://127.0.0.1:3000/api/config | ConvertTo-Json -Depth 6
```

重点看：

- `autoTrade`
- `amount`
- `minConfidence`
- `maxActionableLagMs`

#### 平板状态

```powershell
Invoke-RestMethod http://127.0.0.1:3000/api/tablet-diagnostics | ConvertTo-Json -Depth 8
```

重点看：

- `status`
- `checks.heartbeatOnline`
- `checks.balanceRecent`
- `nextAction`

常见正常状态：

- `autojs_online_waiting_for_order_done`
- `has_order_done`

### 4.4 文件与日志层

重点文件：

- `data/live_signals.json`
  - 当前最新信号快照
- `data/live_data_update_status.json`
  - 最近一次数据更新结果
- `data/strategy_health_report.json`
  - 健康报告
- `data/trade_audit.jsonl`
  - 平板心跳、下单、成交、服务事件
- `data/real_balance.json`
  - 最近余额

重点日志：

- `.sig.out`
  - `signal_btc.py` 标准输出
- `.sig.err`
  - `signal_btc.py` 错误日志
- `.reports.out`
  - 报告刷新输出
- `.reports.err`
  - 报告刷新错误
- `.data_update.out`
  - 数据更新输出
- `.data_update.err`
  - 数据更新错误

常用查看命令：

```powershell
Get-Content E:\codex\data\live_signals.json -Raw
Get-Content E:\codex\data\live_data_update_status.json -Raw
Get-Content E:\codex\data\strategy_health_report.json -Raw
Get-Content E:\codex\data\trade_audit.jsonl -Tail 50
Get-Content E:\codex\.sig.out -Tail 50
Get-Content E:\codex\.sig.err -Tail 50
```

## 5. 手动刷新

### 5.1 手动刷新数据

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:3000/api/data-update/refresh
```

### 5.2 手动刷新报告

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:3000/api/reports/refresh
```

## 6. 平板接入

先获取服务地址：

```powershell
Invoke-RestMethod http://127.0.0.1:3000/api/runtime | ConvertTo-Json -Depth 6
```

平板常用地址：

- `tabletPageUrl`
  - 浏览器打开，检查平板是否能访问服务
- `loaderUrl`
  - AutoJS loader 地址
- `scriptUrl`
  - 最新 `auto_btc.js`

如果平板接不上，优先看：

- `/api/tablet-diagnostics`
- `data/trade_audit.jsonl`

## 7. 常见问题

### 7.1 服务起不来

先看：

```powershell
Test-NetConnection 127.0.0.1 -Port 3000
Get-Content E:\codex\.sig.err -Tail 100
Get-Content E:\codex\.reports.err -Tail 100
```

常见原因：

- 3000 端口被占用
- `python` 不在 PATH
- 数据文件损坏或缺失

### 7.2 数据过期

先看：

```powershell
Invoke-RestMethod http://127.0.0.1:3000/api/data-health | ConvertTo-Json -Depth 8
Get-Content E:\codex\data\live_data_update_status.json -Raw
```

再手动触发一次：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:3000/api/data-update/refresh
```

### 7.3 平板在线但不下单

按顺序检查：

1. `/api/config` 里的 `autoTrade` 是否为 `true`
2. `/api/tablet-diagnostics` 里的 `heartbeatOnline` 是否为 `true`
3. `/api/signal` 里是否真的有可交易信号
4. `trade_audit.jsonl` 里有没有 `signal_tradeable` / `order_attempt` / `order_done`

### 7.4 改了 policy 但没生效

这是正常现象，因为 `signal_btc.py` 只在启动时读取 policy。

处理方式：

1. 改 policy 文件
2. 重启 `signal_btc.py`
3. 再检查 `data/live_signals.json` 里的 `policy_name`

## 8. 当前项目里最常用的 3 条命令

启动：

```powershell
Set-Location E:\codex
npm start
```

看状态：

```powershell
Invoke-RestMethod http://127.0.0.1:3000/api/signal-service | ConvertTo-Json -Depth 6
Invoke-RestMethod http://127.0.0.1:3000/api/tablet-diagnostics | ConvertTo-Json -Depth 8
Invoke-RestMethod http://127.0.0.1:3000/api/data-health | ConvertTo-Json -Depth 8
```

停自动下单但不关服务：

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:3000/api/config `
  -ContentType "application/json" `
  -Body '{"autoTrade":false}'
```
