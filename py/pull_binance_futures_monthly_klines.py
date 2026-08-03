"""Download and freeze official Binance USD-M monthly one-minute klines."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "tmp" / "v27_klines_monthly_cache"
BASE_URL = "https://data.binance.vision/data/futures/um/monthly/klines"
RAW_COLUMNS = [
    "open_time_ms",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time_ms",
    "quote_volume",
    "trades",
    "taker_buy_volume",
    "taker_buy_quote",
    "ignore",
]


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_utc(value: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def month_keys(start: pd.Timestamp, end: pd.Timestamp) -> list[str]:
    if end <= start:
        raise ValueError("end must be later than start")
    first = start.tz_localize(None).to_period("M")
    final = (end - pd.Timedelta(nanoseconds=1)).tz_localize(None).to_period("M")
    return [period.strftime("%Y-%m") for period in pd.period_range(first, final, freq="M")]


def archive_name(symbol: str, month: str) -> str:
    return f"{symbol.upper()}-1m-{month}.zip"


def _download_one(symbol: str, month: str) -> dict[str, Any]:
    CACHE.mkdir(parents=True, exist_ok=True)
    name = archive_name(symbol, month)
    url = f"{BASE_URL}/{symbol.upper()}/1m/{name}"
    checksum_response = requests.get(f"{url}.CHECKSUM", timeout=30)
    checksum_response.raise_for_status()
    expected = checksum_response.text.strip().split()[0].lower()
    if len(expected) != 64:
        raise ValueError(f"invalid checksum response for {month}: {expected!r}")
    path = CACHE / name
    if path.exists() and sha256_file(path) == expected:
        return {
            "month": month,
            "url": url,
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": expected,
            "cached": True,
        }
    temporary = path.with_suffix(path.suffix + ".partial")
    with requests.get(url, stream=True, timeout=90) as response:
        response.raise_for_status()
        with temporary.open("wb") as handle:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    handle.write(chunk)
    observed = sha256_file(temporary)
    if observed != expected:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"checksum mismatch for {month}: {observed} != {expected}")
    temporary.replace(path)
    return {
        "month": month,
        "url": url,
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": observed,
        "cached": False,
    }


def download_archives(
    symbol: str, months: list[str], *, workers: int = 4
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(_download_one, symbol, month): month for month in months
        }
        for future in as_completed(futures):
            row = future.result()
            print(
                f"archive {row['month']} {row['bytes']} "
                f"{'cached' if row['cached'] else 'downloaded'}",
                flush=True,
            )
            results.append(row)
    return sorted(results, key=lambda row: str(row["month"]))


def read_archive(archive: dict[str, Any], symbol: str) -> pd.DataFrame:
    path = Path(str(archive["path"]))
    with zipfile.ZipFile(path) as bundle:
        members = [name for name in bundle.namelist() if name.lower().endswith(".csv")]
        if len(members) != 1:
            raise ValueError(f"{path} expected one CSV member, got {members}")
        with bundle.open(members[0]) as stream:
            raw = pd.read_csv(
                stream,
                header=None,
                names=RAW_COLUMNS,
                usecols=range(len(RAW_COLUMNS)),
                low_memory=False,
            )
    raw["open_time_ms"] = pd.to_numeric(raw["open_time_ms"], errors="coerce")
    raw["close_time_ms"] = pd.to_numeric(raw["close_time_ms"], errors="coerce")
    raw = raw.dropna(subset=["open_time_ms", "close_time_ms"]).copy()
    for column in (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "taker_buy_volume",
        "taker_buy_quote",
    ):
        raw[column] = pd.to_numeric(raw[column], errors="coerce")
    raw["trades"] = pd.to_numeric(raw["trades"], errors="coerce").astype("Int64")
    raw["open_time"] = pd.to_datetime(raw["open_time_ms"], unit="ms", utc=True)
    raw["close_time"] = pd.to_datetime(raw["close_time_ms"], unit="ms", utc=True)
    raw["symbol"] = symbol.upper()
    raw["market"] = "futures"
    columns = [
        "open_time",
        "close_time",
        "symbol",
        "market",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "trades",
        "taker_buy_volume",
        "taker_buy_quote",
    ]
    frame = raw[columns].dropna(subset=["open", "high", "low", "close"])
    numeric = frame[["open", "high", "low", "close"]].to_numpy(float)
    frame = frame.loc[np.isfinite(numeric).all(axis=1) & (numeric > 0.0).all(axis=1)]
    return frame.sort_values("open_time", kind="stable").drop_duplicates(
        "open_time", keep="last"
    )


def audit(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> dict[str, Any]:
    times = pd.DatetimeIndex(pd.to_datetime(frame["open_time"], utc=True)).sort_values()
    expected = int((end - start).total_seconds() // 60)
    if times.empty:
        raise ValueError("frozen kline frame is empty")
    duplicates = int(len(times) - times.nunique())
    missing_index = pd.date_range(start, end - pd.Timedelta(minutes=1), freq="min")
    missing = missing_index.difference(times.unique())
    outside = times[(times < start) | (times >= end)]
    return {
        "rows": int(len(frame)),
        "uniqueMinutes": int(times.nunique()),
        "expectedMinutes": expected,
        "start": times[0].isoformat(),
        "end": times[-1].isoformat(),
        "missingMinutes": int(len(missing)),
        "firstMissingMinutes": [value.isoformat() for value in missing[:20]],
        "duplicateMinutes": duplicates,
        "outsideRequestedRange": int(len(outside)),
        "marketValues": sorted(frame["market"].astype(str).unique().tolist()),
        "symbolValues": sorted(frame["symbol"].astype(str).unique().tolist()),
    }


def run(
    *,
    symbol: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    output: str | Path,
    workers: int,
) -> dict[str, Any]:
    output = Path(output)
    manifest_path = output.with_suffix(".manifest.json")
    if output.exists() and manifest_path.exists():
        saved = json.loads(manifest_path.read_text(encoding="utf-8"))
        observed = sha256_file(output)
        if saved.get("sha256") != observed:
            raise RuntimeError("existing frozen kline file hash does not match manifest")
        return saved
    months = month_keys(start, end)
    archives = download_archives(symbol, months, workers=workers)
    parts = []
    for archive in archives:
        part = read_archive(archive, symbol)
        print(f"parsed {archive['month']} {len(part)} rows", flush=True)
        parts.append(part)
    frame = pd.concat(parts, ignore_index=True)
    frame = frame.loc[
        pd.to_datetime(frame["open_time"], utc=True).ge(start)
        & pd.to_datetime(frame["open_time"], utc=True).lt(end)
    ].sort_values("open_time", kind="stable").drop_duplicates("open_time", keep="last")
    report_audit = audit(frame, start, end)
    if (
        report_audit["missingMinutes"] != 0
        or report_audit["duplicateMinutes"] != 0
        or report_audit["outsideRequestedRange"] != 0
    ):
        raise ValueError(f"frozen kline continuity audit failed: {report_audit}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".partial")
    frame.to_csv(temporary, index=False, encoding="utf-8")
    temporary.replace(output)
    report = {
        "purpose": "V27 frozen reverse-time validation; no parameter selection",
        "source": BASE_URL,
        "symbol": symbol.upper(),
        "market": "Binance USD-M Futures",
        "requestedStart": start.isoformat(),
        "requestedEndExclusive": end.isoformat(),
        "output": str(output.resolve()),
        "sha256": sha256_file(output),
        "audit": report_audit,
        "archives": archives,
    }
    manifest_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--start", default="2020-01-01T00:00:00Z")
    parser.add_argument("--end", default="2024-01-01T00:00:00Z")
    parser.add_argument(
        "--output",
        default=str(ROOT / "data" / "btcusdt_futures_1m_20200101_20240101.csv"),
    )
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    report = run(
        symbol=args.symbol,
        start=parse_utc(args.start),
        end=parse_utc(args.end),
        output=args.output,
        workers=args.workers,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
