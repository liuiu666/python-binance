"""Download an immutable Binance USD-M futures one-minute research snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = "https://fapi.binance.com/fapi/v1/klines"
COLUMNS = [
    "open_time_ms", "open", "high", "low", "close", "volume",
    "close_time_ms", "quote_volume", "trades", "taker_buy_volume",
    "taker_buy_quote", "ignore",
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_utc(value: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def fetch(symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    cursor = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000) - 1
    rows: list[list] = []
    session = requests.Session()
    while cursor <= end_ms:
        error: Exception | None = None
        for attempt in range(5):
            try:
                response = session.get(
                    ENDPOINT,
                    params={
                        "symbol": symbol.upper(),
                        "interval": "1m",
                        "startTime": cursor,
                        "endTime": end_ms,
                        "limit": 1500,
                    },
                    timeout=20,
                )
                response.raise_for_status()
                batch = response.json()
                error = None
                break
            except Exception as exc:  # pragma: no cover - network retry path
                error = exc
                time.sleep(min(5.0, 0.5 * (2**attempt)))
        if error is not None:
            raise error
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1][0]) + 60_000
        if next_cursor <= cursor:
            raise RuntimeError("Binance kline cursor did not advance")
        cursor = next_cursor
        time.sleep(0.03)

    frame = pd.DataFrame(rows, columns=COLUMNS)
    if frame.empty:
        raise RuntimeError("Binance returned no futures minute rows")
    frame["open_time"] = pd.to_datetime(frame["open_time_ms"], unit="ms", utc=True)
    frame["close_time"] = pd.to_datetime(frame["close_time_ms"], unit="ms", utc=True)
    for column in (
        "open", "high", "low", "close", "volume", "quote_volume",
        "taker_buy_volume", "taker_buy_quote",
    ):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["trades"] = pd.to_numeric(frame["trades"], errors="coerce").astype("Int64")
    frame["symbol"] = symbol.upper()
    frame["market"] = "futures"
    frame = frame.loc[
        frame["open_time"].ge(start) & frame["open_time"].lt(end),
        [
            "open_time", "close_time", "symbol", "market", "open", "high",
            "low", "close", "volume", "quote_volume", "trades",
            "taker_buy_volume", "taker_buy_quote",
        ],
    ]
    return frame.sort_values("open_time").drop_duplicates("open_time", keep="last")


def audit(frame: pd.DataFrame) -> dict:
    times = pd.DatetimeIndex(pd.to_datetime(frame["open_time"], utc=True)).sort_values()
    steps = pd.Series(times).diff().dt.total_seconds().dropna()
    expected = int((times[-1] - times[0]).total_seconds() // 60) + 1
    return {
        "rows": int(len(frame)),
        "uniqueMinutes": int(times.nunique()),
        "start": times[0].isoformat(),
        "end": times[-1].isoformat(),
        "expectedMinutes": expected,
        "missingMinutes": max(0, expected - times.nunique()),
        "duplicateMinutes": int(len(times) - times.nunique()),
        "maxStepMinutes": round(float(steps.max() / 60.0), 4) if len(steps) else 0.0,
        "marketValues": sorted(frame["market"].dropna().astype(str).unique().tolist()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--start", default="2026-01-31T00:00:00Z")
    parser.add_argument("--end", default="2026-07-30T00:00:00Z")
    parser.add_argument(
        "--output",
        default=str(ROOT / "data" / "btcusdt_futures_1m_20260131_20260730.csv"),
    )
    args = parser.parse_args()
    start = parse_utc(args.start)
    end = parse_utc(args.end)
    if end <= start:
        raise ValueError("end must be later than start")
    output = Path(args.output)
    manifest = output.with_suffix(".manifest.json")
    if output.exists() and manifest.exists():
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        actual = sha256(output)
        if saved.get("sha256") != actual:
            raise RuntimeError("existing futures minute snapshot hash mismatch")
        print(json.dumps(saved, ensure_ascii=False, indent=2))
        return 0

    frame = fetch(args.symbol, start, end)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".partial")
    frame.to_csv(temporary, index=False, encoding="utf-8")
    temporary.replace(output)
    report = {
        "purpose": "V15 causal volatility-regime normal-reversion research",
        "endpoint": ENDPOINT,
        "symbol": args.symbol.upper(),
        "market": "Binance USD-M Futures",
        "requestedStart": start.isoformat(),
        "requestedEndExclusive": end.isoformat(),
        "output": str(output.resolve()),
        "sha256": sha256(output),
        "audit": audit(frame),
    }
    manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
