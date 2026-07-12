"""Collect BTCUSDT second-level trade bars for future backtests.

The live strategy still uses the existing 1m/2m pipeline. This service only
stores raw-ish second bars so later research can test second-level entry timing
and taker-flow filters without changing production signals.
"""
import atexit
import csv
import json
import os
import queue
import shutil
import socket
import sys
import threading
import time
from datetime import datetime, timezone

import pandas as pd
import requests
import websocket

try:
    import msvcrt
    fcntl = None
except ImportError:
    msvcrt = None
    import fcntl


APP_DIR = os.environ.get("APP_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("DATA_DIR", os.path.join(APP_DIR, "data"))
SYMBOL = os.environ.get("SECOND_DATA_SYMBOL", "BTCUSDT").upper()
MARKET = os.environ.get("SECOND_DATA_MARKET", "futures").strip().lower()
MODE = os.environ.get("SECOND_DATA_MODE", "rest").strip().lower()
INTERVAL_SEC = max(0.5, float(os.environ.get("SECOND_DATA_INTERVAL_SEC", "1")))
BACKFILL_MINUTES = max(1, int(os.environ.get("SECOND_DATA_BACKFILL_MINUTES", "10")))
STARTUP_BACKFILL_MINUTES = max(0, int(os.environ.get("SECOND_DATA_STARTUP_BACKFILL_MINUTES", "0")))
BACKFILL_SLEEP_SEC = max(0.0, float(os.environ.get("SECOND_DATA_BACKFILL_SLEEP_SEC", "0.08")))
RETENTION_DAYS = max(1, int(os.environ.get("SECOND_DATA_RETENTION_DAYS", "120")))
HTTP_TIMEOUT = float(os.environ.get("SECOND_DATA_HTTP_TIMEOUT", "8"))
FINALIZE_DELAY_SEC = max(1, int(os.environ.get("SECOND_DATA_FINALIZE_DELAY_SEC", "1")))
RATE_LIMIT_BACKOFF_SEC = max(30, int(os.environ.get("SECOND_DATA_RATE_LIMIT_BACKOFF_SEC", "180")))
WS_PING_INTERVAL_SEC = max(10, int(os.environ.get("SECOND_DATA_WS_PING_INTERVAL_SEC", "20")))
WS_PING_TIMEOUT_SEC = max(5, int(os.environ.get("SECOND_DATA_WS_PING_TIMEOUT_SEC", "10")))
STATUS_INTERVAL_SEC = max(1, int(os.environ.get("SECOND_DATA_STATUS_INTERVAL_SEC", "2")))
WS_FLUSH_INTERVAL_SEC = max(0.2, float(os.environ.get("SECOND_DATA_WS_FLUSH_INTERVAL_SEC", "0.5")))
WS_FLUSH_MAX_TRADES = max(100, int(os.environ.get("SECOND_DATA_WS_FLUSH_MAX_TRADES", "5000")))

OUT_FILE = os.path.join(OUT, f"{SYMBOL.lower()}_1s_trades.csv")
STATUS_FILE = os.path.join(OUT, "second_data_status.json")
LOCK_FILE = os.path.join(OUT, "second_data.lock")
LOCK_DIR = os.path.join(OUT, "second_data.lockdir")
LOCK_PORT = int(os.environ.get("SECOND_DATA_LOCK_PORT", "39872"))
SHARD_DIR = os.path.join(OUT, "second", SYMBOL, "futures")
# 主文件最多保留多少天的数据（超过的转移到 shard）
MAIN_FILE_KEEP_DAYS = max(1, int(os.environ.get("SECOND_DATA_MAIN_KEEP_DAYS", "1")))

SPOT_BASES = [
    base.strip().rstrip("/")
    for base in os.environ.get(
        "SECOND_DATA_SPOT_BASES",
        "https://data-api.binance.vision,https://api.binance.com,https://api1.binance.com,https://api2.binance.com,https://api3.binance.com",
    ).split(",")
    if base.strip()
]
FAPI_BASES = [
    base.strip().rstrip("/")
    for base in os.environ.get(
        "SECOND_DATA_FAPI_BASES",
        "https://fapi.binance.com,https://fapi1.binance.com,https://fapi2.binance.com,https://fapi3.binance.com",
    ).split(",")
    if base.strip()
]


def default_ws_url():
    if MARKET == "futures":
        stream = f"{SYMBOL.lower()}@trade"
        return f"wss://fstream.binance.com/ws/{stream}"
    stream = f"{SYMBOL.lower()}@aggTrade"
    return f"wss://stream.binance.com:9443/ws/{stream}"


WS_URL = os.environ.get("SECOND_DATA_WS_URL", default_ws_url()).strip()
GAP_REPAIR_INTERVAL_SEC = max(5, int(os.environ.get("SECOND_DATA_GAP_REPAIR_INTERVAL_SEC", "30")))
GAP_REPAIR_LOOKBACK_SEC = max(60, int(os.environ.get("SECOND_DATA_GAP_REPAIR_LOOKBACK_SEC", "900")))
GAP_REPAIR_MERGE_GAP_SEC = max(0, int(os.environ.get("SECOND_DATA_GAP_REPAIR_MERGE_GAP_SEC", "20")))
GAP_REPAIR_MAX_RANGE_SEC = max(30, int(os.environ.get("SECOND_DATA_GAP_REPAIR_MAX_RANGE_SEC", "240")))
GAP_REPAIR_MAX_RANGES = max(1, int(os.environ.get("SECOND_DATA_GAP_REPAIR_MAX_RANGES", "8")))
GAP_REPAIR_MAX_PAGES = max(1, int(os.environ.get("SECOND_DATA_GAP_REPAIR_MAX_PAGES", "20")))
FILL_EMPTY_SECONDS = os.environ.get("SECOND_DATA_FILL_EMPTY_SECONDS", "1").strip().lower() not in ("0", "false", "no", "off")
FILL_EMPTY_MAX_GAP_SEC = max(0, int(os.environ.get("SECOND_DATA_FILL_EMPTY_MAX_GAP_SEC", "3")))
FILE_REPAIR_INTERVAL_SEC = max(30, int(os.environ.get("SECOND_DATA_FILE_REPAIR_INTERVAL_SEC", "300")))
CATCHUP_MAX_PAGES = max(1, int(os.environ.get("SECOND_DATA_CATCHUP_MAX_PAGES", "80")))
CATCHUP_RECENT_MINUTES = max(1, int(os.environ.get("SECOND_DATA_CATCHUP_RECENT_MINUTES", str(BACKFILL_MINUTES))))
STARTUP_FILE_REPAIR = os.environ.get("SECOND_DATA_STARTUP_FILE_REPAIR", "0").strip().lower() in ("1", "true", "yes", "on")
CSV_WRITE_LOCK = threading.RLock()


def websocket_cursor_matches_rest():
    return "@aggtrade" in WS_URL.lower()


class RateLimitError(RuntimeError):
    def __init__(self, message, retry_after_sec=None):
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


def utc_ms():
    return int(time.time() * 1000)


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def acquire_singleton_lock():
    os.makedirs(OUT, exist_ok=True)
    try:
        os.mkdir(LOCK_DIR)
        with open(os.path.join(LOCK_DIR, "pid"), "w", encoding="utf-8") as fpid:
            fpid.write(str(os.getpid()))
        atexit.register(lambda: shutil.rmtree(LOCK_DIR, ignore_errors=True))
    except FileExistsError:
        pid_path = os.path.join(LOCK_DIR, "pid")
        try:
            with open(pid_path, "r", encoding="utf-8") as fpid:
                old_pid = int((fpid.read() or "0").strip())
            os.kill(old_pid, 0)
            print(f"[SecondData] Another instance is active pid={old_pid}; exiting.")
            sys.exit(0)
        except Exception:
            shutil.rmtree(LOCK_DIR, ignore_errors=True)
            os.mkdir(LOCK_DIR)
            with open(pid_path, "w", encoding="utf-8") as fpid:
                fpid.write(str(os.getpid()))
            atexit.register(lambda: shutil.rmtree(LOCK_DIR, ignore_errors=True))

    handle = open(LOCK_FILE, "a+", encoding="utf-8")
    try:
        handle.seek(0)
        if msvcrt is not None:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
    except OSError:
        print(f"[SecondData] Another instance holds {LOCK_FILE}; exiting.")
        sys.exit(0)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", LOCK_PORT))
        sock.listen(1)
        return handle, sock
    except OSError:
        print(f"[SecondData] Another instance is already running on lock port {LOCK_PORT}; exiting.")
        sys.exit(0)


def request_json(path, params):
    bases = FAPI_BASES if MARKET == "futures" else SPOT_BASES
    errors = []
    for base in bases:
        url = base + path
        for attempt in range(3):
            try:
                r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
                if r.status_code in (418, 429):
                    retry_after = _rate_limit_retry_after(r)
                    raise RateLimitError(
                        f"{r.status_code} rate limited for {url}: {r.text[:300]}",
                        retry_after,
                    )
                r.raise_for_status()
                data = r.json()
                if isinstance(data, dict) and data.get("code"):
                    raise RuntimeError(f"{data.get('code')}: {data.get('msg')}")
                return data
            except RateLimitError:
                raise
            except Exception as exc:
                errors.append(f"{url} attempt={attempt + 1}: {exc}")
                time.sleep(0.25 + attempt * 0.5)
    raise RuntimeError("; ".join(errors[-4:]))


def _rate_limit_retry_after(response):
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return max(RATE_LIMIT_BACKOFF_SEC, int(float(retry_after)))
        except (TypeError, ValueError):
            pass
    try:
        data = response.json()
        msg = str(data.get("msg") or "")
    except Exception:
        msg = response.text or ""
    import re

    match = re.search(r"banned until (\d{12,})", msg)
    if match:
        until_ms = int(match.group(1))
        return max(RATE_LIMIT_BACKOFF_SEC, int((until_ms - utc_ms()) / 1000) + 15)
    return RATE_LIMIT_BACKOFF_SEC


def read_status():
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def write_status(obj):
    os.makedirs(OUT, exist_ok=True)
    tmp = f"{STATUS_FILE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATUS_FILE)


CSV_COLUMNS = [
    "timestamp",
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
    "taker_sell_volume",
    "taker_buy_quote",
    "taker_sell_quote",
    "taker_buy_sell_ratio",
    "first_trade_time",
    "last_trade_time",
    "first_agg_trade_id",
    "last_agg_trade_id",
]


def parse_ts(value):
    try:
        if value is None:
            return None
        ts = pd.to_datetime(str(value).replace("\x00", "").strip(), utc=True, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.floor("s")
    except Exception:
        return None


def parse_float(value, default=0.0):
    try:
        n = float(value)
        return n if pd.notna(n) else default
    except Exception:
        return default


def parse_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


def valid_price_bar(row):
    prices = {col: parse_float(row.get(col), None) for col in ("open", "high", "low", "close")}
    if any(value is None or value <= 0 for value in prices.values()):
        return None
    high = prices["high"]
    low = prices["low"]
    if high < low:
        return None
    if high < max(prices["open"], prices["close"]) or low > min(prices["open"], prices["close"]):
        return None
    return prices


def csv_line_to_record(line):
    try:
        clean = line.replace("\x00", "").strip()
        if not clean or clean.startswith("timestamp,"):
            return None
        fields = next(csv.reader([clean]))
        if len(fields) < len(CSV_COLUMNS):
            return None
        row = dict(zip(CSV_COLUMNS, fields[:len(CSV_COLUMNS)]))
        ts = parse_ts(row.get("timestamp"))
        prices = valid_price_bar(row)
        if ts is None or prices is None:
            return None
        return {
            "dt": ts,
            "line": clean,
            "row": row,
            "close": prices["close"],
            "last_agg_trade_id": parse_int(row.get("last_agg_trade_id"), 0),
        }
    except Exception:
        return None


def repair_result_last_fields(record):
    if record is None:
        return {}
    return {
        "lastTs": record["dt"].isoformat(),
        "lastAggTradeId": int(record["last_agg_trade_id"]),
        "lastClose": float(record["close"]),
    }


def repair_csv_file(path, *, rewrite_out_of_order=True):
    """Remove NUL bytes, invalid rows and duplicate timestamps from the hot CSV."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return {"checked": False, "reason": "missing"}
    with open(path, "rb") as f:
        data = f.read()
    nul_bytes = data.count(b"\x00")
    text = data.replace(b"\x00", b"").decode("utf-8", "replace")
    lines = text.splitlines()
    if not lines:
        return {"checked": True, "rewritten": False, "reason": "empty"}

    header_index = 0
    for i, line in enumerate(lines[:20]):
        if line.startswith("timestamp,"):
            header_index = i
            break
    header = ",".join(CSV_COLUMNS)
    if lines[header_index].startswith("timestamp,"):
        header = lines[header_index].strip()

    by_ts = {}
    ordered_ts = []
    invalid_rows = 0
    duplicate_rows = 0
    for line in lines[header_index + 1:]:
        rec = csv_line_to_record(line)
        if rec is None:
            if line.strip():
                invalid_rows += 1
            continue
        key = rec["dt"].value
        if key in by_ts:
            duplicate_rows += 1
        by_ts[key] = rec
        ordered_ts.append(key)

    sorted_keys = sorted(by_ts.keys())
    last_record = by_ts[sorted_keys[-1]] if sorted_keys else None
    out_of_order = ordered_ts != sorted(ordered_ts)
    needs_rewrite = bool(nul_bytes or invalid_rows or duplicate_rows or (out_of_order and rewrite_out_of_order))
    if not needs_rewrite:
        return {
            "checked": True,
            "rewritten": False,
            "rows": len(by_ts),
            "nulBytes": int(nul_bytes),
            "invalidRows": int(invalid_rows),
            "duplicateRows": int(duplicate_rows),
            "outOfOrder": bool(out_of_order),
            **repair_result_last_fields(last_record),
        }

    tmp = f"{path}.repair_tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(header.rstrip("\r\n") + "\n")
        for key in sorted_keys:
            f.write(by_ts[key]["line"].rstrip("\r\n") + "\n")
    os.replace(tmp, path)
    return {
        "checked": True,
        "rewritten": True,
        "rows": len(by_ts),
        "nulBytes": int(nul_bytes),
        "invalidRows": int(invalid_rows),
        "duplicateRows": int(duplicate_rows),
        "outOfOrder": bool(out_of_order),
        **repair_result_last_fields(last_record),
    }


def maybe_repair_hot_csv(state, force=False, rewrite_out_of_order=True):
    now = time.time()
    if not force and now - float(state.get("last_file_repair") or 0) < FILE_REPAIR_INTERVAL_SEC:
        return None
    state["last_file_repair"] = now
    result = repair_csv_file(OUT_FILE, rewrite_out_of_order=rewrite_out_of_order)
    if result:
        state["status"]["file_repair"] = result
    if result and result.get("checked"):
        row_count = int(result.get("rows") if result.get("rows") is not None else count_csv_rows(OUT_FILE))
        state["total_rows"] = row_count
        state["status"]["rows"] = state["total_rows"]
        if result.get("lastTs"):
            state["status"]["last_ts"] = result["lastTs"]
            state["status"]["last_agg_trade_id"] = result.get("lastAggTradeId")
            state["cursor_id"] = result.get("lastAggTradeId")
        state["status"]["updated_at"] = iso_now()
        if result.get("rewritten") or result.get("lastTs"):
            write_status(state["status"])
    if result and result.get("rewritten"):
        print(f"\n[SecondData] file_repair={result}", flush=True)
    return result


def read_recent_records(path, lookback_sec):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return []
    # The hot file is usually one day. A tail read avoids scanning old shards
    # while still covering repairs appended near the end of the file.
    tail_bytes = max(4 * 1024 * 1024, min(32 * 1024 * 1024, int(lookback_sec) * 4096))
    with CSV_WRITE_LOCK:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            size = min(end, tail_bytes)
            f.seek(end - size)
            text = f.read(size).replace(b"\x00", b"").decode("utf-8", "replace")
    lines = text.splitlines()
    if end > size and lines:
        lines = lines[1:]
    records = []
    for line in lines:
        rec = csv_line_to_record(line)
        if rec is not None:
            records.append(rec)
    if not records:
        return []
    latest = max(rec["dt"] for rec in records)
    cutoff = latest - pd.Timedelta(seconds=max(1, int(lookback_sec)) + 120)
    return sorted([rec for rec in records if rec["dt"] >= cutoff], key=lambda r: r["dt"])


def group_missing_seconds(missing):
    if not missing:
        return []
    ranges = []
    start = prev = missing[0]
    for ts in missing[1:]:
        if (ts - prev).total_seconds() == 1:
            prev = ts
            continue
        ranges.append((start, prev))
        start = prev = ts
    ranges.append((start, prev))
    return ranges


def merge_gap_ranges(ranges):
    merged = []
    for start, end in ranges:
        if not merged:
            merged.append([start, end])
            continue
        prev = merged[-1]
        gap = int((start - prev[1]).total_seconds()) - 1
        span = int((end - prev[0]).total_seconds()) + 1
        if gap <= GAP_REPAIR_MERGE_GAP_SEC and span <= GAP_REPAIR_MAX_RANGE_SEC:
            prev[1] = end
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def fetch_trades_time_range_all(start_ms, end_ms, max_pages=None):
    max_pages = max_pages or GAP_REPAIR_MAX_PAGES
    rows = []
    cursor = int(start_ms)
    pages = 0
    while cursor <= int(end_ms) and pages < max_pages:
        page = fetch_trades_time_range(cursor, end_ms)
        pages += 1
        if not page:
            break
        rows.extend(page)
        last_trade_ms = max(int(row["T"]) for row in page if "T" in row)
        next_cursor = last_trade_ms + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(page) < 1000:
            break
        if BACKFILL_SLEEP_SEC > 0:
            time.sleep(BACKFILL_SLEEP_SEC)
    return rows, pages


def update_known_from_bars(known, bars):
    if bars is None or bars.empty:
        return
    for _, row in bars.iterrows():
        ts = pd.to_datetime(row["timestamp"], utc=True, format="ISO8601").floor("s")
        known[ts.value] = {
            "dt": ts,
            "close": parse_float(row.get("close"), 0.0),
            "last_agg_trade_id": parse_int(row.get("last_agg_trade_id"), 0),
        }


def synthetic_empty_bars_for_missing(missing_ranges, checked_seconds, known):
    if not FILL_EMPTY_SECONDS or FILL_EMPTY_MAX_GAP_SEC <= 0:
        return pd.DataFrame()
    rows = []
    for start, end in missing_ranges:
        length = int((end - start).total_seconds()) + 1
        if length > FILL_EMPTY_MAX_GAP_SEC:
            continue
        cur = start
        while cur <= end:
            if cur.value not in checked_seconds or cur.value in known:
                cur += pd.Timedelta(seconds=1)
                continue
            prev_keys = [key for key in known.keys() if key < cur.value]
            if not prev_keys:
                cur += pd.Timedelta(seconds=1)
                continue
            prev = known[max(prev_keys)]
            close = float(prev["close"])
            trade_id = int(prev.get("last_agg_trade_id") or 0)
            rows.append({
                "timestamp": cur,
                "symbol": SYMBOL,
                "market": MARKET,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 0.0,
                "quote_volume": 0.0,
                "trades": 0,
                "taker_buy_volume": 0.0,
                "taker_sell_volume": 0.0,
                "taker_buy_quote": 0.0,
                "taker_sell_quote": 0.0,
                "taker_buy_sell_ratio": 0.0,
                "first_trade_time": cur,
                "last_trade_time": cur,
                "first_agg_trade_id": trade_id,
                "last_agg_trade_id": trade_id,
            })
            known[cur.value] = {"dt": cur, "close": close, "last_agg_trade_id": trade_id}
            cur += pd.Timedelta(seconds=1)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)[CSV_COLUMNS]


def append_repair_bars(state, bars, *, update_state=True):
    if bars is None or bars.empty:
        return 0, {"enabled": True, "dir": SHARD_DIR, "written_files": [], "rows": 0}
    added = append_csv(bars, OUT_FILE)
    shard_write = append_daily_shards(bars) if added else {"enabled": True, "dir": SHARD_DIR, "written_files": [], "rows": 0}
    if added and update_state:
        state["total_rows"] = count_csv_rows(OUT_FILE)
        state["status"]["rows"] = state["total_rows"]
        current_last = parse_ts(state["status"].get("last_ts"))
        bar_last = pd.to_datetime(bars["timestamp"], utc=True, format="ISO8601").max().floor("s")
        if current_last is None or bar_last > current_last:
            state["status"]["last_ts"] = bar_last.isoformat()
            state["status"]["last_agg_trade_id"] = int(bars["last_agg_trade_id"].max())
    return int(added), shard_write


def repair_recent_gaps(state, force=False, *, update_state=True):
    now = time.time()
    if not force and now - float(state.get("last_gap_repair") or 0) < GAP_REPAIR_INTERVAL_SEC:
        return None
    state["last_gap_repair"] = now
    records = read_recent_records(OUT_FILE, GAP_REPAIR_LOOKBACK_SEC)
    if not records:
        return {"enabled": True, "reason": "no_recent_records"}

    known = {
        rec["dt"].value: {
            "dt": rec["dt"],
            "close": rec["close"],
            "last_agg_trade_id": rec["last_agg_trade_id"],
        }
        for rec in records
    }
    latest = max(rec["dt"] for rec in records)
    end_ts = min(latest, pd.Timestamp.now(tz="UTC").floor("s") - pd.Timedelta(seconds=FINALIZE_DELAY_SEC))
    start_ts = end_ts - pd.Timedelta(seconds=GAP_REPAIR_LOOKBACK_SEC - 1)
    observed = {rec["dt"].value for rec in records if start_ts <= rec["dt"] <= end_ts}
    expected = []
    cur = start_ts
    while cur <= end_ts:
        expected.append(cur)
        cur += pd.Timedelta(seconds=1)
    missing = [ts for ts in expected if ts.value not in observed]
    missing_ranges = group_missing_seconds(missing)
    merged_ranges = merge_gap_ranges(missing_ranges)[-GAP_REPAIR_MAX_RANGES:]
    if not missing:
        return {
            "enabled": True,
            "lookbackSec": GAP_REPAIR_LOOKBACK_SEC,
            "observed": len(observed),
            "missing": 0,
            "coveragePct": 100.0,
        }

    checked_seconds = set()
    total_trades = 0
    total_pages = 0
    actual_parts = []
    for start, end in merged_ranges:
        start_ms = int(start.timestamp() * 1000)
        end_ms = int((end + pd.Timedelta(milliseconds=999)).timestamp() * 1000)
        rows, pages = fetch_trades_time_range_all(start_ms, end_ms)
        total_pages += int(pages)
        total_trades += len(rows)
        cur = start
        while cur <= end:
            checked_seconds.add(cur.value)
            cur += pd.Timedelta(seconds=1)
        bars = aggregate_trades(rows)
        if not bars.empty:
            actual_parts.append(bars)

    actual_bars = collapse_bars(pd.concat(actual_parts, ignore_index=True)) if actual_parts else pd.DataFrame()
    update_known_from_bars(known, actual_bars)
    actual_added, shard_write = append_repair_bars(state, actual_bars, update_state=update_state)

    synthetic_bars = synthetic_empty_bars_for_missing(missing_ranges, checked_seconds, known)
    synthetic_added, synthetic_shard = append_repair_bars(state, synthetic_bars, update_state=update_state)

    repaired_seconds = int(actual_added + synthetic_added)
    after_observed = min(GAP_REPAIR_LOOKBACK_SEC, len(observed) + repaired_seconds)
    result = {
        "enabled": True,
        "lookbackSec": GAP_REPAIR_LOOKBACK_SEC,
        "ranges": len(missing_ranges),
        "requestedRanges": len(merged_ranges),
        "pages": total_pages,
        "trades": total_trades,
        "actualAdded": int(actual_added),
        "syntheticAdded": int(synthetic_added),
        "missingBefore": len(missing),
        "coverageBeforePct": round(len(observed) / max(1, GAP_REPAIR_LOOKBACK_SEC) * 100.0, 4),
        "coverageAfterApproxPct": round(after_observed / max(1, GAP_REPAIR_LOOKBACK_SEC) * 100.0, 4),
        "shards": shard_write,
        "syntheticShards": synthetic_shard,
    }
    if update_state:
        state["status"]["gap_repair"] = result
        write_status(state["status"])
        if repaired_seconds or len(missing) > 0:
            print(f"\n[SecondData] gap_repair={result}", flush=True)
    return result


def schedule_gap_repair(state, force=False):
    """Start gap repair without blocking websocket message handling."""
    now = time.time()
    if not force and now - float(state.get("last_gap_repair") or 0) < GAP_REPAIR_INTERVAL_SEC:
        return False
    worker = state.get("gap_repair_worker")
    if worker is not None and worker.is_alive():
        return False
    state["last_gap_repair"] = now
    result_queue = state.setdefault("gap_repair_results", queue.Queue())

    def run():
        try:
            result = repair_recent_gaps(
                {"last_gap_repair": 0.0},
                force=True,
                update_state=False,
            )
            result_queue.put({"result": result, "error": None})
        except Exception as exc:
            result_queue.put({"result": None, "error": str(exc)})

    worker = threading.Thread(target=run, name="second-gap-repair", daemon=True)
    state["gap_repair_worker"] = worker
    worker.start()
    return True


def drain_gap_repair_results(state):
    result_queue = state.setdefault("gap_repair_results", queue.Queue())
    drained = 0
    while True:
        try:
            item = result_queue.get_nowait()
        except queue.Empty:
            break
        drained += 1
        error = item.get("error")
        if error:
            state["status"]["gap_repair_error"] = error
            write_status(state["status"])
            print(f"\n[SecondData] background gap repair warning: {error}", flush=True)
            continue
        result = item.get("result") or {"enabled": True, "reason": "empty_result"}
        repaired = int(result.get("actualAdded") or 0) + int(result.get("syntheticAdded") or 0)
        if repaired:
            state["total_rows"] += repaired
            state["status"]["rows"] = state["total_rows"]
        state["status"].pop("gap_repair_error", None)
        state["status"]["gap_repair"] = result
        write_status(state["status"])
        if repaired or int(result.get("missingBefore") or 0) > 0:
            print(f"\n[SecondData] gap_repair={result}", flush=True)
    worker = state.get("gap_repair_worker")
    if worker is not None and not worker.is_alive():
        state["gap_repair_worker"] = None
    return drained


def count_csv_rows(path):
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            return max(0, sum(1 for _ in f) - 1)
    except Exception:
        return 0


def last_csv_row(path):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        size = min(end, 65536)
        f.seek(end - size)
        text = f.read(size).decode("utf-8", "replace")
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    for line in reversed(lines):
        rec = csv_line_to_record(line)
        if rec is not None:
            return rec["row"]
    return None


def format_second_bars(df):
    out = df.copy()
    for col in ("open", "high", "low", "close", "volume", "quote_volume"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    valid = (
        (out["open"] > 0)
        & (out["high"] > 0)
        & (out["low"] > 0)
        & (out["close"] > 0)
        & (out["high"] >= out["low"])
        & (out["high"] >= out[["open", "close"]].max(axis=1))
        & (out["low"] <= out[["open", "close"]].min(axis=1))
    )
    out = out.loc[valid].copy()
    if out.empty:
        return out
    for col in ["timestamp", "first_trade_time", "last_trade_time"]:
        out[col] = pd.to_datetime(out[col], utc=True, format="ISO8601").dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return out[CSV_COLUMNS]


def _append_formatted_csv_unlocked(out, path, *, dedupe_timestamp=False):
    if out.empty:
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = os.path.exists(path) and os.path.getsize(path) > 0
    if dedupe_timestamp and exists:
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                end = f.tell()
                size = min(end, 2 * 1024 * 1024)
                f.seek(end - size)
                text = f.read(size).decode("utf-8", "replace")
            lines = text.splitlines()
            if end > size and lines:
                lines = lines[1:]
            seen = {line.split(",", 1)[0] for line in lines if line.strip() and not line.startswith("timestamp,")}
            out = out[~out["timestamp"].isin(seen)].copy()
        except Exception:
            pass
    if out.empty:
        return 0
    out.to_csv(path, mode="a", index=False, header=not exists)
    return int(len(out))


def append_formatted_csv(out, path, *, dedupe_timestamp=False):
    with CSV_WRITE_LOCK:
        return _append_formatted_csv_unlocked(out, path, dedupe_timestamp=dedupe_timestamp)


def append_csv(df, path):
    if df.empty:
        return 0
    return append_formatted_csv(format_second_bars(df), path, dedupe_timestamp=True)


def append_daily_shards(df):
    if df.empty:
        return {"enabled": True, "dir": SHARD_DIR, "written_files": [], "rows": 0}
    out = format_second_bars(df)
    days = pd.to_datetime(out["timestamp"], utc=True, format="ISO8601").dt.strftime("%Y-%m-%d")
    written_files = []
    total = 0
    os.makedirs(SHARD_DIR, exist_ok=True)
    for day in sorted(set(days)):
        shard_path = os.path.join(SHARD_DIR, f"{day}.csv")
        part = out[days == day].copy()
        rows = append_formatted_csv(part, shard_path, dedupe_timestamp=True)
        if rows:
            written_files.append({"file": shard_path, "rows": int(rows)})
            total += int(rows)
    return {"enabled": True, "dir": SHARD_DIR, "written_files": written_files, "rows": int(total)}


def fetch_trades(status):
    endpoint = "/fapi/v1/aggTrades" if MARKET == "futures" else "/api/v3/aggTrades"
    params = {"symbol": SYMBOL, "limit": 1000}
    last_id = status.get("last_agg_trade_id")
    if isinstance(last_id, int) and last_id >= 0:
        params["fromId"] = last_id + 1
    else:
        params["startTime"] = utc_ms() - BACKFILL_MINUTES * 60 * 1000
        params["endTime"] = utc_ms()
    rows = request_json(endpoint, params)
    return rows if isinstance(rows, list) else []


def fetch_trades_time_range(start_ms, end_ms):
    endpoint = "/fapi/v1/aggTrades" if MARKET == "futures" else "/api/v3/aggTrades"
    params = {
        "symbol": SYMBOL,
        "limit": 1000,
        "startTime": int(start_ms),
        "endTime": int(end_ms),
    }
    rows = request_json(endpoint, params)
    return rows if isinstance(rows, list) else []


def make_state(status):
    last_row = last_csv_row(OUT_FILE)
    persisted_id = status.get("last_agg_trade_id")
    if not isinstance(persisted_id, int) and last_row:
        try:
            persisted_id = int(float(last_row.get("last_agg_trade_id")))
        except Exception:
            persisted_id = None
    return {
        "status": status,
        "cursor_id": persisted_id,
        "total_rows": int(status.get("rows") or count_csv_rows(OUT_FILE)),
        "pending": pd.DataFrame(),
        "last_flush": 0.0,
        "ws_rows": [],
        "last_ws_flush": time.time(),
        "last_gap_repair": time.time(),
        "gap_repair_worker": None,
        "gap_repair_results": queue.Queue(),
        "last_archive_check": time.time(),
        "archive_worker": None,
        "archive_results": queue.Queue(),
        "nonblocking_writes": False,
        "last_file_repair": time.time(),
    }


def startup_backfill():
    if STARTUP_BACKFILL_MINUTES <= 0:
        return {"enabled": False, "minutes": 0, "bars": 0, "trades": 0}
    end_ms = utc_ms() - FINALIZE_DELAY_SEC * 1000
    start_ms = end_ms - STARTUP_BACKFILL_MINUTES * 60 * 1000
    all_bars = []
    total_trades = 0
    cursor = start_ms
    while cursor < end_ms:
        rows = fetch_trades_time_range(cursor, end_ms)
        if not rows:
            break
        total_trades += len(rows)
        bars = aggregate_trades(rows)
        if not bars.empty:
            all_bars.append(bars)
        last_trade_ms = max(int(row["T"]) for row in rows if "T" in row)
        next_cursor = last_trade_ms + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(rows) < 1000:
            break
        if BACKFILL_SLEEP_SEC > 0:
            time.sleep(BACKFILL_SLEEP_SEC)
    if not all_bars:
        return {"enabled": True, "minutes": STARTUP_BACKFILL_MINUTES, "bars": 0, "trades": total_trades}
    bars = collapse_bars(pd.concat(all_bars, ignore_index=True))
    main_rows = append_formatted_csv(format_second_bars(bars), OUT_FILE, dedupe_timestamp=True)
    shard_write = append_daily_shards(bars)
    status = read_status()
    last_ts = pd.to_datetime(bars["timestamp"].iloc[-1], utc=True).isoformat()
    last_id = int(bars["last_agg_trade_id"].max())
    status.update({
        "ok": True,
        "symbol": SYMBOL,
        "market": MARKET,
        "mode": MODE,
        "file": OUT_FILE,
        "updated_at": iso_now(),
        "error": None,
        "startup_backfill": {
            "enabled": True,
            "minutes": STARTUP_BACKFILL_MINUTES,
            "bars": int(len(bars)),
            "main_added": int(main_rows),
            "trades": int(total_trades),
        },
        "last_ts": last_ts,
        "last_agg_trade_id": last_id,
        "rows": count_csv_rows(OUT_FILE),
        "shards": {"enabled": True, "dir": SHARD_DIR, "last_write": shard_write},
    })
    write_status(status)
    return status["startup_backfill"]


def catch_up_from_last_id(state, max_pages=12):
    """Fill gaps after websocket disconnects using REST aggTrades."""
    cursor_id = state.get("cursor_id")
    if not isinstance(cursor_id, int):
        return {"enabled": True, "pages": 0, "trades": 0, "added": 0, "reason": "missing_cursor"}
    total_trades = 0
    total_added = 0
    pages = 0
    for _ in range(max_pages):
        rows = fetch_trades({"last_agg_trade_id": cursor_id})
        if not rows:
            break
        pages += 1
        total_trades += len(rows)
        cursor_id = max(int(row["a"] if "a" in row else row["t"]) for row in rows)
        state["cursor_id"] = cursor_id
        bars = aggregate_trades(rows)
        if not bars.empty:
            pending = state["pending"]
            state["pending"] = collapse_bars(pd.concat([pending, bars], ignore_index=True) if not pending.empty else bars)
        total_added += flush_state(state, fetched_trades=len(rows), force=True)
        if len(rows) < 1000:
            break
        if BACKFILL_SLEEP_SEC > 0:
            time.sleep(BACKFILL_SLEEP_SEC)
    return {"enabled": True, "pages": pages, "trades": total_trades, "added": total_added}


def catch_up_recent_by_time(state, max_pages=None):
    """Recover the latest seconds even when websocket trade IDs cannot map to REST aggTrade IDs."""
    max_pages = int(max_pages or CATCHUP_MAX_PAGES)
    end_ts = pd.Timestamp.now(tz="UTC").floor("s") - pd.Timedelta(seconds=FINALIZE_DELAY_SEC)
    last_ts = parse_ts(state["status"].get("last_ts"))
    if last_ts is None:
        records = read_recent_records(OUT_FILE, 60)
        if records:
            last_ts = max(rec["dt"] for rec in records)

    reason = "from_last_ts"
    if last_ts is None:
        start_ts = end_ts - pd.Timedelta(minutes=CATCHUP_RECENT_MINUTES)
        reason = "missing_last_ts"
    else:
        start_ts = last_ts + pd.Timedelta(seconds=1)
        max_back = pd.Timedelta(minutes=CATCHUP_RECENT_MINUTES)
        if end_ts - start_ts > max_back:
            start_ts = end_ts - max_back
            reason = "stale_jump_recent"

    if start_ts > end_ts:
        return {"enabled": True, "pages": 0, "trades": 0, "added": 0, "reason": "fresh"}

    start_ms = int(start_ts.timestamp() * 1000)
    end_ms = int((end_ts + pd.Timedelta(milliseconds=999)).timestamp() * 1000)
    rows, pages = fetch_trades_time_range_all(
        start_ms,
        end_ms,
        max_pages=max_pages,
    )
    tail_pages = 0
    if pages >= max_pages:
        tail_start_ms = max(start_ms, end_ms - 90_000)
        tail_rows, tail_pages = fetch_trades_time_range_all(tail_start_ms, end_ms, max_pages=min(30, max_pages))
        rows.extend(tail_rows)
    bars = aggregate_trades(rows)
    if not bars.empty:
        pending = state["pending"]
        state["pending"] = collapse_bars(pd.concat([pending, bars], ignore_index=True) if not pending.empty else bars)
    added = flush_state(state, fetched_trades=len(rows), force=True)
    return {
        "enabled": True,
        "pages": int(pages),
        "tailPages": int(tail_pages),
        "trades": int(len(rows)),
        "added": int(added),
        "reason": reason,
        "start": start_ts.isoformat(),
        "end": end_ts.isoformat(),
    }


def aggregate_trades(rows):
    if not rows:
        return pd.DataFrame()
    records = []
    seen_ids = set()
    for row in rows:
        try:
            price = float(row["p"])
            qty = float(row["q"])
            trade_time = int(row["T"])
            agg_id = int(row["a"] if "a" in row else row["t"])
            if price <= 0 or qty < 0 or trade_time <= 0:
                continue
            if agg_id in seen_ids:
                continue
            seen_ids.add(agg_id)
            taker_sell = bool(row.get("m"))
            records.append({
                "agg_trade_id": agg_id,
                "trade_time": pd.to_datetime(trade_time, unit="ms", utc=True),
                "timestamp": pd.to_datetime((trade_time // 1000) * 1000, unit="ms", utc=True),
                "price": price,
                "qty": qty,
                "quote_qty": price * qty,
                "taker_buy_volume": 0.0 if taker_sell else qty,
                "taker_sell_volume": qty if taker_sell else 0.0,
                "taker_buy_quote": 0.0 if taker_sell else price * qty,
                "taker_sell_quote": price * qty if taker_sell else 0.0,
            })
        except Exception:
            continue
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records).sort_values("trade_time")
    grouped = df.groupby("timestamp", as_index=False).agg(
        open=("price", "first"),
        high=("price", "max"),
        low=("price", "min"),
        close=("price", "last"),
        volume=("qty", "sum"),
        quote_volume=("quote_qty", "sum"),
        trades=("price", "count"),
        taker_buy_volume=("taker_buy_volume", "sum"),
        taker_sell_volume=("taker_sell_volume", "sum"),
        taker_buy_quote=("taker_buy_quote", "sum"),
        taker_sell_quote=("taker_sell_quote", "sum"),
        first_trade_time=("trade_time", "first"),
        last_trade_time=("trade_time", "last"),
        first_agg_trade_id=("agg_trade_id", "min"),
        last_agg_trade_id=("agg_trade_id", "max"),
    )
    grouped["taker_buy_sell_ratio"] = [
        999.0 if sell == 0 and buy > 0 else (0.0 if sell == 0 else buy / sell)
        for buy, sell in zip(grouped["taker_buy_volume"], grouped["taker_sell_volume"])
    ]
    grouped["symbol"] = SYMBOL
    grouped["market"] = MARKET
    return grouped


def _flush_state_unlocked(state, fetched_trades=0, force=False):
    status = state["status"]
    pending = state["pending"]
    cutoff = pd.Timestamp.now(tz="UTC").floor("s") - pd.Timedelta(seconds=FINALIZE_DELAY_SEC)
    if pending.empty:
        complete = pending
    else:
        complete = pending[pending["timestamp"] <= cutoff].copy()
        pending = pending[pending["timestamp"] > cutoff].copy().reset_index(drop=True)
    if complete.empty and not force and time.time() - state["last_flush"] < STATUS_INTERVAL_SEC:
        state["pending"] = pending
        return 0

    added = append_csv(complete, OUT_FILE)
    shard_write = append_daily_shards(complete) if added else {"enabled": True, "dir": SHARD_DIR, "written_files": [], "rows": 0}
    state["total_rows"] += added
    last_ts = status.get("last_ts")
    last_persisted_id = status.get("last_agg_trade_id")
    if added:
        last_ts = pd.to_datetime(complete["timestamp"].iloc[-1], utc=True).isoformat()
        last_persisted_id = int(complete["last_agg_trade_id"].max())
        status["last_agg_trade_id"] = last_persisted_id

    status.update({
        "ok": True,
        "symbol": SYMBOL,
        "market": MARKET,
        "mode": MODE,
        "file": OUT_FILE,
        "updated_at": iso_now(),
        "error": None,
        "fetched_trades": int(fetched_trades),
        "rows": state["total_rows"],
        "added": added,
        "last_ts": last_ts,
        "pending_seconds": int(len(pending)),
        "last_agg_trade_id": last_persisted_id,
        "shards": {"enabled": True, "dir": SHARD_DIR, "last_write": shard_write},
    })
    write_status(status)
    state["pending"] = pending
    state["last_flush"] = time.time()
    return added


def flush_state(state, fetched_trades=0, force=False):
    nonblocking = bool(state.get("nonblocking_writes")) and not force
    acquired = CSV_WRITE_LOCK.acquire(blocking=not nonblocking)
    if not acquired:
        return 0
    try:
        return _flush_state_unlocked(state, fetched_trades=fetched_trades, force=force)
    finally:
        CSV_WRITE_LOCK.release()


def flush_ws_rows(state, force=False):
    rows = state.get("ws_rows") or []
    if not rows and not force:
        return 0
    now = time.time()
    if (
        not force
        and len(rows) < WS_FLUSH_MAX_TRADES
        and now - float(state.get("last_ws_flush") or 0) < WS_FLUSH_INTERVAL_SEC
    ):
        return 0
    state["ws_rows"] = []
    state["last_ws_flush"] = now
    bars = aggregate_trades(rows)
    if not bars.empty:
        pending = state["pending"]
        state["pending"] = collapse_bars(pd.concat([pending, bars], ignore_index=True) if not pending.empty else bars)
    return flush_state(state, fetched_trades=len(rows), force=force)



def _archive_old_main_rows_unlocked():
    """Archive old rows from the hot CSV without loading the whole file."""
    if not os.path.exists(OUT_FILE):
        return 0
    try:
        size = os.path.getsize(OUT_FILE)
    except OSError:
        return 0
    if size < 5 * 1024 * 1024:  # <5MB 不处理
        return 0

    cutoff_date = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=MAIN_FILE_KEEP_DAYS)).date()
    os.makedirs(SHARD_DIR, exist_ok=True)
    tmp = OUT_FILE + ".archive_tmp"
    shard_handles = {}
    shard_seen = {}
    archived = 0
    kept = 0

    def shard_writer(day, header):
        if day in shard_handles:
            return shard_handles[day]
        shard_path = os.path.join(SHARD_DIR, f"{day}.csv")
        exists = os.path.exists(shard_path) and os.path.getsize(shard_path) > 0
        seen = set()
        if exists:
            try:
                with open(shard_path, "r", encoding="utf-8", newline="") as sf:
                    next(sf, None)
                    seen = {line.rstrip("\r\n") for line in sf if line.strip()}
            except Exception:
                seen = set()
        handle = open(shard_path, "a", encoding="utf-8", newline="")
        if not exists:
            handle.write(header)
        shard_handles[day] = handle
        shard_seen[day] = seen
        return handle

    try:
        with open(OUT_FILE, "r", encoding="utf-8", newline="") as src, open(tmp, "w", encoding="utf-8", newline="") as keep:
            header = src.readline()
            if not header or "timestamp" not in header.split(","):
                return 0
            keep.write(header)
            for line in src:
                raw = line.rstrip("\r\n")
                if not raw:
                    continue
                ts = raw.split(",", 1)[0]
                try:
                    day = pd.Timestamp(ts).date()
                except Exception:
                    keep.write(line)
                    kept += 1
                    continue
                if day < cutoff_date:
                    handle = shard_writer(str(day), header)
                    seen = shard_seen[str(day)]
                    if raw not in seen:
                        handle.write(line)
                        seen.add(raw)
                    archived += 1
                else:
                    keep.write(line)
                    kept += 1
    finally:
        for handle in shard_handles.values():
            try:
                handle.close()
            except Exception:
                pass

    if archived <= 0:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return 0

    os.replace(tmp, OUT_FILE)
    print(f"\n[SecondData] archived {archived} old rows to shards, main file kept {kept} rows", flush=True)
    return archived


def archive_old_main_rows():
    with CSV_WRITE_LOCK:
        return _archive_old_main_rows_unlocked()


def schedule_archive(state, force=False):
    now = time.time()
    if not force and now - float(state.get("last_archive_check") or 0) < 3600:
        return False
    worker = state.get("archive_worker")
    if worker is not None and worker.is_alive():
        return False
    state["last_archive_check"] = now
    result_queue = state.setdefault("archive_results", queue.Queue())

    def run():
        try:
            result_queue.put({"archived": int(archive_old_main_rows()), "error": None})
        except Exception as exc:
            result_queue.put({"archived": 0, "error": str(exc)})

    worker = threading.Thread(target=run, name="second-data-archive", daemon=True)
    state["archive_worker"] = worker
    worker.start()
    return True


def drain_archive_results(state):
    result_queue = state.setdefault("archive_results", queue.Queue())
    drained = 0
    while True:
        try:
            item = result_queue.get_nowait()
        except queue.Empty:
            break
        drained += 1
        error = item.get("error")
        if error:
            state["status"]["archive_error"] = error
            write_status(state["status"])
            print(f"\n[SecondData] archive warning: {error}", flush=True)
            continue
        state["status"].pop("archive_error", None)
        if int(item.get("archived") or 0) > 0:
            with CSV_WRITE_LOCK:
                state["total_rows"] = count_csv_rows(OUT_FILE)
            state["status"]["rows"] = state["total_rows"]
            write_status(state["status"])
    worker = state.get("archive_worker")
    if worker is not None and not worker.is_alive():
        state["archive_worker"] = None
    return drained


def collapse_bars(bars):
    if bars.empty:
        return bars
    merged = bars.copy()
    merged["timestamp"] = pd.to_datetime(merged["timestamp"], utc=True, format="ISO8601")
    merged["first_trade_time"] = pd.to_datetime(merged["first_trade_time"], utc=True, format="ISO8601", errors="coerce")
    merged["last_trade_time"] = pd.to_datetime(merged["last_trade_time"], utc=True, format="ISO8601", errors="coerce")
    numeric_sum_cols = [
        "volume",
        "quote_volume",
        "trades",
        "taker_buy_volume",
        "taker_sell_volume",
        "taker_buy_quote",
        "taker_sell_quote",
    ]
    rows = []
    for _, group in merged.sort_values(["timestamp", "first_trade_time"]).groupby("timestamp", as_index=False):
        first = group.iloc[0]
        last = group.iloc[-1]
        row = {
            "timestamp": group["timestamp"].iloc[0],
            "symbol": last.get("symbol", SYMBOL),
            "market": last.get("market", MARKET),
            "open": first["open"],
            "high": group["high"].max(),
            "low": group["low"].min(),
            "close": last["close"],
            "first_trade_time": group["first_trade_time"].min(),
            "last_trade_time": group["last_trade_time"].max(),
            "first_agg_trade_id": int(group["first_agg_trade_id"].min()),
            "last_agg_trade_id": int(group["last_agg_trade_id"].max()),
        }
        for col in numeric_sum_cols:
            row[col] = group[col].sum()
        sell = row["taker_sell_volume"]
        buy = row["taker_buy_volume"]
        row["taker_buy_sell_ratio"] = 999.0 if sell == 0 and buy > 0 else (0.0 if sell == 0 else buy / sell)
        rows.append(row)
    out = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)
    return out[CSV_COLUMNS]


def run_rest_loop():
    print(f"[SecondData] Starting {SYMBOL} {MARKET} REST collector -> {OUT_FILE}")
    status = read_status()
    state = make_state(status)
    _last_archive_check = 0.0  # 归档检查时间戳（每小时触发一次）
    while True:
        started = time.time()
        try:
            cursor_id = state["cursor_id"]
            fetch_status = {"last_agg_trade_id": cursor_id} if isinstance(cursor_id, int) else {}
            rows = fetch_trades(fetch_status)
            bars = aggregate_trades(rows)
            if rows:
                state["cursor_id"] = max(int(row["a"]) for row in rows if "a" in row)
            if not bars.empty:
                pending = state["pending"]
                state["pending"] = collapse_bars(pd.concat([pending, bars], ignore_index=True) if not pending.empty else bars)
            flush_state(state, fetched_trades=len(rows), force=True)
            repair_recent_gaps(state)
            # 每小时触发一次归档检查
            if time.time() - _last_archive_check >= 3600:
                try:
                    maybe_repair_hot_csv(state, force=True, rewrite_out_of_order=False)
                    archive_old_main_rows()
                except Exception as _arc_exc:
                    print(f"\n[SecondData] archive warning: {_arc_exc}", flush=True)
                _last_archive_check = time.time()
            print(
                f"\r[SecondData] rows={state['status'].get('rows')} last={state['status'].get('last_ts')} "
                f"trades={len(rows)} pending={len(state['pending'])}",
                end="",
                flush=True,
            )
        except Exception as exc:
            retry_after = getattr(exc, "retry_after_sec", None)
            state["status"].update({
                "ok": False,
                "symbol": SYMBOL,
                "market": MARKET,
                "mode": MODE,
                "file": OUT_FILE,
                "updated_at": iso_now(),
                "error": str(exc),
            })
            if retry_after is not None:
                state["status"]["rate_limit_backoff_sec"] = int(retry_after)
            write_status(state["status"])
            print(f"\n[SecondData] Error: {exc}", flush=True)
            if retry_after is not None:
                print(f"[SecondData] Rate limited; sleeping {int(retry_after)}s", flush=True)
                time.sleep(float(retry_after))
        elapsed = time.time() - started
        time.sleep(max(0.0, INTERVAL_SEC - elapsed))


def _websocket_proxy_args():
    from urllib.parse import urlparse

    proxy = (
        os.environ.get("SECOND_DATA_WS_PROXY")
        or os.environ.get("ALL_PROXY")
        or os.environ.get("all_proxy")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
    )
    if not proxy:
        return {}
    parsed = urlparse(proxy)
    if parsed.hostname not in ("127.0.0.1", "localhost") and os.environ.get("SECOND_DATA_WS_USE_PROXY", "1") == "0":
        return {}
    scheme = (parsed.scheme or "").lower()
    proxy_type = "socks5h" if scheme.startswith("socks5") else "http"
    return {
        "http_proxy_host": parsed.hostname,
        "http_proxy_port": parsed.port,
        "proxy_type": proxy_type,
    }


def run_websocket_loop():
    print(f"[SecondData] Starting {SYMBOL} {MARKET} websocket collector {WS_URL} -> {OUT_FILE}")
    status = read_status()
    state = make_state(status)
    state["nonblocking_writes"] = True
    state["status"].update({
        "ok": True,
        "symbol": SYMBOL,
        "market": MARKET,
        "mode": MODE,
        "websocket_url": WS_URL,
        "file": OUT_FILE,
        "updated_at": iso_now(),
        "cursor_matches_rest": websocket_cursor_matches_rest(),
    })
    write_status(state["status"])

    def on_open(_ws):
        state["status"].update({
            "ok": True,
            "symbol": SYMBOL,
            "market": MARKET,
            "mode": MODE,
            "websocket_url": WS_URL,
            "file": OUT_FILE,
            "updated_at": iso_now(),
            "error": None,
        })
        write_status(state["status"])
        print("[SecondData] WebSocket connected", flush=True)

    def on_message(_ws, message):
        try:
            row = json.loads(message)
            if isinstance(row, dict) and isinstance(row.get("data"), dict):
                row = row["data"]
            if isinstance(row, dict) and row.get("e") not in ("trade", "aggTrade"):
                return
            if not isinstance(row, dict) or ("a" not in row and "t" not in row):
                return
            state["cursor_id"] = int(row["a"] if "a" in row else row["t"])
            state["ws_rows"].append(row)
            added = flush_ws_rows(state)
            drain_gap_repair_results(state)
            schedule_gap_repair(state)
            drain_archive_results(state)
            schedule_archive(state)
            if added:
                print(
                    f"\r[SecondData] rows={state['status'].get('rows')} last={state['status'].get('last_ts')} "
                    f"ws={len(state.get('ws_rows') or [])} pending={len(state['pending'])}",
                    end="",
                    flush=True,
                )
        except Exception as exc:
            state["status"].update({
                "ok": False,
                "symbol": SYMBOL,
                "market": MARKET,
                "mode": MODE,
                "websocket_url": WS_URL,
                "file": OUT_FILE,
                "updated_at": iso_now(),
                "error": str(exc),
            })
            write_status(state["status"])
            print(f"\n[SecondData] WebSocket message error: {exc}", flush=True)

    def on_error(_ws, error):
        state["status"].update({
            "ok": False,
            "symbol": SYMBOL,
            "market": MARKET,
            "mode": MODE,
            "websocket_url": WS_URL,
            "file": OUT_FILE,
            "updated_at": iso_now(),
            "error": str(error),
        })
        write_status(state["status"])
        print(f"\n[SecondData] WebSocket error: {error}", flush=True)

    def on_close(_ws, code, reason):
        flush_ws_rows(state, force=True)
        drain_gap_repair_results(state)
        drain_archive_results(state)
        print(f"\n[SecondData] WebSocket closed code={code} reason={reason}", flush=True)

    while True:
        if websocket_cursor_matches_rest():
            try:
                catchup = catch_up_from_last_id(state)
                if catchup.get("trades") or catchup.get("added"):
                    state["status"]["last_catchup"] = catchup
                    write_status(state["status"])
                    print(f"\n[SecondData] catchup={catchup}", flush=True)
            except Exception as exc:
                state["status"].update({
                    "ok": False,
                    "symbol": SYMBOL,
                    "market": MARKET,
                    "mode": MODE,
                    "websocket_url": WS_URL,
                    "file": OUT_FILE,
                    "updated_at": iso_now(),
                    "error": f"catchup_failed: {exc}",
                })
                write_status(state["status"])
                print(f"\n[SecondData] catchup warning: {exc}", flush=True)
        else:
            try:
                catchup = catch_up_recent_by_time(state)
                if catchup.get("trades") or catchup.get("added") or catchup.get("reason") == "stale_jump_recent":
                    state["status"]["last_time_catchup"] = catchup
                    write_status(state["status"])
                    print(f"\n[SecondData] time_catchup={catchup}", flush=True)
                repair_recent_gaps(state, force=True)
            except Exception as exc:
                state["status"].update({
                    "ok": False,
                    "symbol": SYMBOL,
                    "market": MARKET,
                    "mode": MODE,
                    "websocket_url": WS_URL,
                    "file": OUT_FILE,
                    "updated_at": iso_now(),
                    "error": f"time_catchup_failed: {exc}",
                })
                write_status(state["status"])
                print(f"\n[SecondData] pre-ws time catchup warning: {exc}", flush=True)
        ws = websocket.WebSocketApp(
            WS_URL,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        ws.run_forever(
            ping_interval=WS_PING_INTERVAL_SEC,
            ping_timeout=WS_PING_TIMEOUT_SEC,
            **_websocket_proxy_args(),
        )
        time.sleep(max(1.0, min(INTERVAL_SEC, 10.0)))


def main():
    lock_handle, lock_socket = acquire_singleton_lock()
    try:
        if STARTUP_FILE_REPAIR:
            startup_state = make_state(read_status())
            maybe_repair_hot_csv(startup_state, force=True)
    except Exception as exc:
        status = read_status()
        status.update({
            "ok": False,
            "symbol": SYMBOL,
            "market": MARKET,
            "mode": MODE,
            "file": OUT_FILE,
            "updated_at": iso_now(),
            "error": f"file_repair_failed: {exc}",
        })
        write_status(status)
        print(f"[SecondData] file repair warning: {exc}", flush=True)
    try:
        result = startup_backfill()
        if result.get("enabled"):
            print(f"[SecondData] startup_backfill={result}", flush=True)
    except Exception as exc:
        status = read_status()
        status.update({
            "ok": False,
            "symbol": SYMBOL,
            "market": MARKET,
            "mode": MODE,
            "file": OUT_FILE,
            "updated_at": iso_now(),
            "error": f"startup_backfill_failed: {exc}",
        })
        write_status(status)
        print(f"[SecondData] startup backfill warning: {exc}", flush=True)
    if MODE in ("rest", "poll", "polling"):
        run_rest_loop()
    else:
        run_websocket_loop()
    return lock_handle, lock_socket


if __name__ == "__main__":
    main()
