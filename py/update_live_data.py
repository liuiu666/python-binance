"""Incrementally refresh BTCUSDT live data files.

This keeps the local CSVs continuous enough for the live signal service. It is
small on purpose: fetch only rows newer than the CSV tail, dedupe, sort, and
atomically replace the file so readers never see a partial write.
"""
import json
import os
import time
from datetime import datetime, timezone

import pandas as pd
import requests


OUT = os.environ.get("DATA_DIR", "E:/codex/data")
STATUS_FILE = os.path.join(OUT, "live_data_update_status.json")
SYMBOLS = [
    s.strip().lower()
    for s in os.environ.get("LIVE_UPDATE_SYMBOLS", "btcusdt").split(",")
    if s.strip()
]
BACKFILL_DAYS = max(1, int(os.environ.get("LIVE_UPDATE_BACKFILL_DAYS", "7")))
GAP_BACKFILL_DAYS = max(1, int(os.environ.get("LIVE_UPDATE_GAP_BACKFILL_DAYS", "30")))
HTTP_TIMEOUT = float(os.environ.get("LIVE_UPDATE_HTTP_TIMEOUT", "15"))
SPOT_BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]
FAPI_BASE = "https://fapi.binance.com"


def utc_now_ms():
    return int(time.time() * 1000)


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def request_json(url, params, bases=None):
    errors = []
    urls = [url]
    if bases:
        urls = [base + url for base in bases]
    for full_url in urls:
        for attempt in range(3):
            try:
                r = requests.get(full_url, params=params, timeout=HTTP_TIMEOUT)
                r.raise_for_status()
                data = r.json()
                if isinstance(data, dict) and data.get("code"):
                    raise RuntimeError(f"{data.get('code')}: {data.get('msg')}")
                return data
            except Exception as e:
                errors.append(f"{full_url} attempt={attempt + 1}: {e}")
                time.sleep(0.5 + attempt)
    raise RuntimeError("; ".join(errors[-4:]))


def read_existing(path, time_col):
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_csv(path)
    if df.empty or time_col not in df.columns:
        return pd.DataFrame()
    df[time_col] = pd.to_datetime(df[time_col], utc=True, format="ISO8601")
    return df.dropna(subset=[time_col]).sort_values(time_col).reset_index(drop=True)


def atomic_write_csv(df, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


def merge_and_write(existing, new_rows, path, time_col, columns):
    if new_rows is None or new_rows.empty:
        merged = existing
        added = 0
    else:
        merged = pd.concat([existing, new_rows], ignore_index=True) if not existing.empty else new_rows
        before = len(existing)
        merged[time_col] = pd.to_datetime(merged[time_col], utc=True, format="ISO8601")
        merged = (
            merged[columns]
            .dropna(subset=[time_col])
            .drop_duplicates(subset=[time_col], keep="last")
            .sort_values(time_col)
            .reset_index(drop=True)
        )
        added = max(0, len(merged) - before)
        atomic_write_csv(merged, path)
    last_ts = None if merged.empty else pd.to_datetime(merged[time_col].iloc[-1], utc=True).isoformat()
    return {"rows": int(len(merged)), "added": int(added), "last_ts": last_ts}


def start_ms_from_existing(existing, time_col, interval_ms):
    if existing.empty:
        return utc_now_ms() - BACKFILL_DAYS * 86400 * 1000
    last = pd.to_datetime(existing[time_col].iloc[-1], utc=True)
    return int(last.timestamp() * 1000) + interval_ms


def find_missing_ranges(existing, time_col, interval_ms):
    if existing.empty or len(existing) < 2:
        return []
    times = pd.to_datetime(existing[time_col], utc=True, format="ISO8601").sort_values().reset_index(drop=True)
    ranges = []
    for idx in range(1, len(times)):
        prev_ms = int(times.iloc[idx - 1].timestamp() * 1000)
        cur_ms = int(times.iloc[idx].timestamp() * 1000)
        if cur_ms - prev_ms > int(interval_ms * 1.5):
            start_ms = prev_ms + interval_ms
            end_ms = cur_ms - 1
            if start_ms <= end_ms:
                ranges.append((start_ms, end_ms))
    return ranges


def fetch_klines(symbol, existing):
    path = os.path.join(OUT, f"{symbol}_1m.csv")
    end_ms = (utc_now_ms() // 60000) * 60000 - 60000
    cursor = start_ms_from_existing(existing, "open_time", 60000)
    rows = []
    while cursor <= end_ms:
        batch = request_json(
            "/api/v3/klines",
            {
                "symbol": symbol.upper(),
                "interval": "1m",
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,
            },
            bases=SPOT_BASES,
        )
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1][0]) + 60000
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        time.sleep(0.08)
    if rows:
        df = pd.DataFrame(
            rows,
            columns=[
                "open_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_time",
                "quote_vol",
                "trades",
                "taker_buy_vol",
                "taker_buy_qv",
                "ignore",
            ],
        )
        df = df[["open_time", "open", "high", "low", "close", "volume"]]
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    else:
        df = pd.DataFrame(columns=["open_time", "open", "high", "low", "close", "volume"])
    return merge_and_write(
        existing,
        df,
        path,
        "open_time",
        ["open_time", "open", "high", "low", "close", "volume"],
    )


def period_rows_to_df(rows, out_cols, api_cols):
    if rows:
        df = pd.DataFrame(rows)[api_cols].rename(columns={api_cols[0]: "timestamp"})
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        for col in out_cols:
            if col != "timestamp":
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df[out_cols]
    return pd.DataFrame(columns=out_cols)


def fetch_period_range(symbol, endpoint, start_ms, end_ms):
    rows = []
    cursor = int(start_ms)
    while cursor <= end_ms:
        batch = request_json(
            FAPI_BASE + endpoint,
            {
                "symbol": symbol.upper(),
                "period": "5m",
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 500,
            },
        )
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1]["timestamp"]) + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        time.sleep(0.08)
    return rows


def fetch_period_data(symbol, existing, name, endpoint, out_cols, api_cols):
    path = os.path.join(OUT, f"{symbol}_{name}.csv")
    end_ms = utc_now_ms()
    tail_start_ms = start_ms_from_existing(existing, "timestamp", 5 * 60 * 1000)
    rows = fetch_period_range(symbol, endpoint, tail_start_ms, end_ms)
    df = period_rows_to_df(rows, out_cols, api_cols)
    first_merge = merge_and_write(existing, df, path, "timestamp", out_cols)

    merged = read_existing(path, "timestamp")
    gap_rows = []
    gap_errors = []
    gap_skipped_old = 0
    min_gap_ms = utc_now_ms() - GAP_BACKFILL_DAYS * 86400 * 1000
    gaps = find_missing_ranges(merged, "timestamp", 5 * 60 * 1000)
    for start_ms, stop_ms in gaps:
        if stop_ms < min_gap_ms:
            gap_skipped_old += 1
            continue
        try:
            gap_rows.extend(fetch_period_range(symbol, endpoint, start_ms, stop_ms))
        except Exception as e:
            gap_errors.append({
                "start": pd.to_datetime(start_ms, unit="ms", utc=True).isoformat(),
                "end": pd.to_datetime(stop_ms, unit="ms", utc=True).isoformat(),
                "error": str(e),
            })
    if gap_rows:
        gap_df = period_rows_to_df(gap_rows, out_cols, api_cols)
        final = merge_and_write(merged, gap_df, path, "timestamp", out_cols)
    else:
        final = first_merge
    final["gap_backfill_added"] = max(0, int(final["rows"]) - int(first_merge["rows"]))
    final["gap_ranges_seen"] = len(gaps)
    final["gap_ranges_skipped_old"] = gap_skipped_old
    final["gap_backfill_errors"] = gap_errors[:10]
    final["gap_ranges_remaining"] = len(find_missing_ranges(read_existing(path, "timestamp"), "timestamp", 5 * 60 * 1000))
    return final


def fetch_funding(symbol, existing):
    path = os.path.join(OUT, f"{symbol}_funding.csv")
    cursor = start_ms_from_existing(existing, "fundingTime", 1)
    end_ms = utc_now_ms()
    rows = []
    while cursor <= end_ms:
        batch = request_json(
            FAPI_BASE + "/fapi/v1/fundingRate",
            {"symbol": symbol.upper(), "startTime": cursor, "endTime": end_ms, "limit": 1000},
        )
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1]["fundingTime"]) + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        time.sleep(0.08)
    if rows:
        df = pd.DataFrame(rows)[["fundingTime", "fundingRate"]]
        df["fundingTime"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
        df["fundingRate"] = pd.to_numeric(df["fundingRate"], errors="coerce")
    else:
        df = pd.DataFrame(columns=["fundingTime", "fundingRate"])
    return merge_and_write(existing, df, path, "fundingTime", ["fundingTime", "fundingRate"])


def update_symbol(symbol):
    result = {}
    kline_path = os.path.join(OUT, f"{symbol}_1m.csv")
    result["klines_1m"] = fetch_klines(symbol, read_existing(kline_path, "open_time"))

    taker_path = os.path.join(OUT, f"{symbol}_taker.csv")
    result["taker"] = fetch_period_data(
        symbol,
        read_existing(taker_path, "timestamp"),
        "taker",
        "/futures/data/takerlongshortRatio",
        ["timestamp", "buySellRatio", "buyVol", "sellVol"],
        ["timestamp", "buySellRatio", "buyVol", "sellVol"],
    )

    ls_path = os.path.join(OUT, f"{symbol}_lsratio.csv")
    result["lsratio"] = fetch_period_data(
        symbol,
        read_existing(ls_path, "timestamp"),
        "lsratio",
        "/futures/data/topLongShortPositionRatio",
        ["timestamp", "longAccount", "shortAccount", "longShortRatio"],
        ["timestamp", "longAccount", "shortAccount", "longShortRatio"],
    )

    funding_path = os.path.join(OUT, f"{symbol}_funding.csv")
    result["funding"] = fetch_funding(symbol, read_existing(funding_path, "fundingTime"))
    return result


def main():
    status = {
        "ok": True,
        "started_at": iso_now(),
        "finished_at": None,
        "symbols": {},
        "errors": [],
    }
    for symbol in SYMBOLS:
        try:
            status["symbols"][symbol] = update_symbol(symbol)
        except Exception as e:
            status["ok"] = False
            status["errors"].append({"symbol": symbol, "error": str(e)})
    status["finished_at"] = iso_now()
    os.makedirs(OUT, exist_ok=True)
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2, ensure_ascii=False)
    print(json.dumps(status, ensure_ascii=False))
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
