"""Pull an immutable, read-only V14 research snapshot from production.

The script only snapshots explicitly named files. It creates a temporary copy
under remote ``/tmp`` for consistency and compression, but never mutates
production data or overwrites a completed local snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import paramiko


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REMOTE_ROOT = "/opt/btc-binary-options/data"
RAW_FILES = (
    "btcusdt_1s_trades.csv",
    "btcusdt_orderbook_1s.csv",
    "trade_config.json",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def trim_partial_csv_row(path: Path) -> None:
    """Drop a trailing partial append without rewriting the full CSV."""
    with path.open("r+b") as handle:
        size = handle.seek(0, os.SEEK_END)
        if size == 0:
            return
        handle.seek(-1, os.SEEK_END)
        if handle.read(1) == b"\n":
            return
        probe = min(size, 1024 * 1024)
        handle.seek(-probe, os.SEEK_END)
        tail = handle.read(probe)
        last_newline = tail.rfind(b"\n")
        if last_newline < 0:
            raise RuntimeError(f"No complete CSV row found in trailing {probe} bytes: {path}")
        handle.truncate(size - probe + last_newline + 1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        default=str(ROOT / "tmp" / f"v14_forward_{datetime.now():%Y%m%d}"),
        help="New local snapshot directory. Existing completed snapshots are not overwritten.",
    )
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output = Path(args.out).resolve()
    manifest_path = output / "snapshot_manifest.json"
    if manifest_path.exists():
        raise RuntimeError(f"Completed snapshot already exists: {manifest_path}")

    host = os.environ.get("DEPLOY_HOST", "115.190.218.128")
    user = os.environ.get("DEPLOY_SSH_USER") or os.environ.get("DEPLOY_USER", "root")
    password = os.environ.get("DEPLOY_SSH_PASSWORD") or os.environ.get("DEPLOY_PASS")
    if not password:
        raise RuntimeError("Set DEPLOY_SSH_PASSWORD or DEPLOY_PASS")

    output.mkdir(parents=True, exist_ok=True)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host,
        username=user,
        password=password,
        timeout=20,
        banner_timeout=40,
        auth_timeout=20,
    )

    records: list[dict[str, object]] = []
    capture_id = uuid.uuid4().hex[:12]
    remote_stage = f"/tmp/v14-research-{capture_id}"
    remote_archive = f"/tmp/v14-research-{capture_id}.tar.gz"
    if not remote_stage.startswith("/tmp/v14-research-"):
        raise RuntimeError("Unsafe remote staging path")
    try:
        copy_commands = [f"mkdir -p {shlex.quote(remote_stage)}"]
        for name in RAW_FILES:
            source = f"{args.remote_root.rstrip('/')}/{name}"
            destination = f"{remote_stage}/{name}"
            copy_commands.append(f"cp -- {shlex.quote(source)} {shlex.quote(destination)}")
        copy_commands.append(
            f"tar -czf {shlex.quote(remote_archive)} -C {shlex.quote(remote_stage)} ."
        )
        _, stdout, stderr = client.exec_command("set -e; " + "; ".join(copy_commands), timeout=120)
        exit_code = stdout.channel.recv_exit_status()
        error_text = stderr.read().decode("utf-8", errors="replace").strip()
        if exit_code != 0:
            raise RuntimeError(f"Remote snapshot failed: {error_text}")

        sftp = client.open_sftp()
        try:
            archive = output / "raw_snapshot.tar.gz"
            archive_tmp = output / "raw_snapshot.tar.gz.tmp"
            archive_tmp.unlink(missing_ok=True)
            sftp.get(remote_archive, str(archive_tmp))
            archive_tmp.replace(archive)
            with tarfile.open(archive, "r:gz") as bundle:
                bundle.extractall(output, filter="data")

            for name in RAW_FILES:
                local = output / name
                if name.endswith(".csv"):
                    trim_partial_csv_row(local)
                staged_stat = sftp.stat(f"{remote_stage}/{name}")
                records.append(
                    {
                        "name": name,
                        "remote": f"{args.remote_root.rstrip('/')}/{name}",
                        "bytes": local.stat().st_size,
                        "capturedRemoteBytes": staged_stat.st_size,
                        "remoteMtime": datetime.fromtimestamp(staged_stat.st_mtime, tz=timezone.utc).isoformat(),
                        "sha256": sha256(local),
                    }
                )
        finally:
            sftp.close()
    finally:
        cleanup = (
            f"rm -rf -- {shlex.quote(remote_stage)}; "
            f"rm -f -- {shlex.quote(remote_archive)}"
        )
        try:
            _, stdout, _ = client.exec_command(cleanup, timeout=30)
            stdout.channel.recv_exit_status()
        except Exception:
            pass
        client.close()

    manifest = {
        "purpose": "V14 stability research; immutable raw production snapshot",
        "pulledAt": datetime.now(tz=timezone.utc).isoformat(),
        "host": host,
        "remoteRoot": args.remote_root,
        "files": records,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
