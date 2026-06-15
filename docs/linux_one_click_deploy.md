# Linux 一键部署脚本文档

本文档说明本地脚本 `tools/deploy_linux.ps1` 的用法。这个脚本从 Windows 本地项目目录打包代码、构建前端、上传到 Linux 服务器，并重启线上服务。

当前线上地址：

```text
http://115.190.218.128:3000
```

当前远程目录：

```text
/opt/btc-binary-options
```

## 最常用部署命令

在本地项目目录执行：

```powershell
Set-Location E:\python-binance

powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\deploy_linux.ps1 `
  -ServerHost "115.190.218.128" `
  -ServerUser "root" `
  -RemotePath "/opt/btc-binary-options"
```

脚本会提示输入 SSH 密码。不要把真实 SSH 密码写进文档或提交到 Git。

如果只是临时手动执行，也可以传入密码参数：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\deploy_linux.ps1 `
  -ServerHost "115.190.218.128" `
  -ServerUser "root" `
  -RemotePath "/opt/btc-binary-options" `
  -Password "<SSH密码>"
```

## 本地前置条件

本地机器需要：

- Windows PowerShell
- Node.js 和 npm
- Python
- 能连到服务器 SSH
- 本地 Python 能安装 `paramiko`

脚本会自动检查 `paramiko`，没有就执行：

```powershell
python -m pip install --user paramiko
```

## 脚本会做什么

`tools/deploy_linux.ps1` 会按顺序执行：

1. 本地检查 `npm`、`node`、`python` 是否存在。
2. 本地运行 `npm test`。
3. 本地运行 `npm run frontend:build`，生成前端静态资源到 `public/dashboard`。
4. 本地生成 `btc-binary-options-deploy.tar.gz`。
5. 通过 SSH/SFTP 上传到服务器 `/tmp/btc-binary-options-deploy.tar.gz`。
6. 解压到服务器 `/opt/btc-binary-options`。
7. 服务器安装或确认系统依赖：
   - `ca-certificates`
   - `curl`
   - `gnupg`
   - `build-essential`
   - `python3-venv`
   - `python3-pip`
8. 如果服务器 Node.js 低于 20，自动安装 Node.js 20 LTS。
9. 创建 Python 虚拟环境 `.venv`，安装：
   - `pandas`
   - `numpy`
   - `requests`
   - `scikit-learn`
   - `lightgbm`
   - `xgboost`
10. 服务器执行 Node 依赖安装：
    - `npm ci --omit=dev`
11. 服务器执行语法检查：
    - `node --check server.js`
    - `node --check auto_btc.js`
    - `python -m py_compile py/signal_btc.py py/price_proxy.py py/update_live_data.py py/collect_second_data.py`
12. 写入并重启 systemd 服务。
13. 执行部署后健康检查。

## 前端部署方式

前端是静态资源，不需要服务器跑 Vite。

部署时本地会先执行：

```powershell
npm run frontend:build
```

生成内容：

```text
public/dashboard/index.html
public/dashboard/assets/*
```

服务器上的 `server.js` 负责托管这些静态文件。

## 线上配置是否会丢

部署脚本会保留服务器上的关键运行配置：

```text
data/trade_config.json
data/prod_config.json
data/real_balance.json
```

也就是说，页面里改过的策略开关、金额、实盘/影子单配置，正常部署不会被本地包覆盖。

部署包会排除这些运行态文件：

```text
data/codex.db
data/codex.db-shm
data/codex.db-wal
data/trade_config.json
data/prod_config.json
data/real_balance.json
data/current_price.json
data/live_signals.json
data/live_data_update_status.json
data/second_data_status.json
logs/*
*.out
*.err
*.tmp
*.pyc
node_modules
.git
```

注意：脚本会删除并替换服务器上的：

```text
public/dashboard/assets
```

这是为了避免旧前端静态包残留。

## 线上服务结构

部署后会创建并启用三个 systemd 服务。

### btc-price.service

作用：

- 运行 `py/price_proxy.py`
- 持续拉取当前 BTC 价格
- 写入 `data/current_price.json`

查看状态：

```bash
systemctl status btc-price.service --no-pager
journalctl -u btc-price.service -n 100 --no-pager
```

### btc-second-data.service

作用：

- 运行 `py/collect_second_data.py`
- 采集 BTCUSDT 秒级成交/价格数据
- 写入 `data/btcusdt_1s_trades.csv`
- 维护 `data/second_data_status.json`

关键环境变量：

```text
SECOND_DATA_MARKET=futures
SECOND_DATA_SYMBOL=BTCUSDT
SECOND_DATA_INTERVAL_SEC=1
SECOND_DATA_RETENTION_DAYS=120
```

查看状态：

```bash
systemctl status btc-second-data.service --no-pager
journalctl -u btc-second-data.service -n 100 --no-pager
```

### btc-app.service

作用：

- 运行 `server.js`
- 监听 `0.0.0.0:3000`
- 托管前端页面
- 提供 `/api/*`
- 拉起 `py/signal_btc.py`
- 定时更新数据
- 管理实盘/影子单记录

关键环境变量：

```text
NODE_ENV=production
PORT=3000
APP_DIR=/opt/btc-binary-options
DATA_DIR=/opt/btc-binary-options/data
PYTHON_EXE=/opt/btc-binary-options/.venv/bin/python
SERVER_SIM_TRADING_ENABLED=0
ENABLE_SIGNAL_SHADOWS=0
ENABLE_LEGACY_TWO_MINUTE_LIVE=0
```

查看状态：

```bash
systemctl status btc-app.service --no-pager
journalctl -u btc-app.service -n 150 --no-pager
```

## 部署后检查

浏览器打开：

```text
http://115.190.218.128:3000
```

当前 Web 登录账号：

```text
账号：sl
密码：sl,123321
```

服务器本机检查：

```bash
curl -fsS http://127.0.0.1:3000/api/config
curl -fsS http://127.0.0.1:3000/api/data-health
curl -fsS http://127.0.0.1:3000/api/second-data-health
curl -fsS 'http://127.0.0.1:3000/api/signal?source=dashboard'
```

服务状态：

```bash
systemctl status btc-price.service --no-pager
systemctl status btc-second-data.service --no-pager
systemctl status btc-app.service --no-pager
```

## 当前策略形态

页面策略来自 `data/trade_config.json`。部署脚本不会强行覆盖线上配置。

当前系统支持的主要策略类型：

- `SAFE`：推荐稳健，正态尾部反转。
- `TAKER`：资金流过滤版，支持多个阈值档位和不同投数。
- `SECOND`：秒级正态档位。
- `SECOND_CHIP`：秒级筹码区反转。

不再需要的旧策略或旧展示项不应该出现在页面里，例如：

```text
BTC_30min
M1
M2
M3
30分钟二元期权
```

如果部署后页面还看到旧内容，优先检查浏览器缓存和服务器静态包：

```bash
ls -lh /opt/btc-binary-options/public/dashboard/assets
cat /opt/btc-binary-options/public/dashboard/index.html
```

## 常用脚本参数

### 跳过本地测试

只在确认本地测试刚跑过时使用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\deploy_linux.ps1 -SkipTests
```

### 跳过前端构建

只在确认 `public/dashboard` 已经是最新时使用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\deploy_linux.ps1 -SkipBuild
```

### 只打包不部署

用于检查部署包内容，不连接服务器：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\deploy_linux.ps1 -SkipRemoteInstall
```

生成文件：

```text
btc-binary-options-deploy.tar.gz
```

## 手动重启服务

如果只想重启线上服务，不重新部署：

```bash
systemctl restart btc-price.service
systemctl restart btc-second-data.service
systemctl restart btc-app.service
```

只重启主应用：

```bash
systemctl restart btc-app.service
```

## 常见问题

### 页面打不开

检查服务和端口：

```bash
systemctl status btc-app.service --no-pager
ss -lntp | grep ':3000'
journalctl -u btc-app.service -n 150 --no-pager
```

### 页面能打开但价格不动

检查价格服务：

```bash
systemctl status btc-price.service --no-pager
journalctl -u btc-price.service -n 100 --no-pager
cat /opt/btc-binary-options/data/current_price.json
```

### 秒级策略一直等待数据

检查秒级采集服务：

```bash
systemctl status btc-second-data.service --no-pager
journalctl -u btc-second-data.service -n 100 --no-pager
curl -fsS http://127.0.0.1:3000/api/second-data-health
tail -n 5 /opt/btc-binary-options/data/btcusdt_1s_trades.csv
```

### 没有信号

先确认数据是否健康：

```bash
curl -fsS http://127.0.0.1:3000/api/data-health
curl -fsS http://127.0.0.1:3000/api/second-data-health
curl -fsS 'http://127.0.0.1:3000/api/signal?source=debug'
```

如果数据健康但没有信号，可能是策略条件没有触发。比如 `SECOND_CHIP` 只在“从区间内刚突破到区间外”的瞬间触发，如果价格已经在区外，会等待重新进区后再突破。

### 实盘没有下单

按顺序检查：

```bash
curl -fsS http://127.0.0.1:3000/api/config
curl -fsS 'http://127.0.0.1:3000/api/signal?source=debug'
journalctl -u btc-app.service -n 150 --no-pager
```

重点看：

- `autoTrade_10m` 是否为 `true`
- `realTradingEnabled` 是否为 `true`
- 对应策略 `enabled` 是否为 `true`
- 对应策略 `tradeEnabled` 是否为 `true`
- 信号是否被数据健康、安全网、方向过滤、冷却或策略锁拦截

### npm 安装失败

当前依赖 `better-sqlite3` 需要 Node.js 20+。脚本会自动安装 Node.js 20 LTS；如果仍失败，检查：

```bash
node -v
npm -v
```

### Python 依赖失败

检查虚拟环境：

```bash
cd /opt/btc-binary-options
. .venv/bin/activate
python -V
python -m pip list
python -m py_compile py/signal_btc.py
```

## 部署成功标志

部署脚本最后应看到类似输出：

```text
DEPLOY_OK http://<server-hostname>:3000
Deploy complete: http://115.190.218.128:3000
```

同时三个服务应为 `active (running)`：

```text
btc-price.service
btc-second-data.service
btc-app.service
```
