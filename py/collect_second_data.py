"""Collect BTCUSDT second-level trade bars for future backtests.

The live strategy still uses the existing 1m/2m pipeline. This service only
stores raw-ish second bars so later research can test second-level entry timing
and taker-flow filters without changing production signals.
"""
import atexit
import csv
import json
import os
import shutil
import socket
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import requests

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
INTERVAL_SEC = max(0.5, float(os.environ.get("SECOND_DATA_INTERVAL_SEC", "1")))
BACKFILL_MINUTES = max(1, int(os.environ.get("SECOND_DATA_BACKFILL_MINUTES", "10")))
RETENTION_DAYS = max(1, int(os.environ.get("SECOND_DATA_RETENTION_DAYS", "120")))
HTTP_TIMEOUT = float(os.environ.get("SECOND_DATA_HTTP_TIMEOUT", "8"))
FINALIZE_DELAY_SEC = max(1, int(os.environ.get("SECOND_DATA_FINALIZE_DELAY_SEC", "2")))

OUT_FILE = os.path.join(OUT, f"{SYMBOL.lower()}_1s_trades.csv")
STATUS_FILE = os.path.join(OUT, "second_data_status.json")
LOCK_FILE = os.path.join(OUT, "second_data.lock")
LOCK_DIR = os.path.join(OUT, "second_data.lockdir")
LOCK_PORT = int(os.environ.get("SECOND_DATA_LOCK_PORT", "39872"))

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
                r.raise_for_status()
                data = r.json()
                if isinstance(data, dict) and data.get("code"):
                    raise RuntimeError(f"{data.get('code')}: {data.get('msg')}")
                return data
            except Exception as exc:
                errors.append(f"{url} attempt={attempt + 1}: {exc}")
                time.sleep(0.25 + attempt * 0.5)
    raise RuntimeError("; ".join(errors[-4:]))


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


def count_csv_rows(path):
    if not os.path.exists(OUT_FILE):
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
    header = lines[0].split(",") if text.find("\n") == len(lines[0]) else CSV_COLUMNS
    try:
        return dict(zip(header, next(csv.reader([lines[-1]]))))
    except Exception:
        return None


def append_csv(df, path):
    if df.empty:
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = os.path.exists(path) and os.path.getsize(path) > 0
    out = df.copy()
    for col in ["timestamp", "first_trade_time", "last_trade_time"]:
        out[col] = pd.to_datetime(out[col], utc=True, format="ISO8601").dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    out = out[CSV_COLUMNS]
    out.to_csv(path, mode="a", index=False, header=not exists)
    return int(len(out))


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


def aggregate_trades(rows):
    if not rows:
        return pd.DataFrame()
    records = []
    for row in rows:
        try:
            price = float(row["p"])
            qty = float(row["q"])
            trade_time = int(row["T"])
            agg_id = int(row["a"])
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


def main():
    acquire_singleton_lock()
    print(f"[SecondData] Starting {SYMBOL} {MARKET} collector -> {OUT_FILE}")
    status = read_status()
    last_row = last_csv_row(OUT_FILE)
    persisted_id = status.get("last_agg_trade_id")
    if not isinstance(persisted_id, int) and last_row:
        try:
            persisted_id = int(float(last_row.get("last_agg_trade_id")))
        except Exception:
            persisted_id = None
    cursor_id = persisted_id
    total_rows = int(status.get("rows") or count_csv_rows(OUT_FILE))
    pending = pd.DataFrame()
    while True:
        started = time.time()
        try:
            fetch_status = {"last_agg_trade_id": cursor_id} if isinstance(cursor_id, int) else {}
            rows = fetch_trades(fetch_status)
            bars = aggregate_trades(rows)
            if rows:
                cursor_id = max(int(row["a"]) for row in rows if "a" in row)
            if not bars.empty:
                pending = collapse_bars(pd.concat([pending, bars], ignore_index=True) if not pending.empty else bars)
            cutoff = pd.Timestamp.now(tz="UTC").floor("s") - pd.Timedelta(seconds=FINALIZE_DELAY_SEC)
            if pending.empty:
                complete = pending
            else:
                complete = pending[pending["timestamp"] <= cutoff].copy()
                pending = pending[pending["timestamp"] > cutoff].copy().reset_index(drop=True)
            added = append_csv(complete, OUT_FILE)
            total_rows += added
            last_ts = None
            last_persisted_id = status.get("last_agg_trade_id")
            if added:
                last_ts = pd.to_datetime(complete["timestamp"].iloc[-1], utc=True).isoformat()
                last_persisted_id = int(complete["last_agg_trade_id"].max())
                status["last_agg_trade_id"] = last_persisted_id
            elif last_row and not status.get("last_ts"):
                last_ts = last_row.get("timestamp")
            else:
                last_ts = status.get("last_ts")
            status.update({
                "ok": True,
                "symbol": SYMBOL,
                "market": MARKET,
                "file": OUT_FILE,
                "updated_at": iso_now(),
                "error": None,
                "fetched_trades": int(len(rows)),
                "rows": total_rows,
                "added": added,
                "last_ts": last_ts,
                "pending_seconds": int(len(pending)),
                "last_agg_trade_id": last_persisted_id,
            })
            write_status(status)
            print(
                f"\r[SecondData] rows={status.get('rows')} last={status.get('last_ts')} "
                f"trades={len(rows)} pending={len(pending)}",
                end="",
                flush=True,
            )
        except Exception as exc:
            status.update({
                "ok": False,
                "symbol": SYMBOL,
                "market": MARKET,
                "file": OUT_FILE,
                "updated_at": iso_now(),
                "error": str(exc),
            })
            write_status(status)
            print(f"\n[SecondData] Error: {exc}", flush=True)
        elapsed = time.time() - started
        time.sleep(max(0.0, INTERVAL_SEC - elapsed))


if __name__ == "__main__":
    main()
