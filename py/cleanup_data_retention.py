"""Bound production data growth without touching current strategy inputs."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = Path(os.environ.get("DATA_DIR") or ROOT / "data")


def size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def parse_day(value: str) -> date | None:
    text = value.removeprefix("date=")
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def prune_daily_files(folder: Path, keep_days: int, today: date) -> list[str]:
    removed: list[str] = []
    cutoff = today - timedelta(days=max(1, keep_days) - 1)
    if not folder.exists():
        return removed
    for item in folder.iterdir():
        item_day = parse_day(item.stem if item.is_file() else item.name)
        if item_day is None or item_day >= cutoff:
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()
        removed.append(str(item))
    return removed


def tail_lines(path: Path, limit: int, chunk_size: int = 1024 * 1024) -> bytes:
    if not path.exists() or limit <= 0:
        return b""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        data = b""
        while position > 0 and data.count(b"\n") <= limit:
            step = min(chunk_size, position)
            position -= step
            handle.seek(position)
            data = handle.read(step) + data
    lines = data.splitlines()[-limit:]
    return b"\n".join(lines) + (b"\n" if lines else b"")


def trim_jsonl(path: Path, max_lines: int) -> dict[str, Any]:
    before = size_bytes(path)
    if before == 0:
        return {"path": str(path), "before": before, "after": before, "trimmed": False}
    payload = tail_lines(path, max_lines)
    if len(payload) >= before:
        return {"path": str(path), "before": before, "after": before, "trimmed": False}
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)
    return {"path": str(path), "before": before, "after": len(payload), "trimmed": True}


def prune_database(path: Path, keep_days: int, now_ms: int, vacuum: bool = False) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "present": False}
    cutoff_ms = now_ms - max(1, keep_days) * 24 * 60 * 60 * 1000
    connection = sqlite3.connect(path, timeout=30)
    try:
        audits = connection.execute("DELETE FROM trade_audits WHERE serverTime < ?", (cutoff_ms,)).rowcount
        ticks = connection.execute("DELETE FROM price_ticks WHERE time < ?", (cutoff_ms,)).rowcount
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
        if vacuum:
            connection.execute("VACUUM")
        return {
            "path": str(path),
            "present": True,
            "deletedAudits": audits,
            "deletedTicks": ticks,
            "vacuumed": vacuum,
        }
    finally:
        connection.close()


def cleanup(
    data_dir: Path,
    *,
    market_days: int,
    auction_days: int,
    audit_lines: int,
    prediction_lines: int,
    database_days: int,
    remove_auction: bool,
    vacuum_database: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    today = today or datetime.now(timezone.utc).date()
    before = size_bytes(data_dir)
    removed: list[str] = []
    removed.extend(prune_daily_files(data_dir / "second" / "BTCUSDT" / "futures", market_days, today))
    removed.extend(prune_daily_files(data_dir / "orderbook" / "BTCUSDT" / "futures", market_days, today))
    auction_root = data_dir / "auction" / "BTCUSDT" / "futures"
    if remove_auction and auction_root.exists():
        shutil.rmtree(auction_root)
        removed.append(str(auction_root))
    elif auction_root.exists():
        for stream in auction_root.iterdir():
            if stream.is_dir():
                removed.extend(prune_daily_files(stream, auction_days, today))
    logs = [
        trim_jsonl(data_dir / "signal_audit.jsonl", audit_lines),
        trim_jsonl(data_dir / "orderbook_predictions.jsonl", prediction_lines),
    ]
    database = prune_database(
        data_dir / "codex.db", database_days, int(time.time() * 1000), vacuum=vacuum_database
    )
    after = size_bytes(data_dir)
    return {
        "dataDir": str(data_dir),
        "policy": {
            "marketDays": market_days,
            "auctionDays": 0 if remove_auction else auction_days,
            "auditLines": audit_lines,
            "predictionLines": prediction_lines,
            "databaseDays": database_days,
        },
        "removed": removed,
        "logs": logs,
        "database": database,
        "beforeBytes": before,
        "afterBytes": after,
        "freedBytes": max(0, before - after),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--market-days", type=int, default=7)
    parser.add_argument("--auction-days", type=int, default=3)
    parser.add_argument("--audit-lines", type=int, default=20000)
    parser.add_argument("--prediction-lines", type=int, default=20000)
    parser.add_argument("--database-days", type=int, default=7)
    parser.add_argument("--remove-auction", action="store_true")
    parser.add_argument("--vacuum-database", action="store_true")
    args = parser.parse_args()
    report = cleanup(
        args.data_dir,
        market_days=args.market_days,
        auction_days=args.auction_days,
        audit_lines=args.audit_lines,
        prediction_lines=args.prediction_lines,
        database_days=args.database_days,
        remove_auction=args.remove_auction,
        vacuum_database=args.vacuum_database,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
