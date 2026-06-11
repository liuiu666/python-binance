# Linux 一键部署说明

本文档对应当前新架构：只运行两套 10 分钟策略。

- `BTC_10min_SAFE`：推荐稳健，2m 聚合，POC/正态尾部反转，gap 30，无 taker 过滤。
- `BTC_10min_TAKER`：资金流过滤，同基础逻辑，加 taker align 过滤。

前端是静态资源。部署脚本会在本地执行 `npm run frontend:build`，生成 `public/dashboard`，服务器只负责托管这些静态文件，不需要在服务器重新跑 Vite。

## 一键部署

在本机项目目录执行：

```powershell
Set-Location E:\python-binance
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\deploy_linux.ps1 `
  -ServerHost "115.190.218.128" `
  -ServerUser "root" `
  -RemotePath "/opt/btc-binary-options"
```

脚本会提示输入 SSH 密码。也可以传 `-Password`，但不建议把密码写进命令历史。

## 脚本会做什么

1. 本地运行 `npm test`。
2. 本地运行 `npm run frontend:build`，打包 React 静态前端。
3. 生成 `btc-binary-options-deploy.tar.gz`。
4. 上传到服务器并解压到 `/opt/btc-binary-options`。
5. 如果服务器 Node 低于 20，自动安装 Node.js 20 LTS。
6. 创建 Python venv，并安装：
   - `pandas`
   - `numpy`
   - `requests`
   - `scikit-learn`
   - `lightgbm`
   - `xgboost`
7. 执行语法检查：
   - `node --check server.js`
   - `node --check auto_btc.js`
   - `python -m py_compile py/signal_btc.py py/price_proxy.py py/update_live_data.py`
8. 写入并启动 systemd 服务：
   - `btc-price.service`
   - `btc-app.service`
9. 调用 API 验证：
   - `/api/config`
   - `/api/data-health`
   - `/api/signal?source=dashboard`

## 服务结构

`btc-price.service`

- 运行 `py/price_proxy.py`
- 监听本机 `39870`
- 持续写入 `data/current_price.json`

`btc-app.service`

- 运行 `server.js`
- 监听 `0.0.0.0:3000`
- 自动拉起 `py/signal_btc.py`
- 自动执行行情数据更新
- 托管 `public/dashboard` 静态前端

脚本写入的关键环境变量：

```bash
PORT=3000
PYTHON_EXE=/opt/btc-binary-options/.venv/bin/python
SERVER_SIM_TRADING_ENABLED=1
ENABLE_SIGNAL_SHADOWS=0
ENABLE_LEGACY_TWO_MINUTE_LIVE=0
DATA_DIR=/opt/btc-binary-options/data
```

## 部署后检查

浏览器打开：

```text
http://115.190.218.128:3000
```

登录：

```text
账号：sl
密码：sl,123321
```

服务器上检查服务：

```bash
systemctl status btc-price.service --no-pager
systemctl status btc-app.service --no-pager
```

检查 API：

```bash
curl -s http://127.0.0.1:3000/api/config
curl -s http://127.0.0.1:3000/api/data-health
curl -s 'http://127.0.0.1:3000/api/signal?source=dashboard'
```

正常信号 API 里只应出现：

```text
BTC_10min_SAFE
BTC_10min_TAKER
```

不应出现：

```text
BTC_30min
BTC_10min
M1
M2
M3
```

## 常见问题

### npm 安装失败，提示 Node 版本不支持

当前依赖 `better-sqlite3@12.10.0` 要 Node 20+。一键部署脚本会自动安装 Node.js 20 LTS。

### 信号服务启动失败，提示找不到 python

Ubuntu 默认可能只有 `python3`。部署脚本会创建 `.venv`，并让 `server.js` 使用：

```bash
PYTHON_EXE=/opt/btc-binary-options/.venv/bin/python
```

### 页面能打开但没有价格

检查价格服务：

```bash
systemctl status btc-price.service --no-pager
journalctl -u btc-price.service -n 80 --no-pager
```

也可以看：

```bash
cat /opt/btc-binary-options/data/current_price.json
```

### 页面能打开但没有信号

检查主服务日志：

```bash
journalctl -u btc-app.service -n 120 --no-pager
```

检查信号文件：

```bash
cat /opt/btc-binary-options/data/live_signals.json
```

### 数据健康失败

手动触发一次数据更新：

```bash
curl -X POST http://127.0.0.1:3000/api/data-update/refresh
```

再看：

```bash
curl -s http://127.0.0.1:3000/api/data-health
```

## 只打包不部署

用于检查本地包内容：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\deploy_linux.ps1 -SkipRemoteInstall
```

这会生成：

```text
btc-binary-options-deploy.tar.gz
```

不会连接服务器。
