"""Download selected raw auction partitions for local-only research."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import paramiko


ROOT = Path(__file__).resolve().parents[1]
REMOTE_ROOT = "/opt/btc-binary-options/data/auction/BTCUSDT/futures"
DEFAULT_OUTPUT = ROOT / "tmp" / "auction_raw"
STREAMS = ("trades", "depth_updates", "force_orders")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dates", nargs="+", help="UTC dates such as 2026-07-15")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    password = os.environ.get("DEPLOY_PASS")
    if not password:
        raise RuntimeError("DEPLOY_PASS is required")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        os.environ.get("DEPLOY_HOST", "115.190.218.128"),
        username=os.environ.get("DEPLOY_USER", "root"),
        password=password,
        timeout=20,
        banner_timeout=40,
        auth_timeout=20,
    )
    sftp = client.open_sftp()
    try:
        for day in args.dates:
            for stream in STREAMS:
                remote = f"{REMOTE_ROOT}/{stream}/date={day}/events.jsonl.gz"
                destination = args.output / stream / f"date={day}" / "events.jsonl.gz"
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_suffix(".gz.tmp")
                try:
                    remote_size = sftp.stat(remote).st_size
                except FileNotFoundError:
                    print(f"missing {stream} {day}", flush=True)
                    continue
                if destination.exists() and destination.stat().st_size > 0:
                    state = "complete" if destination.stat().st_size == remote_size else "existing_snapshot"
                    print(f"{state} {stream} {day} {destination.stat().st_size}", flush=True)
                    continue
                sftp.get(remote, str(temporary))
                if temporary.stat().st_size < remote_size:
                    raise RuntimeError(f"incomplete download: {stream} {day}")
                temporary.replace(destination)
                print(f"downloaded {stream} {day} {remote_size}", flush=True)
    finally:
        sftp.close()
        client.close()


if __name__ == "__main__":
    main()
