#!/usr/bin/env python3
"""使用本地部署凭据执行预定义的服务器只读检查。"""

import argparse
import json
from pathlib import Path

import paramiko


ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "tools" / "deploy_linux.local.json"
REMOTE_ROOT = "/opt/btc-binary-options"

# 仅允许预定义的只读检查，避免该工具被误用于修改服务器。
CHECKS = {
    "connect": "hostname; date -u; test -d /opt/btc-binary-options && echo app_dir_ok",
    "services": (
        "systemctl is-active btc-app.service btc-price.service btc-second-data.service "
        "btc-orderbook.service btc-auction-data.service; "
        "systemctl show btc-app.service btc-price.service btc-second-data.service "
        "btc-orderbook.service btc-auction-data.service "
        "--property=Id,ActiveState,SubState,MainPID,NRestarts --no-pager"
    ),
    "files": (
        "cd /opt/btc-binary-options && "
        "stat -c '%n|%s|%y' data/btcusdt_1m.csv data/live_signals.json "
        "data/prod_config.json data/trade_config.json py/signal_btc.py "
        "py/llm_predict_service.py 2>&1"
    ),
    "tests": (
        "cd /opt/btc-binary-options && "
        ".venv/bin/python -m py_compile py/signal_btc.py py/update_live_data.py && "
        "npm test"
    ),
    "signals": (
        "cd /opt/btc-binary-options && .venv/bin/python -c \""
        "import json; d=json.load(open('data/live_signals.json')); "
        "print(json.dumps({'snapshot_time':d.get('_snapshot_time'),"
        "'strategies':{k:{'signal':v.get('signal'),'time':v.get('time'),"
        "'reason':v.get('reason')} for k,v in d.items() if isinstance(v,dict)}},ensure_ascii=False))\""
    ),
    "data": (
        "cd /opt/btc-binary-options && .venv/bin/python -c \""
        "import json,pandas as pd; "
        "d=pd.read_csv('data/btcusdt_1m.csv'); "
        "t=pd.to_datetime(d['open_time'],utc=True,errors='coerce'); "
        "print(json.dumps({'rows':len(d),'columns':list(d.columns),'first':str(t.min()),"
        "'last':str(t.max()),'invalid_time':int(t.isna().sum()),"
        "'duplicates':int(t.duplicated().sum()),"
        "'missing_minutes':int(((t.sort_values().diff().dt.total_seconds().fillna(60)//60)-1).clip(lower=0).sum()),"
        "'nulls':{c:int(d[c].isna().sum()) for c in d.columns}},ensure_ascii=False))\""
    ),
    "config": (
        "cd /opt/btc-binary-options && .venv/bin/python -c \""
        "import json; "
        "p=json.load(open('data/prod_config.json')); "
        "t=json.load(open('data/trade_config.json')); "
        "print(json.dumps({'llm_prod':p.get('BTC_10min_LLM_GLM52'),"
        "'realTradingEnabled':t.get('realTradingEnabled'),"
        "'autoTrade_10m':t.get('autoTrade_10m'),"
        "'variants':[{'id':v.get('id'),'base':v.get('base'),'enabled':v.get('enabled'),"
        "'tradeEnabled':v.get('tradeEnabled'),'observationMode':v.get('observationMode'),"
        "'amount':v.get('amount')} for v in t.get('strategyVariants',[])]},ensure_ascii=False))\""
    ),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("checks", nargs="+", choices=sorted(CHECKS))
    args = parser.parse_args()

    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=config.get("host", "115.190.218.128"),
        username=config.get("user", "root"),
        password=config["password"],
        timeout=20,
        banner_timeout=30,
        auth_timeout=20,
    )
    try:
        for name in args.checks:
            print(f"=== {name} ===")
            _, stdout, stderr = client.exec_command(CHECKS[name], timeout=180)
            output = stdout.read().decode("utf-8", errors="replace")
            error = stderr.read().decode("utf-8", errors="replace")
            print(output.rstrip())
            if error:
                print(error.rstrip())
            print(f"exit={stdout.channel.recv_exit_status()}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
