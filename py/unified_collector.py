"""Unified data collector — single process coordinating all Binance REST calls.

Consolidates:
  price_proxy.py        (ticker price,    Spot,     every 2s)
  collect_second_data.py (aggTrades,      Futures,  every 1s)
  update_live_data.py   (klines/taker/ls/funding,  Spot+Futures, every 5min)

Outputs:
  current_price.json        — latest BTC price for server.js
  btcusdt_1s_trades.csv     — second-level trade bars
  btcusdt_1m.csv            — 1-minute klines (for signal_btc.py)
  btcusdt_taker.csv         — taker buy/sell ratio (5min)
  btcusdt_lsratio.csv       — long/short position ratio (5min)
  btcusdt_funding.csv       — funding rate
"""
import atexit, csv, json, os, shutil, signal, socket, sys, time, threading
from datetime import datetime, timezone
from collections import defaultdict

import pandas as pd
import requests

# ── Config ────────────────────────────────────────────────────────────────────
APP_DIR = os.environ.get("APP_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("DATA_DIR", os.path.join(APP_DIR, "data"))
SYMBOL = os.environ.get("COLLECTOR_SYMBOL", "BTCUSDT").upper()

INTERVAL_PRICE   = float(os.environ.get("COLLECTOR_PRICE_SEC",   "2"))
INTERVAL_1S      = float(os.environ.get("COLLECTOR_1S_SEC",      "1"))
INTERVAL_1M      = float(os.environ.get("COLLECTOR_1M_SEC",     "30"))
INTERVAL_PERIOD  = float(os.environ.get("COLLECTOR_PERIOD_SEC", "300"))
BACKFILL_1M_DAYS = int(os.environ.get("COLLECTOR_BACKFILL_1M",   "7"))
BACKFILL_1S_MIN  = int(os.environ.get("COLLECTOR_BACKFILL_1S",  "10"))
FINALIZE_DELAY   = int(os.environ.get("COLLECTOR_FINALIZE_SEC",  "2"))
RETENTION_1S     = int(os.environ.get("COLLECTOR_RETENTION_1S", "120"))
HTTP_TIMEOUT     = float(os.environ.get("COLLECTOR_HTTP_TIMEOUT","8"))

SPOT_BASES = [
    b.strip().rstrip("/") for b in os.environ.get(
        "COLLECTOR_SPOT_BASES",
        "https://data-api.binance.vision,https://api.binance.com,https://api1.binance.com"
    ).split(",") if b.strip()
]
FAPI_BASES = [
    b.strip().rstrip("/") for b in os.environ.get(
        "COLLECTOR_FAPI_BASES",
        "https://fapi.binance.com,https://fapi1.binance.com,https://fapi2.binance.com"
    ).split(",") if b.strip()
]

# Output files
PRICE_FILE   = os.path.join(OUT, "current_price.json")
FILE_1S      = os.path.join(OUT, f"{SYMBOL.lower()}_1s_trades.csv")
FILE_1M      = os.path.join(OUT, f"{SYMBOL.lower()}_1m.csv")
FILE_TAKER   = os.path.join(OUT, f"{SYMBOL.lower()}_taker.csv")
FILE_LSRATIO = os.path.join(OUT, f"{SYMBOL.lower()}_lsratio.csv")
FILE_FUNDING = os.path.join(OUT, f"{SYMBOL.lower()}_funding.csv")
STATUS_FILE  = os.path.join(OUT, "collector_status.json")
LOCK_DIR     = os.path.join(OUT, "collector.lockdir")
LOCK_FILE    = os.path.join(OUT, "collector.lock")
LOCK_PORT    = int(os.environ.get("COLLECTOR_LOCK_PORT", "39873"))

# Binance rate limits
SPOT_WEIGHT_LIMIT   = 6000   # weight/min
FUTURES_WEIGHT_LIMIT = 2400  # weight/min
WEIGHT_WARN_PCT     = 0.75

# Shutdown flag
_shutdown = threading.Event()

CSV_1S_COLS = [
    "timestamp", "symbol", "market", "open", "high", "low", "close",
    "volume", "quote_volume", "trades", "taker_buy_volume",
    "taker_sell_volume", "taker_buy_quote", "taker_sell_quote",
    "taker_buy_sell_ratio", "first_trade_time", "last_trade_time",
    "first_agg_trade_id", "last_agg_trade_id",
]

# ── Utilities ─────────────────────────────────────────────────────────────────

def utc_ms():
    return int(time.time() * 1000)

def iso_now():
    return datetime.now(timezone.utc).isoformat()

def log(tag, msg, **kwargs):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{tag}] {msg}", **kwargs)


# ── Singleton Lock ───────────────────────────────────────────────────────────

def acquire_lock():
    os.makedirs(OUT, exist_ok=True)
    try:
        os.mkdir(LOCK_DIR)
    except FileExistsError:
        pid_path = os.path.join(LOCK_DIR, "pid")
        try:
            with open(pid_path, "r") as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)
            log("Lock", f"Another instance active pid={old_pid}; exiting.")
            sys.exit(0)
        except Exception:
            shutil.rmtree(LOCK_DIR, ignore_errors=True)
            os.mkdir(LOCK_DIR)
    with open(os.path.join(LOCK_DIR, "pid"), "w") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: shutil.rmtree(LOCK_DIR, ignore_errors=True))
    handle = open(LOCK_FILE, "a+", encoding="utf-8")
    try:
        handle.seek(0)
        try:
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except ImportError:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
    except OSError:
        log("Lock", "Another instance holds the lock; exiting.")
        sys.exit(0)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", LOCK_PORT))
        sock.listen(1)
    except OSError:
        log("Lock", f"Port {LOCK_PORT} occupied; exiting.")
        sys.exit(0)
    return handle, sock


# ── Rate Limiter ─────────────────────────────────────────────────────────────

class RateLimiter:
    def __init__(self):
        self._lock = threading.Lock()
        self._windows = defaultdict(list)  # market -> [timestamps]

    def check_and_add(self, market, weight):
        now = time.time()
        cutoff = now - 60
        limit = SPOT_WEIGHT_LIMIT if market == "spot" else FUTURES_WEIGHT_LIMIT
        with self._lock:
            w = self._windows[market]
            self._windows[market] = [(t, wt) for t, wt in w if t > cutoff]
            w = self._windows[market]
            used = sum(wt for _, wt in w)
            if used + weight > limit:
                return False, used, limit
            w.append((now, weight))
            return True, used + weight, limit

    def get_usage(self):
        now = time.time()
        cutoff = now - 60
        result = {}
        with self._lock:
            for mkt in ("spot", "futures"):
                w = [(t, wt) for t, wt in self._windows.get(mkt, []) if t > cutoff]
                self._windows[mkt] = w
                used = sum(wt for _, wt in w)
                limit = SPOT_WEIGHT_LIMIT if mkt == "spot" else FUTURES_WEIGHT_LIMIT
                result[mkt] = {"used": used, "limit": limit, "pct": used / limit * 100}
        return result


# ── HTTP Layer ────────────────────────────────────────────────────────────────

def request_json(path, params, market, weight, limiter, retries=3):
    bases = SPOT_BASES if market == "spot" else FAPI_BASES
    errors = []
    for base in bases:
        url = base + path
        for attempt in range(retries):
            if _shutdown.is_set():
                return None
            ok, used, limit = limiter.check_and_add(market, weight)
            if not ok:
                log("Rate", f"{market} limit reached ({used}/{limit}), backing off 5s")
                time.sleep(5)
                continue
            try:
                r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
                r.raise_for_status()
                data = r.json()
                if isinstance(data, dict) and data.get("code"):
                    raise RuntimeError(f"{data.get('code')}: {data.get('msg')}")
                return data
            except Exception as e:
                errors.append(f"{url}: {e}")
                time.sleep(0.3 + attempt * 0.5)
    raise RuntimeError("; ".join(errors[-3:]))


# ── File I/O ──────────────────────────────────────────────────────────────────

def atomic_json_write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def atomic_csv_write(df, path, columns=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    if columns:
        df = df[columns]
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


def append_csv_rows(df, path, columns):
    if df.empty:
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = os.path.exists(path) and os.path.getsize(path) > 0
    out = df.copy()
    for col in ["timestamp", "first_trade_time", "last_trade_time", "open_time"]:
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    out = out[columns]
    out.to_csv(path, mode="a", index=False, header=not exists)
    return len(out)


def read_csv(path, time_col):
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
        if df.empty or time_col not in df.columns:
            return pd.DataFrame()
        df[time_col] = pd.to_datetime(df[time_col], utc=True, format="ISO8601")
        return df.dropna(subset=[time_col]).sort_values(time_col).reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def csv_row_count(path):
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r") as f:
            return max(0, sum(1 for _ in f) - 1)
    except Exception:
        return 0


# ── Task: Price Ticker ───────────────────────────────────────────────────────

def task_price(limiter):
    """Fetch BTCUSDT spot price, write to current_price.json."""
    data = request_json(
        "/api/v3/ticker/price",
        {"symbol": SYMBOL}, "spot", 2, limiter
    )
    if data is None:
        return {"fetched": False}
    price = float(data["price"])
    atomic_json_write(PRICE_FILE, {"price": price, "time": utc_ms()})
    return {"fetched": True, "price": price}


# ── Task: Second-Level Trades ────────────────────────────────────────────────

class SecondTraderState:
    def __init__(self):
        self.last_agg_id = None
        self.pending = pd.DataFrame()
        self.total_rows = csv_row_count(FILE_1S)
        self._load_state()

    def _load_state(self):
        try:
            st = json.load(open(os.path.join(OUT, "second_data_status.json")))
            if isinstance(st.get("last_agg_trade_id"), int):
                self.last_agg_id = st["last_agg_trade_id"]
        except Exception:
            pass
        if self.last_agg_id is None and os.path.exists(FILE_1S):
            try:
                df = pd.read_csv(FILE_1S)
                if not df.empty and "last_agg_trade_id" in df.columns:
                    self.last_agg_id = int(df["last_agg_trade_id"].iloc[-1])
            except Exception:
                pass


def task_1s_trades(state, limiter):
    """Fetch aggTrades and aggregate into 1-second bars."""
    params = {"symbol": SYMBOL, "limit": 1000}
    if state.last_agg_id is not None:
        params["fromId"] = state.last_agg_id + 1
    else:
        now = utc_ms()
        params["startTime"] = now - BACKFILL_1S_MIN * 60_000
        params["endTime"] = now

    rows = request_json("/fapi/v1/aggTrades", params, "futures", 4, limiter)
    if not rows or not isinstance(rows, list):
        return {"fetched": 0, "added": 0}

    # Update cursor
    state.last_agg_id = max(int(r["a"]) for r in rows if "a" in r)

    # Aggregate trades into 1-second bars
    records = []
    for r in rows:
        try:
            price = float(r["p"])
            qty = float(r["q"])
            tt = int(r["T"])
            taker_sell = bool(r.get("m"))
            records.append({
                "agg_id": int(r["a"]),
                "trade_time": pd.to_datetime(tt, unit="ms", utc=True),
                "timestamp": pd.to_datetime((tt // 1000) * 1000, unit="ms", utc=True),
                "price": price, "qty": qty,
                "quote_qty": price * qty,
                "taker_buy_vol": 0.0 if taker_sell else qty,
                "taker_sell_vol": qty if taker_sell else 0.0,
                "taker_buy_qv": 0.0 if taker_sell else price * qty,
                "taker_sell_qv": price * qty if taker_sell else 0.0,
            })
        except Exception:
            continue

    if not records:
        return {"fetched": len(rows), "added": 0}

    df = pd.DataFrame(records).sort_values("trade_time")
    bars = df.groupby("timestamp", as_index=False).agg(
        open=("price", "first"), high=("price", "max"),
        low=("price", "min"), close=("price", "last"),
        volume=("qty", "sum"), quote_volume=("quote_qty", "sum"),
        trades=("price", "count"),
        taker_buy_volume=("taker_buy_vol", "sum"),
        taker_sell_volume=("taker_sell_vol", "sum"),
        taker_buy_quote=("taker_buy_qv", "sum"),
        taker_sell_quote=("taker_sell_qv", "sum"),
        first_trade_time=("trade_time", "first"),
        last_trade_time=("trade_time", "last"),
        first_agg_trade_id=("agg_id", "min"),
        last_agg_trade_id=("agg_id", "max"),
    )
    bars["taker_buy_sell_ratio"] = [
        999.0 if s == 0 and b > 0 else (0.0 if s == 0 else b / s)
        for b, s in zip(bars["taker_buy_volume"], bars["taker_sell_volume"])
    ]
    bars["symbol"] = SYMBOL
    bars["market"] = "futures"

    # Merge with pending
    if not state.pending.empty:
        bars = pd.concat([state.pending, bars], ignore_index=True)

    # Finalize completed bars (older than FINALIZE_DELAY seconds)
    cutoff = pd.Timestamp.now(tz="UTC").floor("s") - pd.Timedelta(seconds=FINALIZE_DELAY)
    complete = bars[bars["timestamp"] <= cutoff].copy()
    state.pending = bars[bars["timestamp"] > cutoff].copy().reset_index(drop=True)

    added = 0
    if not complete.empty:
        # Collapse duplicate timestamps
        complete = _collapse_1s_bars(complete)
        added = append_csv_rows(complete, FILE_1S, CSV_1S_COLS)
        state.total_rows += added

    return {"fetched": len(rows), "added": added, "rows": state.total_rows}


def _collapse_1s_bars(bars):
    """Merge rows with same timestamp."""
    bars["timestamp"] = pd.to_datetime(bars["timestamp"], utc=True)
    sum_cols = ["volume", "quote_volume", "trades",
                "taker_buy_volume", "taker_sell_volume",
                "taker_buy_quote", "taker_sell_quote"]
    rows = []
    for _, group in bars.sort_values("timestamp").groupby("timestamp", as_index=False):
        first = group.iloc[0]
        last = group.iloc[-1]
        row = {
            "timestamp": group["timestamp"].iloc[0],
            "symbol": last.get("symbol", SYMBOL),
            "market": last.get("market", "futures"),
            "open": first["open"], "high": group["high"].max(),
            "low": group["low"].min(), "close": last["close"],
            "first_trade_time": group["first_trade_time"].min(),
            "last_trade_time": group["last_trade_time"].max(),
            "first_agg_trade_id": int(group["first_agg_trade_id"].min()),
            "last_agg_trade_id": int(group["last_agg_trade_id"].max()),
        }
        for c in sum_cols:
            row[c] = group[c].sum()
        s = row["taker_sell_volume"]
        b = row["taker_buy_volume"]
        row["taker_buy_sell_ratio"] = 999.0 if s == 0 and b > 0 else (0.0 if s == 0 else b / s)
        rows.append(row)
    return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)


# ── Task: 1-Minute Klines ────────────────────────────────────────────────────

def task_1m_klines(limiter):
    """Fetch 1m klines incrementally and maintain CSV."""
    existing = read_csv(FILE_1M, "open_time")
    if existing.empty:
        start_ms = utc_ms() - BACKFILL_1M_DAYS * 86400_000
    else:
        last = existing["open_time"].iloc[-1]
        start_ms = int(last.timestamp() * 1000) + 60000

    end_ms = (utc_ms() // 60000) * 60000 - 60000
    all_rows = []
    cursor = start_ms

    while cursor <= end_ms and not _shutdown.is_set():
        data = request_json("/api/v3/klines", {
            "symbol": SYMBOL, "interval": "1m",
            "startTime": cursor, "endTime": end_ms, "limit": 1000,
        }, "spot", 2, limiter)
        if not data:
            break
        all_rows.extend(data)
        next_cursor = int(data[-1][0]) + 60000
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        time.sleep(0.1)

    if not all_rows:
        return {"added": 0, "total": len(existing)}

    df = pd.DataFrame(all_rows, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "taker_buy_vol", "taker_buy_qv", "ignore",
    ])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[["open_time", "open", "high", "low", "close", "volume"]]

    # Merge
    cols = ["open_time", "open", "high", "low", "close", "volume"]
    merged = pd.concat([existing, df], ignore_index=True) if not existing.empty else df
    merged = merged.drop_duplicates(subset=["open_time"], keep="last").sort_values("open_time").reset_index(drop=True)
    atomic_csv_write(merged, FILE_1M, cols)
    added = max(0, len(merged) - len(existing))
    return {"added": added, "total": len(merged)}


# ── Task: Period Data (taker, lsratio, funding) ──────────────────────────────

def _fetch_period_range(endpoint, start_ms, end_ms, limiter):
    rows, cursor = [], int(start_ms)
    while cursor <= end_ms and not _shutdown.is_set():
        batch = request_json(endpoint, {
            "symbol": SYMBOL, "period": "5m",
            "startTime": cursor, "endTime": end_ms, "limit": 500,
        }, "futures", 5, limiter)
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1]["timestamp"]) + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        time.sleep(0.1)
    return rows


def task_taker(limiter):
    existing = read_csv(FILE_TAKER, "timestamp")
    end_ms = utc_ms()
    start_ms = int(existing["timestamp"].iloc[-1].timestamp() * 1000) + 300_000 if not existing.empty else end_ms - 7 * 86400_000
    rows = _fetch_period_range("/futures/data/takerlongshortRatio", start_ms, end_ms, limiter)
    if not rows:
        return {"added": 0, "total": len(existing)}
    df = pd.DataFrame(rows)[["timestamp", "buySellRatio", "buyVol", "sellVol"]].rename(
        columns={"timestamp": "timestamp"})
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    for c in ["buySellRatio", "buyVol", "sellVol"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    cols = ["timestamp", "buySellRatio", "buyVol", "sellVol"]
    merged = pd.concat([existing, df], ignore_index=True) if not existing.empty else df
    merged = merged.drop_duplicates(subset=["timestamp"], keep="last").sort_values("timestamp").reset_index(drop=True)
    atomic_csv_write(merged, FILE_TAKER, cols)
    return {"added": max(0, len(merged) - len(existing)), "total": len(merged)}


def task_lsratio(limiter):
    existing = read_csv(FILE_LSRATIO, "timestamp")
    end_ms = utc_ms()
    start_ms = int(existing["timestamp"].iloc[-1].timestamp() * 1000) + 300_000 if not existing.empty else end_ms - 7 * 86400_000
    rows = _fetch_period_range("/futures/data/topLongShortPositionRatio", start_ms, end_ms, limiter)
    if not rows:
        return {"added": 0, "total": len(existing)}
    df = pd.DataFrame(rows)[["timestamp", "longAccount", "shortAccount", "longShortRatio"]]
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    for c in ["longAccount", "shortAccount", "longShortRatio"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    cols = ["timestamp", "longAccount", "shortAccount", "longShortRatio"]
    merged = pd.concat([existing, df], ignore_index=True) if not existing.empty else df
    merged = merged.drop_duplicates(subset=["timestamp"], keep="last").sort_values("timestamp").reset_index(drop=True)
    atomic_csv_write(merged, FILE_LSRATIO, cols)
    return {"added": max(0, len(merged) - len(existing)), "total": len(merged)}


def task_funding(limiter):
    existing = read_csv(FILE_FUNDING, "fundingTime")
    end_ms = utc_ms()
    cursor = int(existing["fundingTime"].iloc[-1].timestamp() * 1000) + 1 if not existing.empty else end_ms - 30 * 86400_000
    all_rows = []
    while cursor <= end_ms and not _shutdown.is_set():
        batch = request_json("/fapi/v1/fundingRate", {
            "symbol": SYMBOL, "startTime": cursor, "endTime": end_ms, "limit": 1000,
        }, "futures", 1, limiter)
        if not batch:
            break
        all_rows.extend(batch)
        next_cursor = int(batch[-1]["fundingTime"]) + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        time.sleep(0.1)
    if not all_rows:
        return {"added": 0, "total": len(existing)}
    df = pd.DataFrame(all_rows)[["fundingTime", "fundingRate"]]
    df["fundingTime"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
    df["fundingRate"] = pd.to_numeric(df["fundingRate"], errors="coerce")
    cols = ["fundingTime", "fundingRate"]
    merged = pd.concat([existing, df], ignore_index=True) if not existing.empty else df
    merged = merged.drop_duplicates(subset=["fundingTime"], keep="last").sort_values("fundingTime").reset_index(drop=True)
    atomic_csv_write(merged, FILE_FUNDING, cols)
    return {"added": max(0, len(merged) - len(existing)), "total": len(merged)}


# ── Scheduler ─────────────────────────────────────────────────────────────────

class Scheduler:
    def __init__(self):
        self.tasks = {}       # name -> {func, interval, last_run, result, errors, error_count}
        self.status = {"ok": True, "started_at": iso_now(), "tasks": {}}

    def register(self, name, func, interval_sec):
        self.tasks[name] = {
            "func": func, "interval": interval_sec,
            "last_run": 0, "result": None, "errors": 0, "last_error": None,
        }

    def tick(self):
        now = time.time()
        for name, t in self.tasks.items():
            if now - t["last_run"] < t["interval"]:
                continue
            if _shutdown.is_set():
                break
            t["last_run"] = now
            try:
                t["result"] = t["func"]()
                t["last_error"] = None
            except Exception as e:
                t["errors"] += 1
                t["last_error"] = str(e)
                t["result"] = {"error": str(e)}
                log(name, f"Error: {e}")

    def build_status(self, limiter):
        now = time.time()
        tasks_status = {}
        for name, t in self.tasks.items():
            tasks_status[name] = {
                "interval": t["interval"],
                "last_run_ago": round(now - t["last_run"], 1) if t["last_run"] else None,
                "result": t["result"],
                "errors": t["errors"],
                "last_error": t["last_error"],
            }
        rate_usage = limiter.get_usage()
        self.status.update({
            "ok": True,
            "updated_at": iso_now(),
            "symbol": SYMBOL,
            "tasks": tasks_status,
            "rate_usage": rate_usage,
        })
        return self.status


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    lock_handle, lock_sock = acquire_lock()
    limiter = RateLimiter()
    trader = SecondTraderState()
    sched = Scheduler()

    # Register tasks with intervals
    sched.register("price",       lambda: task_price(limiter),                     INTERVAL_PRICE)
    sched.register("1s_trades",   lambda: task_1s_trades(trader, limiter),         INTERVAL_1S)
    sched.register("1m_klines",   lambda: task_1m_klines(limiter),                 INTERVAL_1M)
    sched.register("taker",       lambda: task_taker(limiter),                     INTERVAL_PERIOD)
    sched.register("lsratio",     lambda: task_lsratio(limiter),                   INTERVAL_PERIOD)
    sched.register("funding",     lambda: task_funding(limiter),                   INTERVAL_PERIOD)

    # Graceful shutdown
    def _handle_signal(signum, frame):
        log("Main", f"Signal {signum}, shutting down...")
        _shutdown.set()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log("Main", f"Unified collector started. Symbol={SYMBOL}")
    log("Main", f"Intervals: price={INTERVAL_PRICE}s, 1s={INTERVAL_1S}s, 1m={INTERVAL_1M}s, period={INTERVAL_PERIOD}s")
    log("Main", f"Outputs: {OUT}")

    try:
        while not _shutdown.is_set():
            sched.tick()
            # Write status every 5s
            if int(time.time()) % 5 == 0:
                atomic_json_write(STATUS_FILE, sched.build_status(limiter))
            time.sleep(0.2)
    finally:
        atomic_json_write(STATUS_FILE, sched.build_status(limiter))
        log("Main", "Stopped.")


if __name__ == "__main__":
    main()
