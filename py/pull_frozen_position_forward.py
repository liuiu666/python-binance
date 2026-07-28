"""Download raw forward-validation files without running research remotely."""

from __future__ import annotations

import os
import json
import shlex
import tarfile
from pathlib import Path

import paramiko
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tmp" / "frozen_position_forward"
REMOTE_ROOT = os.environ.get("FORWARD_REMOTE_ROOT", "/opt/btc-binary-options/data")
FILES = (
    "btcusdt_1s_trades.csv",
    "btcusdt_orderbook_1s.csv",
    "btcusdt_open_interest.csv",
    "btcusdt_global_lsratio.csv",
    "btcusdt_top_account_lsratio.csv",
    "btcusdt_lsratio.csv",
    "btcusdt_taker.csv",
    "btcusdt_funding.csv",
)
CONFIG = ROOT / "data" / "frozen_position_build_up_v1.json"


def main() -> None:
    host = os.environ.get("DEPLOY_HOST", "115.190.218.128")
    user = os.environ.get("DEPLOY_USER", "root")
    password = os.environ.get("DEPLOY_PASS")
    if not password:
        raise RuntimeError("DEPLOY_PASS is required")
    OUT.mkdir(parents=True, exist_ok=True)
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    cutoff = pd.Timestamp(config["frozenAt"]) - pd.Timedelta(minutes=10)
    cutoff_text = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, password=password, timeout=20, banner_timeout=40, auth_timeout=20)
    remote_stage = "/tmp/frozen-position-forward"
    remote_archive = "/tmp/frozen-position-forward.tar.gz"
    raw_files = ("btcusdt_1s_trades.csv", "btcusdt_orderbook_1s.csv")
    context_files = tuple(name for name in FILES if name not in raw_files)
    commands = [f"rm -rf {remote_stage}", f"mkdir -p {remote_stage}"]
    for name in raw_files:
        source = f"{REMOTE_ROOT}/{name}"
        destination = f"{remote_stage}/{name}"
        commands.append(
            f"awk -F, -v cutoff={shlex.quote(cutoff_text)} "
            f"'NR == 1 || $1 >= cutoff' {shlex.quote(source)} > {shlex.quote(destination)}"
        )
    for name in context_files:
        commands.append(f"cp {shlex.quote(f'{REMOTE_ROOT}/{name}')} {shlex.quote(f'{remote_stage}/{name}')}")
    commands.append(f"tar -czf {remote_archive} -C {remote_stage} .")
    _, stdout, stderr = client.exec_command("set -e; " + "; ".join(commands), timeout=90)
    error_text = stderr.read().decode("utf-8", errors="replace")
    if stdout.channel.recv_exit_status() != 0:
        raise RuntimeError(error_text)
    sftp = client.open_sftp()
    try:
        local_archive = OUT / "forward.tar.gz"
        temporary = OUT / "forward.tar.gz.tmp"
        sftp.get(remote_archive, str(temporary))
        temporary.replace(local_archive)
        with tarfile.open(local_archive, "r:gz") as archive:
            archive.extractall(OUT, filter="data")
        for name in FILES:
            destination = OUT / name
            print(f"{name}: {destination.stat().st_size} bytes")
    finally:
        sftp.close()
        client.close()


if __name__ == "__main__":
    main()
