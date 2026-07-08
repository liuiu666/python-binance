"""Collect lightweight BTCUSDT futures order-book features.

This service runs beside the trade/second collector. It stores one feature row
per second from Binance futures partial-depth WebSocket data, so research can
test book imbalance, spread, and wall features without touching live signals.
"""
import atexit
import csv
import json
import os
import shutil
import socket
import sys
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urlparse

import websocket

try:
    import msvcrt
    fcntl = None
except ImportError:
    msvcrt = None
    import fcntl


APP_DIR = os.environ.get("APP_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("DATA_DIR", os.path.join(APP_DIR, "data"))
SYMBOL = os.environ.get("ORDERBOOK_SYMBOL", "BTCUSDT").upper()
LEVELS = max(5, int(os.environ.get("ORDERBOOK_LEVELS", "20")))
UPDATE_MS = int(os.environ.get("ORDERBOOK_UPDATE_MS", "1000"))
STATUS_INTERVAL_SEC = max(1.0, float(os.environ.get("ORDERBOOK_STATUS_INTERVAL_SEC", "2")))
SNAPSHOT_INTERVAL_SEC = max(0.2, float(os.environ.get("ORDERBOOK_SNAPSHOT_INTERVAL_SEC", "1")))
ARCHIVE_INTERVAL_SEC = max(60.0, float(os.environ.get("ORDERBOOK_ARCHIVE_INTERVAL_SEC", "3600")))
MAIN_FILE_KEEP_DAYS = max(1, int(os.environ.get("ORDERBOOK_MAIN_KEEP_DAYS", "1")))
LOCK_PORT = int(os.environ.get("ORDERBOOK_LOCK_PORT", "39873"))

OUT_FILE = os.path.join(OUT, f"{SYMBOL.lower()}_orderbook_1s.csv")
STATUS_FILE = os.path.join(OUT, "orderbook_status.json")
PREDICTION_STATUS_FILE = os.path.join(OUT, "orderbook_prediction_status.json")
PREDICTIONS_FILE = os.path.join(OUT, "orderbook_predictions.jsonl")
LOCK_FILE = os.path.join(OUT, "orderbook.lock")
LOCK_DIR = os.path.join(OUT, "orderbook.lockdir")
SHARD_DIR = os.path.join(OUT, "orderbook", SYMBOL, "futures")
PREDICTION_HORIZONS_SEC = tuple(
    int(x) for x in os.environ.get("ORDERBOOK_PREDICTION_HORIZONS_SEC", "10,30,60").split(",")
    if str(x).strip().isdigit()
)
PREDICTION_LOG_INTERVAL_SEC = max(1, int(os.environ.get("ORDERBOOK_PREDICTION_LOG_INTERVAL_SEC", "5")))
PREDICTION_ROLLING_WINDOW = max(30, int(os.environ.get("ORDERBOOK_PREDICTION_ROLLING_WINDOW", "300")))


def default_ws_url():
    levels = 20 if LEVELS <= 20 else 20
    speed = "100ms" if UPDATE_MS <= 100 else "500ms"
    return f"wss://fstream.binance.com/ws/{SYMBOL.lower()}@depth{levels}@{speed}"


WS_URL = os.environ.get("ORDERBOOK_WS_URL", default_ws_url()).strip()

CSV_COLUMNS = [
    "timestamp",
    "symbol",
    "market",
    "event_time",
    "first_update_id",
    "last_update_id",
    "bid",
    "ask",
    "mid",
    "spread_bps",
    "bid_qty_1",
    "ask_qty_1",
    "bid_qty_5",
    "ask_qty_5",
    "bid_qty_10",
    "ask_qty_10",
    "bid_qty_20",
    "ask_qty_20",
    "imbalance_1",
    "imbalance_5",
    "imbalance_10",
    "imbalance_20",
    "microprice_1",
    "microprice_edge_bps",
    "bid_wall_bps",
    "ask_wall_bps",
    "bid_wall_qty",
    "ask_wall_qty",
]


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
            print(f"[OrderBook] Another instance is active pid={old_pid}; exiting.", flush=True)
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
        print(f"[OrderBook] Another instance holds {LOCK_FILE}; exiting.", flush=True)
        sys.exit(0)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", LOCK_PORT))
        sock.listen(1)
        return handle, sock
    except OSError:
        print(f"[OrderBook] Another instance is already running on lock port {LOCK_PORT}; exiting.", flush=True)
        sys.exit(0)


def write_status(obj):
    os.makedirs(OUT, exist_ok=True)
    tmp = f"{STATUS_FILE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATUS_FILE)


def write_prediction_status(obj):
    os.makedirs(OUT, exist_ok=True)
    tmp = f"{PREDICTION_STATUS_FILE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PREDICTION_STATUS_FILE)


def append_prediction_result(obj):
    os.makedirs(os.path.dirname(PREDICTIONS_FILE), exist_ok=True)
    with open(PREDICTIONS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")


def append_csv(row):
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    exists = os.path.exists(OUT_FILE) and os.path.getsize(OUT_FILE) > 0
    with open(OUT_FILE, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in CSV_COLUMNS})


def sum_qty(levels, n):
    return sum(qty for _price, qty in levels[:n])


def imbalance(bid_qty, ask_qty):
    total = bid_qty + ask_qty
    if total <= 0:
        return 0.0
    return (bid_qty - ask_qty) / total


def wall(levels, best, side):
    if not levels or best <= 0:
        return "", ""
    max_price, max_qty = max(levels, key=lambda item: item[1])
    if side == "bid":
        dist_bps = (best - max_price) / best * 10000.0
    else:
        dist_bps = (max_price - best) / best * 10000.0
    return round(dist_bps, 4), round(max_qty, 8)


def clamp(value, low, high):
    try:
        num = float(value)
    except Exception:
        num = 0.0
    return max(low, min(high, num))


def timestamp_ms(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return int(time.time() * 1000)


def prediction_score(row):
    imb5 = clamp(row.get("imbalance_5"), -1.0, 1.0)
    imb20 = clamp(row.get("imbalance_20"), -1.0, 1.0)
    micro = clamp(float(row.get("microprice_edge_bps") or 0.0) / 0.08, -1.0, 1.0)
    bid_wall_qty = float(row.get("bid_wall_qty") or 0.0)
    ask_wall_qty = float(row.get("ask_wall_qty") or 0.0)
    bid_wall_bps = float(row.get("bid_wall_bps") or 999.0)
    ask_wall_bps = float(row.get("ask_wall_bps") or 999.0)
    wall_total = bid_wall_qty + ask_wall_qty
    wall_score = 0.0
    if wall_total > 0:
        raw_wall = (bid_wall_qty - ask_wall_qty) / wall_total
        if min(bid_wall_bps, ask_wall_bps) <= 8.0:
            wall_score = clamp(raw_wall, -1.0, 1.0)
    return clamp(0.42 * imb5 + 0.30 * imb20 + 0.22 * micro + 0.06 * wall_score, -1.0, 1.0)


def build_prediction(row):
    mid = float(row.get("mid") or 0.0)
    spread_bps = float(row.get("spread_bps") or 0.0)
    score = prediction_score(row)
    direction = "UP" if score >= 0.08 else "DOWN" if score <= -0.08 else "FLAT"
    spread_penalty = min(8.0, max(0.0, spread_bps - 0.5) * 2.0)
    confidence = 50.0 + min(18.0, abs(score) * 35.0) - spread_penalty
    if direction == "FLAT":
        confidence = min(confidence, 52.0)
    confidence = round(clamp(confidence, 45.0, 68.0), 2)
    horizon_scale = {10: 0.9, 30: 1.7, 60: 2.6}
    targets = []
    for horizon in PREDICTION_HORIZONS_SEC or (10, 30, 60):
        scale = horizon_scale.get(horizon, min(4.0, max(0.5, horizon / 25.0)))
        pred_bps = clamp(score * scale, -8.0, 8.0)
        target_price = mid * (1.0 + pred_bps / 10000.0) if mid > 0 else None
        targets.append({
            "horizonSec": horizon,
            "predictedBps": round(pred_bps, 4),
            "predictedPrice": round(target_price, 2) if target_price else None,
        })
    return {
        "timestamp": row.get("timestamp"),
        "symbol": SYMBOL,
        "mid": round(mid, 2) if mid > 0 else None,
        "direction": direction,
        "score": round(score, 5),
        "confidence": confidence,
        "features": {
            "spreadBps": row.get("spread_bps"),
            "imbalance5": row.get("imbalance_5"),
            "imbalance20": row.get("imbalance_20"),
            "micropriceEdgeBps": row.get("microprice_edge_bps"),
            "bidWallBps": row.get("bid_wall_bps"),
            "askWallBps": row.get("ask_wall_bps"),
        },
        "targets": targets,
        "note": "orderbook heuristic only; not wired to live trading",
    }


def validation_summary(outcomes_by_horizon):
    summary = {}
    for horizon, items in outcomes_by_horizon.items():
        rows = list(items)
        decided = [x for x in rows if x.get("direction") in ("UP", "DOWN")]
        hits = [x for x in decided if x.get("hit") is True]
        mae_items = [abs(float(x.get("errorBps") or 0.0)) for x in rows]
        summary[str(horizon)] = {
            "samples": len(rows),
            "decided": len(decided),
            "hits": len(hits),
            "hitRate": round(len(hits) / len(decided) * 100.0, 2) if decided else None,
            "maeBps": round(sum(mae_items) / len(mae_items), 4) if mae_items else None,
            "last": rows[-1] if rows else None,
        }
    return summary


def update_prediction_state(row, state):
    prediction = build_prediction(row)
    ts_ms = timestamp_ms(row.get("timestamp"))
    mid = float(row.get("mid") or 0.0)
    if mid <= 0:
        return prediction, {}

    log_this = int(ts_ms / 1000) % PREDICTION_LOG_INTERVAL_SEC == 0
    for target in prediction["targets"]:
        state["pending_predictions"].append({
            "timestamp": prediction["timestamp"],
            "dueMs": ts_ms + int(target["horizonSec"]) * 1000,
            "startMid": mid,
            "direction": prediction["direction"],
            "score": prediction["score"],
            "confidence": prediction["confidence"],
            "horizonSec": int(target["horizonSec"]),
            "predictedBps": float(target["predictedBps"]),
            "predictedPrice": target["predictedPrice"],
            "log": log_this,
        })

    remaining = deque()
    settled_count = 0
    while state["pending_predictions"]:
        item = state["pending_predictions"].popleft()
        if ts_ms < item["dueMs"]:
            remaining.append(item)
            continue
        actual_bps = (mid - item["startMid"]) / item["startMid"] * 10000.0
        direction = item["direction"]
        hit = None
        if direction == "UP":
            hit = actual_bps > 0
        elif direction == "DOWN":
            hit = actual_bps < 0
        outcome = {
            "timestamp": item["timestamp"],
            "settledAt": row.get("timestamp"),
            "horizonSec": item["horizonSec"],
            "direction": direction,
            "confidence": item["confidence"],
            "startMid": round(item["startMid"], 2),
            "actualMid": round(mid, 2),
            "predictedBps": round(item["predictedBps"], 4),
            "actualBps": round(actual_bps, 4),
            "errorBps": round(actual_bps - item["predictedBps"], 4),
            "hit": hit,
        }
        state["outcomes_by_horizon"][item["horizonSec"]].append(outcome)
        settled_count += 1
        if item.get("log"):
            append_prediction_result(outcome)
    state["pending_predictions"] = remaining
    return prediction, {
        "settledThisTick": settled_count,
        "validation": validation_summary(state["outcomes_by_horizon"]),
    }


def build_features(book):
    bids = [(float(price), float(qty)) for price, qty in book.get("b", []) if float(qty) > 0]
    asks = [(float(price), float(qty)) for price, qty in book.get("a", []) if float(qty) > 0]
    if not bids or not asks:
        return None
    bids.sort(key=lambda item: item[0], reverse=True)
    asks.sort(key=lambda item: item[0])
    bid = bids[0][0]
    ask = asks[0][0]
    mid = (bid + ask) / 2.0
    if bid <= 0 or ask <= 0 or mid <= 0:
        return None

    bid1 = sum_qty(bids, 1)
    ask1 = sum_qty(asks, 1)
    bid5 = sum_qty(bids, 5)
    ask5 = sum_qty(asks, 5)
    bid10 = sum_qty(bids, 10)
    ask10 = sum_qty(asks, 10)
    bid20 = sum_qty(bids, 20)
    ask20 = sum_qty(asks, 20)
    micro = (ask * bid1 + bid * ask1) / (bid1 + ask1) if bid1 + ask1 > 0 else mid
    bid_wall_bps, bid_wall_qty = wall(bids[:20], bid, "bid")
    ask_wall_bps, ask_wall_qty = wall(asks[:20], ask, "ask")
    event_ms = int(book.get("E") or book.get("T") or int(time.time() * 1000))
    ts = datetime.fromtimestamp(event_ms / 1000.0, tz=timezone.utc)
    return {
        "timestamp": ts.replace(microsecond=0).isoformat().replace("+00:00", ".000000Z"),
        "symbol": SYMBOL,
        "market": "futures",
        "event_time": ts.isoformat().replace("+00:00", "Z"),
        "first_update_id": book.get("U", ""),
        "last_update_id": book.get("u", ""),
        "bid": round(bid, 8),
        "ask": round(ask, 8),
        "mid": round(mid, 8),
        "spread_bps": round((ask - bid) / mid * 10000.0, 4),
        "bid_qty_1": round(bid1, 8),
        "ask_qty_1": round(ask1, 8),
        "bid_qty_5": round(bid5, 8),
        "ask_qty_5": round(ask5, 8),
        "bid_qty_10": round(bid10, 8),
        "ask_qty_10": round(ask10, 8),
        "bid_qty_20": round(bid20, 8),
        "ask_qty_20": round(ask20, 8),
        "imbalance_1": round(imbalance(bid1, ask1), 6),
        "imbalance_5": round(imbalance(bid5, ask5), 6),
        "imbalance_10": round(imbalance(bid10, ask10), 6),
        "imbalance_20": round(imbalance(bid20, ask20), 6),
        "microprice_1": round(micro, 8),
        "microprice_edge_bps": round((micro - mid) / mid * 10000.0, 4),
        "bid_wall_bps": bid_wall_bps,
        "ask_wall_bps": ask_wall_bps,
        "bid_wall_qty": bid_wall_qty,
        "ask_wall_qty": ask_wall_qty,
    }


def proxy_args():
    proxy = (
        os.environ.get("ORDERBOOK_WS_PROXY")
        or os.environ.get("SECOND_DATA_WS_PROXY")
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
    scheme = (parsed.scheme or "").lower()
    proxy_type = "socks5h" if scheme.startswith("socks5") else "http"
    return {
        "http_proxy_host": parsed.hostname,
        "http_proxy_port": parsed.port,
        "proxy_type": proxy_type,
    }


def archive_old_main_rows():
    if not os.path.exists(OUT_FILE):
        return 0
    try:
        if os.path.getsize(OUT_FILE) < 5 * 1024 * 1024:
            return 0
    except OSError:
        return 0
    cutoff_date = (datetime.now(timezone.utc).date().toordinal() - MAIN_FILE_KEEP_DAYS)
    os.makedirs(SHARD_DIR, exist_ok=True)
    tmp = OUT_FILE + ".archive_tmp"
    handles = {}
    archived = 0
    kept = 0

    def get_handle(day, header):
        if day in handles:
            return handles[day]
        path = os.path.join(SHARD_DIR, f"{day}.csv")
        exists = os.path.exists(path) and os.path.getsize(path) > 0
        handle = open(path, "a", encoding="utf-8", newline="")
        if not exists:
            handle.write(header)
        handles[day] = handle
        return handle

    try:
        with open(OUT_FILE, "r", encoding="utf-8", newline="") as src, open(tmp, "w", encoding="utf-8", newline="") as dst:
            header = src.readline()
            if not header:
                return 0
            dst.write(header)
            for line in src:
                raw = line.rstrip("\r\n")
                if not raw:
                    continue
                ts = raw.split(",", 1)[0]
                try:
                    day = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
                except Exception:
                    dst.write(line)
                    kept += 1
                    continue
                if day.toordinal() < cutoff_date:
                    get_handle(str(day), header).write(line)
                    archived += 1
                else:
                    dst.write(line)
                    kept += 1
    finally:
        for handle in handles.values():
            try:
                handle.close()
            except Exception:
                pass
    if archived:
        os.replace(tmp, OUT_FILE)
        print(f"\n[OrderBook] archived {archived} old rows to shards, main file kept {kept} rows", flush=True)
    else:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return archived


def run_loop():
    print(f"[OrderBook] Starting {SYMBOL} futures depth collector {WS_URL} -> {OUT_FILE}", flush=True)
    status = {
        "ok": False,
        "symbol": SYMBOL,
        "market": "futures",
        "mode": "websocket",
        "websocket_url": WS_URL,
        "file": OUT_FILE,
        "updated_at": iso_now(),
    }
    state = {
        "last_written_second": None,
        "rows": 0,
        "last_archive_check": 0.0,
        "pending_predictions": deque(),
        "outcomes_by_horizon": {h: deque(maxlen=PREDICTION_ROLLING_WINDOW) for h in (PREDICTION_HORIZONS_SEC or (10, 30, 60))},
    }

    def on_open(_ws):
        status.update({"ok": True, "updated_at": iso_now(), "error": None})
        write_status(status)
        print("[OrderBook] WebSocket connected", flush=True)

    def on_message(_ws, message):
        try:
            book = json.loads(message)
            row = build_features(book)
            if not row:
                return
            second = row["timestamp"]
            if second == state["last_written_second"]:
                return
            append_csv(row)
            prediction, validation = update_prediction_state(row, state)
            state["last_written_second"] = second
            state["rows"] += 1
            now = time.time()
            status.update({
                "ok": True,
                "updated_at": iso_now(),
                "error": None,
                "last_ts": row["timestamp"],
                "last_event_time": row["event_time"],
                "rows_written_this_run": state["rows"],
                "spread_bps": row["spread_bps"],
                "imbalance_5": row["imbalance_5"],
                "imbalance_20": row["imbalance_20"],
                "microprice_edge_bps": row["microprice_edge_bps"],
                "bid_wall_bps": row["bid_wall_bps"],
                "ask_wall_bps": row["ask_wall_bps"],
            })
            if now - float(status.get("_last_status_write") or 0) >= STATUS_INTERVAL_SEC:
                status["_last_status_write"] = now
                write_status({k: v for k, v in status.items() if not k.startswith("_")})
                write_prediction_status({
                    "ok": True,
                    "updated_at": iso_now(),
                    "last_ts": row["timestamp"],
                    "last_ts_ms": timestamp_ms(row["timestamp"]),
                    "symbol": SYMBOL,
                    "market": "futures",
                    "prediction": prediction,
                    "validation": validation.get("validation") or validation_summary(state["outcomes_by_horizon"]),
                    "pending": len(state["pending_predictions"]),
                    "logFile": PREDICTIONS_FILE,
                })
            if now - state["last_archive_check"] >= ARCHIVE_INTERVAL_SEC:
                try:
                    archive_old_main_rows()
                except Exception as archive_exc:
                    print(f"\n[OrderBook] archive warning: {archive_exc}", flush=True)
                state["last_archive_check"] = now
        except Exception as exc:
            status.update({"ok": False, "updated_at": iso_now(), "error": str(exc)})
            write_status({k: v for k, v in status.items() if not k.startswith("_")})
            print(f"\n[OrderBook] message error: {exc}", flush=True)

    def on_error(_ws, error):
        status.update({"ok": False, "updated_at": iso_now(), "error": str(error)})
        write_status({k: v for k, v in status.items() if not k.startswith("_")})
        print(f"\n[OrderBook] WebSocket error: {error}", flush=True)

    def on_close(_ws, code, reason):
        print(f"\n[OrderBook] WebSocket closed code={code} reason={reason}", flush=True)

    while True:
        ws = websocket.WebSocketApp(
            WS_URL,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        ws.run_forever(
            ping_interval=max(10, int(os.environ.get("ORDERBOOK_WS_PING_INTERVAL_SEC", "60"))),
            ping_timeout=max(5, int(os.environ.get("ORDERBOOK_WS_PING_TIMEOUT_SEC", "20"))),
            **proxy_args(),
        )
        time.sleep(5)


def main():
    acquire_singleton_lock()
    run_loop()


if __name__ == "__main__":
    main()
