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
    Run-Step "python second backtest tests" { python -m unittest test_second_backtest.py }
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
exclude_dirs = {".git", "node_modules", "__pycache__", ".pytest_cache"}
exclude_names = {
    "codex.db", "codex.db-shm", "codex.db-wal",
    "signal_btc.lock", "price_proxy.lock",
    "trade_config.json", "prod_config.json",
    "real_balance.json",
    "current_price.json", "live_signals.json", "live_data_update_status.json",
    "second_data_status.json"
}
exclude_suffixes = {".out", ".err", ".tmp", ".pyc"}
exclude_prefixes = [
    os.path.join(root, "data", "archive"),
    os.path.join(root, "logs"),
]
include_top = {
    "data", "docs", "frontend", "lib", "public", "py",
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
    if any(path.startswith(prefix) for prefix in exclude_prefixes):
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
import paramiko

host = os.environ["DEPLOY_HOST"]
user = os.environ["DEPLOY_USER"]
password = os.environ["DEPLOY_PASS"]
remote_path = os.environ["DEPLOY_REMOTE_PATH"].rstrip("/")
local_archive = os.environ["DEPLOY_ARCHIVE"]
remote_archive = "/tmp/btc-binary-options-deploy.tar.gz"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(hostname=host, username=user, password=password, timeout=30, banner_timeout=30, auth_timeout=30)

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
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl gnupg build-essential python3-venv python3-pip

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
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip wheel setuptools
python -m pip install pandas numpy requests scikit-learn lightgbm xgboost

echo "[5/8] install Node runtime deps"
rm -rf node_modules
npm ci --omit=dev

echo "[6/8] syntax checks"
node --check server.js
node --check auto_btc.js
. .venv/bin/activate
python -m py_compile py/signal_btc.py py/price_proxy.py py/update_live_data.py py/collect_second_data.py py/backtest_enhanced.py py/run_second_backtest.py py/run_second_research.py py/second_backtest/__init__.py py/second_backtest/data.py py/second_backtest/execution.py py/second_backtest/metrics.py py/second_backtest/strategies.py py/second_backtest/research.py

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
Environment=SECOND_DATA_INTERVAL_SEC=1
Environment=SECOND_DATA_RETENTION_DAYS=120
ExecStart=$APP_DIR/.venv/bin/python py/collect_second_data.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable btc-price.service btc-app.service btc-second-data.service
if [ -f /etc/systemd/system/btc-price.service.d/proxy.conf ]; then
  mkdir -p /etc/systemd/system/btc-second-data.service.d
  cp /etc/systemd/system/btc-price.service.d/proxy.conf /etc/systemd/system/btc-second-data.service.d/proxy.conf
  systemctl daemon-reload
fi
systemctl restart btc-price.service
systemctl restart btc-second-data.service
sleep 2
systemctl restart btc-app.service

echo "[8/8] health checks"
sleep 10
curl -fsS http://127.0.0.1:3000/api/config >/tmp/btc-config.json
curl -fsS http://127.0.0.1:3000/api/data-health >/tmp/btc-data-health.json
curl -fsS http://127.0.0.1:3000/api/second-data-health >/tmp/btc-second-data-health.json
curl -fsS 'http://127.0.0.1:3000/api/signal?source=dashboard' >/tmp/btc-signal.json
python3 - <<'PY'
import json
for name, path in [
    ("config", "/tmp/btc-config.json"),
    ("data_health", "/tmp/btc-data-health.json"),
    ("second_data", "/tmp/btc-second-data-health.json"),
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
    else:
        keys = [k for k in obj.keys() if k.startswith("BTC_")]
        gate = obj.get("_autoTradeSafetyGate", {})
        print("signal strategies=", keys, "safety_allow=", gate.get("allow"), "safety_blocked=", gate.get("blocked"))
PY

systemctl --no-pager --full status btc-price.service | sed -n '1,18p'
systemctl --no-pager --full status btc-second-data.service | sed -n '1,18p'
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
