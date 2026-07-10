param(
    [string]$ServerHost = "115.190.218.128",
    [string]$ServerUser = "root",
    [string]$RemotePath = "/opt/btc-binary-options",
    [string]$Password = "",
    [switch]$SkipTests,
    [switch]$SkipBuild,
    [switch]$SkipRemoteInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $RepoRoot

function Run-Step([string]$Name, [scriptblock]$Body) {
    Write-Host ""
    Write-Host "== $Name ==" -ForegroundColor Cyan
    & $Body
}

function Require-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name is required but was not found in PATH."
    }
}

function Get-PlainPassword {
    if ($Password) { return $Password }
    $secure = Read-Host "SSH password for $ServerUser@$ServerHost" -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
}

Require-Command npm
Require-Command node
Require-Command python

Run-Step "check local Python paramiko" {
    $hasParamiko = python -c "import importlib.util; print('1' if importlib.util.find_spec('paramiko') else '0')"
    if ($hasParamiko.Trim() -ne "1") {
        python -m pip install --user paramiko
    }
}

if (-not $SkipTests) {
    Run-Step "npm test" { npm test }
    Run-Step "python signal and second backtest tests" { python -m unittest test_signal_modules.py test_second_backtest.py }
}

if (-not $SkipBuild) {
    Run-Step "npm run frontend:build" { npm run frontend:build }
}

$ArchivePath = Join-Path $RepoRoot "btc-binary-options-deploy.tar.gz"

Run-Step "create deploy archive" {
    $env:DEPLOY_ARCHIVE = $ArchivePath
    @'
import os
import tarfile

root = os.getcwd()
out = os.environ["DEPLOY_ARCHIVE"]
exclude_dirs = {
    ".git", "node_modules", "__pycache__", ".pytest_cache",
    "tmp", "logs", ".venv"
}
exclude_names = {
    "codex.db", "codex.db-shm", "codex.db-wal",
    "signal_btc.lock", "price_proxy.lock",
    "trade_config.json", "prod_config.json",
    "real_balance.json",
    "current_price.json", "live_signals.json", "live_data_update_status.json",
    "second_data_status.json", "orderbook_status.json", "orderbook_prediction_status.json",
    "btcusdt_1s_trades.csv", "btcusdt_orderbook_1s.csv",
    "btcusdt_1m.csv", "btcusdt_taker.csv", "btcusdt_lsratio.csv", "btcusdt_funding.csv"
}
exclude_suffixes = {
    ".out", ".err", ".tmp", ".pyc",
    ".db", ".db-shm", ".db-wal", ".jsonl"
}
exclude_rel_prefixes = [
    ("data",),
    ("frontend", "src", "data"),
]
include_top = {
    "docs", "frontend", "lib", "public", "py",
    "test", "tools"
}
include_files = {
    "auto_btc.js", "package.json", "package-lock.json",
    "server.js", "service.cmd", "service.ps1", "start.ps1", ".gitignore"
}

def should_include(path):
    rel = os.path.relpath(path, root)
    if rel == os.path.basename(out):
        return False
    parts = rel.split(os.sep)
    if any(part in exclude_dirs for part in parts):
        return False
    if any(tuple(parts[:len(prefix)]) == prefix for prefix in exclude_rel_prefixes):
        return False
    name = os.path.basename(path)
    if name in exclude_names:
        return False
    if any(name.endswith(suffix) for suffix in exclude_suffixes):
        return False
    if os.path.isdir(path):
        return True
    return parts[0] in include_top or rel in include_files

if os.path.exists(out):
    os.remove(out)

count = 0
with tarfile.open(out, "w:gz") as tar:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if should_include(os.path.join(dirpath, d))]
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            if should_include(path):
                tar.add(path, arcname=os.path.relpath(path, root))
                count += 1

print(f"archive={out}")
print(f"files={count}")
print(f"size_mb={os.path.getsize(out) / 1024 / 1024:.2f}")
'@ | python -
}

if ($SkipRemoteInstall) {
    Write-Host ""
    Write-Host "Archive ready: $ArchivePath" -ForegroundColor Green
    exit 0
}

$PlainPassword = Get-PlainPassword
$env:DEPLOY_HOST = $ServerHost
$env:DEPLOY_USER = $ServerUser
$env:DEPLOY_PASS = $PlainPassword
$env:DEPLOY_REMOTE_PATH = $RemotePath
$env:DEPLOY_ARCHIVE = $ArchivePath

Run-Step "upload and install on server" {
    $deployClient = @'
import os
import posixpath
import sys
import time
import paramiko

host = os.environ["DEPLOY_HOST"]
user = os.environ["DEPLOY_USER"]
password = os.environ["DEPLOY_PASS"]
remote_path = os.environ["DEPLOY_REMOTE_PATH"].rstrip("/")
local_archive = os.environ["DEPLOY_ARCHIVE"]
remote_archive = "/tmp/btc-binary-options-deploy.tar.gz"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
last_error = None
for attempt in range(1, 7):
    try:
        client.connect(
            hostname=host,
            username=user,
            password=password,
            timeout=30,
            banner_timeout=60,
            auth_timeout=30,
        )
        print(f"ssh connected on attempt {attempt}")
        break
    except Exception as exc:
        last_error = exc
        wait_sec = min(10 * attempt, 30)
        print(f"ssh connect attempt {attempt}/6 failed: {exc}; retry in {wait_sec}s")
        time.sleep(wait_sec)
else:
    raise last_error

sftp = client.open_sftp()
last = {"value": 0}

def progress(done, total):
    if done == total or done - last["value"] >= 5 * 1024 * 1024:
        print(f"uploaded {done}/{total} ({done / total * 100:.1f}%)")
        last["value"] = done

sftp.put(local_archive, remote_archive, callback=progress)
sftp.close()

remote_script = r'''#!/usr/bin/env bash
set -euo pipefail

APP_DIR="__REMOTE_PATH__"
ARCHIVE="__REMOTE_ARCHIVE__"
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"

echo "[1/8] prepare app directory"
mkdir -p "$APP_DIR"
CONFIG_BACKUP="$(mktemp -d)"
for f in real_balance.json trade_config.json prod_config.json; do
  if [ -f "$APP_DIR/data/$f" ]; then
    cp "$APP_DIR/data/$f" "$CONFIG_BACKUP/$f"
  fi
done
rm -rf "$APP_DIR/public/dashboard/assets"
tar -xzf "$ARCHIVE" -C "$APP_DIR"
mkdir -p "$APP_DIR/data"
for f in real_balance.json trade_config.json prod_config.json; do
  if [ -f "$CONFIG_BACKUP/$f" ]; then
    cp "$CONFIG_BACKUP/$f" "$APP_DIR/data/$f"
  fi
done
rm -rf "$CONFIG_BACKUP"
cd "$APP_DIR"

echo "[2/8] ensure system packages"
if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1 && python3 -m venv --help >/dev/null 2>&1; then
  echo "system packages already present; skip apt"
else
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl gnupg build-essential python3-venv python3-pip
fi

if [ "$NODE_MAJOR" -lt 20 ]; then
  echo "[3/8] install Node.js 20 LTS"
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs
else
  echo "[3/8] Node.js already >=20"
fi
node -v
npm -v

echo "[4/8] install Python runtime"
PY_RUNTIME_MARKER=".deploy-cache/python-runtime-v2"
mkdir -p .deploy-cache
if [ -x .venv/bin/python ] && .venv/bin/python - <<'PY' >/dev/null 2>&1
import pandas, numpy, requests, websocket, sklearn, lightgbm, xgboost
PY
then
  echo "Python runtime already ready; skip pip install"
  [ -f "$PY_RUNTIME_MARKER" ] || date -u +"%Y-%m-%dT%H:%M:%SZ" > "$PY_RUNTIME_MARKER"
else
  python3 -m venv .venv
  . .venv/bin/activate
  python -m pip install --upgrade pip wheel setuptools
  python -m pip install pandas numpy requests websocket-client python-socks scikit-learn lightgbm xgboost
  date -u +"%Y-%m-%dT%H:%M:%SZ" > "$PY_RUNTIME_MARKER"
fi

echo "[5/8] install Node runtime deps"
LOCK_HASH="$(sha256sum package-lock.json | awk '{print $1}')"
LOCK_MARKER=".deploy-cache/package-lock.sha256"
if [ -d node_modules ] && node -e "require('better-sqlite3')" >/dev/null 2>&1 && { [ ! -f "$LOCK_MARKER" ] || [ "$(cat "$LOCK_MARKER")" = "$LOCK_HASH" ]; }; then
  echo "Node runtime deps already match package-lock; skip npm ci"
  echo "$LOCK_HASH" > "$LOCK_MARKER"
else
  npm ci --omit=dev
  echo "$LOCK_HASH" > "$LOCK_MARKER"
fi

echo "[6/8] syntax checks"
node --check server.js
node --check auto_btc.js
. .venv/bin/activate
python -m py_compile py/liquidity_v2_core.py py/run_liquidity_v2_backtest.py py/signal_btc.py py/signal_health.py py/signal_io.py py/signal_lock.py py/signal_paths.py py/signal_runtime_cache.py py/signal_state.py py/price_proxy.py py/update_live_data.py py/collect_second_data.py py/collect_orderbook_data.py py/backtest_enhanced.py py/run_second_backtest.py py/run_second_research.py py/research_normal_state_v1.py py/research_normal_state_v6.py py/research_yellow_revert_filters.py py/second_backtest/__init__.py py/second_backtest/data.py py/second_backtest/dynamic_zone.py py/second_backtest/execution.py py/second_backtest/incident_filter.py py/second_backtest/metrics.py py/second_backtest/strategies.py py/second_backtest/research.py py/second_backtest/normal_state_v11.py

echo "[7/8] write systemd services"
NODE_BIN="$(command -v node)"
cat >/etc/systemd/system/btc-price.service <<SERVICE
[Unit]
Description=BTC price proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=APP_DIR=$APP_DIR
Environment=DATA_DIR=$APP_DIR/data
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/python py/price_proxy.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

cat >/etc/systemd/system/btc-app.service <<SERVICE
[Unit]
Description=BTC binary options app
After=network-online.target btc-price.service
Wants=network-online.target btc-price.service

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=NODE_ENV=production
Environment=PORT=3000
Environment=APP_DIR=$APP_DIR
Environment=DATA_DIR=$APP_DIR/data
Environment=PYTHON_EXE=$APP_DIR/.venv/bin/python
Environment=SERVER_SIM_TRADING_ENABLED=0
Environment=ENABLE_SIGNAL_SHADOWS=0
Environment=ENABLE_LEGACY_TWO_MINUTE_LIVE=0
Environment=PYTHONUNBUFFERED=1
Environment=OMP_NUM_THREADS=1
Environment=OPENBLAS_NUM_THREADS=1
Environment=MKL_NUM_THREADS=1
Environment=NUMEXPR_NUM_THREADS=1
ExecStart=$NODE_BIN server.js
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

cat >/etc/systemd/system/btc-second-data.service <<SERVICE
[Unit]
Description=BTC second-level trade data collector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=APP_DIR=$APP_DIR
Environment=DATA_DIR=$APP_DIR/data
Environment=PYTHONUNBUFFERED=1
Environment=SECOND_DATA_MARKET=futures
Environment=SECOND_DATA_SYMBOL=BTCUSDT
Environment=SECOND_DATA_MODE=websocket
Environment=SECOND_DATA_INTERVAL_SEC=5
Environment=SECOND_DATA_RETENTION_DAYS=120
Environment=SECOND_DATA_HTTP_TIMEOUT=8
Environment=SECOND_DATA_FAPI_BASES=https://fapi.binance.com
Environment=SECOND_DATA_FINALIZE_DELAY_SEC=5
Environment=SECOND_DATA_RATE_LIMIT_BACKOFF_SEC=300
Environment=SECOND_DATA_STATUS_INTERVAL_SEC=2
Environment=SECOND_DATA_STARTUP_BACKFILL_MINUTES=15
Environment=SECOND_DATA_BACKFILL_SLEEP_SEC=0.03
Environment=SECOND_DATA_WS_URL=wss://fstream.binance.com/stream?streams=btcusdt@trade/btcusdt@depth20@500ms
Environment=SECOND_DATA_WS_FLUSH_INTERVAL_SEC=1
Environment=SECOND_DATA_WS_FLUSH_MAX_TRADES=5000
Environment=SECOND_DATA_WS_PING_INTERVAL_SEC=180
Environment=SECOND_DATA_WS_PING_TIMEOUT_SEC=60
Environment=SECOND_DATA_GAP_REPAIR_INTERVAL_SEC=120
Environment=SECOND_DATA_GAP_REPAIR_LOOKBACK_SEC=900
Environment=SECOND_DATA_GAP_REPAIR_MERGE_GAP_SEC=20
Environment=SECOND_DATA_GAP_REPAIR_MAX_RANGE_SEC=240
Environment=SECOND_DATA_GAP_REPAIR_MAX_RANGES=8
Environment=SECOND_DATA_FILL_EMPTY_SECONDS=1
Environment=SECOND_DATA_FILL_EMPTY_MAX_GAP_SEC=3
Environment=SECOND_DATA_FILE_REPAIR_INTERVAL_SEC=1800
ExecStart=$APP_DIR/.venv/bin/python py/collect_second_data.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

cat >/etc/systemd/system/btc-orderbook.service <<SERVICE
[Unit]
Description=BTC order-book feature collector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=APP_DIR=$APP_DIR
Environment=DATA_DIR=$APP_DIR/data
Environment=PYTHONUNBUFFERED=1
Environment=ORDERBOOK_SYMBOL=BTCUSDT
Environment=ORDERBOOK_LEVELS=20
Environment=ORDERBOOK_UPDATE_MS=1000
ExecStart=$APP_DIR/.venv/bin/python py/collect_orderbook_data.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable btc-price.service btc-app.service btc-second-data.service btc-orderbook.service
mkdir -p /etc/systemd/system/btc-second-data.service.d
find /etc/systemd/system/btc-second-data.service.d -type f ! -name proxy.conf -delete
if [ -f /etc/systemd/system/btc-price.service.d/proxy.conf ]; then
  cp /etc/systemd/system/btc-price.service.d/proxy.conf /etc/systemd/system/btc-second-data.service.d/proxy.conf
  mkdir -p /etc/systemd/system/btc-orderbook.service.d
  cp /etc/systemd/system/btc-price.service.d/proxy.conf /etc/systemd/system/btc-orderbook.service.d/proxy.conf
fi
systemctl daemon-reload
systemctl restart btc-price.service
systemctl restart btc-second-data.service
systemctl restart btc-orderbook.service
sleep 2
systemctl restart btc-app.service

echo "[8/8] health checks"
sleep 10
curl --max-time 20 -fsS http://127.0.0.1:3000/api/config >/tmp/btc-config.json
curl --max-time 20 -fsS http://127.0.0.1:3000/api/data-health >/tmp/btc-data-health.json
curl --max-time 20 -fsS http://127.0.0.1:3000/api/second-data-health >/tmp/btc-second-data-health.json
curl --max-time 20 -fsS http://127.0.0.1:3000/api/orderbook-health >/tmp/btc-orderbook-health.json
curl --max-time 20 -fsS 'http://127.0.0.1:3000/api/signal?source=dashboard' >/tmp/btc-signal.json
python3 - <<'PY'
import json
for name, path in [
    ("config", "/tmp/btc-config.json"),
    ("data_health", "/tmp/btc-data-health.json"),
    ("second_data", "/tmp/btc-second-data-health.json"),
    ("orderbook", "/tmp/btc-orderbook-health.json"),
    ("signal", "/tmp/btc-signal.json"),
]:
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    if name == "config":
        print("config", obj)
    elif name == "data_health":
        print("data_health allow=", obj.get("allow"), "blocked=", obj.get("blocked"), "reasons=", obj.get("reasons"))
    elif name == "second_data":
        status = obj.get("status", {})
        print("second_data ok=", obj.get("ok"), "ageMs=", obj.get("ageMs"), "rows=", status.get("rows"), "last=", status.get("last_ts"))
    elif name == "orderbook":
        status = obj.get("status", {})
        print("orderbook ok=", obj.get("ok"), "ageMs=", obj.get("ageMs"), "rows=", status.get("rows"), "last=", status.get("last_ts"))
    else:
        keys = [k for k in obj.keys() if k.startswith("BTC_")]
        gate = obj.get("_autoTradeSafetyGate", {})
        print("signal strategies=", keys, "safety_allow=", gate.get("allow"), "safety_blocked=", gate.get("blocked"))
PY

systemctl --no-pager --full status btc-price.service | sed -n '1,18p'
systemctl --no-pager --full status btc-second-data.service | sed -n '1,18p'
systemctl --no-pager --full status btc-orderbook.service | sed -n '1,18p'
systemctl --no-pager --full status btc-app.service | sed -n '1,18p'
rm -f "$ARCHIVE"
echo "DEPLOY_OK http://$HOSTNAME:3000"
'''
remote_script = remote_script.replace("__REMOTE_PATH__", remote_path).replace("__REMOTE_ARCHIVE__", remote_archive)

stdin, stdout, stderr = client.exec_command(remote_script, timeout=1800)
for line in iter(stdout.readline, ""):
    print(line, end="")
err = stderr.read().decode("utf-8", "replace")
if err:
    print("STDERR:", err, file=sys.stderr)
code = stdout.channel.recv_exit_status()
client.close()
sys.exit(code)
'@
    $deployClient | python -
    if ($LASTEXITCODE -ne 0) {
        throw "remote deploy failed with exit code $LASTEXITCODE"
    }
}

Remove-Item Env:\DEPLOY_PASS -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "Deploy complete: http://$ServerHost`:3000" -ForegroundColor Green
