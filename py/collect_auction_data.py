"""Capture raw futures auction events for later causal strategy research.

The existing collectors remain the production source for 1-second bars and
order-book snapshots. This sidecar records the event sequence required to
study auction acceptance, replenishment, cancellation, and liquidation flow.
"""

from __future__ import annotations

import atexit
import gzip
import json
import os
import shutil
import socket
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import websocket


APP_DIR = Path(os.environ.get("APP_DIR") or Path(__file__).resolve().parents[1])
DATA_DIR = Path(os.environ.get("DATA_DIR") or APP_DIR / "data")
SYMBOL = os.environ.get("AUCTION_SYMBOL", "BTCUSDT").upper()
RAW_COMPRESSION = os.environ.get("AUCTION_RAW_COMPRESSION", "gzip").strip().lower()
DEPTH_LIMIT = max(20, min(1000, int(os.environ.get("AUCTION_DEPTH_LIMIT", "100"))))
DEPTH_VIEW_LEVELS = max(5, min(DEPTH_LIMIT, int(os.environ.get("AUCTION_VIEW_LEVELS", "20"))))
NEAR_TOUCH_BPS = max(0.5, min(50.0, float(os.environ.get("AUCTION_NEAR_TOUCH_BPS", "5"))))
STATUS_INTERVAL_SEC = max(1.0, float(os.environ.get("AUCTION_STATUS_INTERVAL_SEC", "2")))
PING_INTERVAL_SEC = max(10, int(os.environ.get("AUCTION_WS_PING_INTERVAL_SEC", "60")))
PING_TIMEOUT_SEC = max(5, int(os.environ.get("AUCTION_WS_PING_TIMEOUT_SEC", "20")))
HTTP_TIMEOUT_SEC = max(2.0, float(os.environ.get("AUCTION_HTTP_TIMEOUT_SEC", "8")))
LOCK_PORT = int(os.environ.get("AUCTION_LOCK_PORT", "39874"))

AUCTION_ROOT = DATA_DIR / "auction" / SYMBOL / "futures"
STATUS_FILE = DATA_DIR / "auction_data_status.json"
LOCK_FILE = DATA_DIR / "auction_data.lock"
LOCK_DIR = DATA_DIR / "auction_data.lockdir"
WS_URL = os.environ.get(
    "AUCTION_WS_URL",
    "wss://fstream.binance.com/stream?streams="
    "btcusdt@trade/btcusdt@depth@100ms/btcusdt@forceOrder",
).strip()
REST_DEPTH_URL = os.environ.get("AUCTION_REST_DEPTH_URL", "https://fapi.binance.com/fapi/v1/depth")


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_from_ms(value: int | float | None) -> str:
    timestamp = int(value or now_ms())
    return datetime.fromtimestamp(timestamp / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def day_key(value: int | float | None) -> str:
    timestamp = int(value or now_ms())
    return datetime.fromtimestamp(timestamp / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


def proxy_args() -> dict[str, Any]:
    proxy = (
        os.environ.get("AUCTION_WS_PROXY")
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
    if not parsed.hostname or not parsed.port:
        return {}
    return {
        "http_proxy_host": parsed.hostname,
        "http_proxy_port": parsed.port,
        "proxy_type": "socks5h" if (parsed.scheme or "").lower().startswith("socks5") else "http",
    }


def acquire_singleton_lock() -> tuple[Any, socket.socket]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        LOCK_DIR.mkdir()
        (LOCK_DIR / "pid").write_text(str(os.getpid()), encoding="utf-8")
        atexit.register(lambda: shutil.rmtree(LOCK_DIR, ignore_errors=True))
    except FileExistsError:
        pid_file = LOCK_DIR / "pid"
        try:
            old_pid = int(pid_file.read_text(encoding="utf-8").strip())
            os.kill(old_pid, 0)
        except Exception:
            shutil.rmtree(LOCK_DIR, ignore_errors=True)
            LOCK_DIR.mkdir()
            pid_file.write_text(str(os.getpid()), encoding="utf-8")
            atexit.register(lambda: shutil.rmtree(LOCK_DIR, ignore_errors=True))
        else:
            print(f"[Auction] Another collector is active pid={old_pid}; exiting.", flush=True)
            sys.exit(0)

    handle = open(LOCK_FILE, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[Auction] Another collector holds the file lock; exiting.", flush=True)
        sys.exit(0)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", LOCK_PORT))
        listener.listen(1)
    except OSError:
        print("[Auction] Another collector owns the lock port; exiting.", flush=True)
        sys.exit(0)
    return handle, listener


class JsonlPartitionWriter:
    def __init__(self, root: Path):
        self.root = root
        self.handles: dict[tuple[str, str], Any] = {}
        self.lock = threading.Lock()

    def append(self, stream: str, event_time_ms: int, payload: dict[str, Any]) -> Path:
        day = day_key(event_time_ms)
        key = (stream, day)
        with self.lock:
            handle = self.handles.get(key)
            if handle is None:
                filename = "events.jsonl.gz" if RAW_COMPRESSION == "gzip" else "events.jsonl"
                path = self.root / stream / f"date={day}" / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                if RAW_COMPRESSION == "gzip":
                    handle = gzip.open(path, "at", encoding="utf-8", compresslevel=3)
                else:
                    handle = open(path, "a", encoding="utf-8", buffering=1024 * 1024)
                self.handles[key] = handle
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            return Path(handle.name)

    def flush(self) -> None:
        with self.lock:
            # Closing each active gzip writer creates a complete gzip member.
            # A live day can then be read safely while the next second starts
            # a new member; an abrupt stop can affect at most that last second.
            for handle in self.handles.values():
                handle.close()
            self.handles.clear()

    def close(self) -> None:
        with self.lock:
            for handle in self.handles.values():
                try:
                    handle.close()
                except Exception:
                    pass
            self.handles.clear()


class LocalOrderBook:
    def __init__(self, view_levels: int):
        self.view_levels = view_levels
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.last_update_id: int | None = None
        self.needs_bridge = True

    def reset(self, snapshot: dict[str, Any]) -> None:
        self.bids = {float(price): float(qty) for price, qty in snapshot.get("bids", []) if float(qty) > 0.0}
        self.asks = {float(price): float(qty) for price, qty in snapshot.get("asks", []) if float(qty) > 0.0}
        self.last_update_id = int(snapshot["lastUpdateId"])
        # The REST snapshot and WebSocket have separate delivery paths. The
        # first accepted diff only needs to cover snapshot_id + 1; every diff
        # after that must chain exactly to the prior WebSocket diff.
        self.needs_bridge = True

    @staticmethod
    def _apply_side(
        levels: dict[float, float],
        updates: list[list[str]],
    ) -> tuple[float, float, int, list[tuple[float, float, float]]]:
        added = removed = 0.0
        changed = 0
        deltas: list[tuple[float, float, float]] = []
        for raw_price, raw_qty in updates:
            price = float(raw_price)
            quantity = float(raw_qty)
            previous = levels.get(price, 0.0)
            if quantity <= 0.0:
                if previous > 0.0:
                    removed += previous
                    levels.pop(price, None)
                    changed += 1
                    deltas.append((price, 0.0, previous))
                continue
            added_delta = removed_delta = 0.0
            if quantity > previous:
                added_delta = quantity - previous
                added += added_delta
            elif previous > quantity:
                removed_delta = previous - quantity
                removed += removed_delta
            if quantity != previous:
                changed += 1
                deltas.append((price, added_delta, removed_delta))
            levels[price] = quantity
        return added, removed, changed, deltas

    def apply(self, event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        first = int(event.get("U") or event.get("u") or 0)
        final = int(event.get("u") or 0)
        previous = event.get("pu")
        if self.last_update_id is None:
            return "uninitialized", {}
        if final <= self.last_update_id:
            return "stale", {}
        if self.needs_bridge and not (first <= self.last_update_id + 1 <= final):
            return "gap", {"expected": self.last_update_id + 1, "first": first, "final": final}
        if not self.needs_bridge and previous is not None and int(previous) != self.last_update_id:
            return "gap", {"expected": self.last_update_id, "previous": int(previous)}
        if not self.needs_bridge and previous is None and not (first <= self.last_update_id + 1 <= final):
            return "gap", {"expected": self.last_update_id + 1, "first": first, "final": final}

        bid_added, bid_removed, bid_changes, bid_deltas = self._apply_side(self.bids, event.get("b", []))
        ask_added, ask_removed, ask_changes, ask_deltas = self._apply_side(self.asks, event.get("a", []))
        summary = self.summary()
        best_bid = float(summary.get("best_bid") or 0.0)
        best_ask = float(summary.get("best_ask") or 0.0)
        bid_floor = best_bid * (1.0 - NEAR_TOUCH_BPS / 10000.0)
        ask_ceiling = best_ask * (1.0 + NEAR_TOUCH_BPS / 10000.0)
        bid_added_near = sum(added for price, added, _removed in bid_deltas if price >= bid_floor)
        bid_removed_near = sum(removed for price, _added, removed in bid_deltas if price >= bid_floor)
        ask_added_near = sum(added for price, added, _removed in ask_deltas if price <= ask_ceiling)
        ask_removed_near = sum(removed for price, _added, removed in ask_deltas if price <= ask_ceiling)
        self.last_update_id = final
        bridged = self.needs_bridge
        self.needs_bridge = False
        return "applied", {
            "snapshot_bridge": bridged,
            "bid_added_qty": bid_added,
            "bid_removed_qty": bid_removed,
            "ask_added_qty": ask_added,
            "ask_removed_qty": ask_removed,
            "near_touch_bps": NEAR_TOUCH_BPS,
            "bid_added_near_qty": bid_added_near,
            "bid_removed_near_qty": bid_removed_near,
            "ask_added_near_qty": ask_added_near,
            "ask_removed_near_qty": ask_removed_near,
            "bid_change_count": bid_changes,
            "ask_change_count": ask_changes,
        }

    def summary(self) -> dict[str, Any]:
        bids = sorted(self.bids.items(), key=lambda item: item[0], reverse=True)[: self.view_levels]
        asks = sorted(self.asks.items(), key=lambda item: item[0])[: self.view_levels]
        if not bids or not asks:
            return {}
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid = (best_bid + best_ask) / 2.0
        bid_depth = sum(quantity for _price, quantity in bids)
        ask_depth = sum(quantity for _price, quantity in asks)
        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "spread_bps": (best_ask - best_bid) / mid * 10000.0 if mid > 0.0 else None,
            "bid_depth_n": bid_depth,
            "ask_depth_n": ask_depth,
            "imbalance_n": (bid_depth - ask_depth) / (bid_depth + ask_depth) if bid_depth + ask_depth > 0.0 else 0.0,
        }


class AuctionCollector:
    def __init__(self):
        self.writer = JsonlPartitionWriter(AUCTION_ROOT)
        self.book = LocalOrderBook(DEPTH_VIEW_LEVELS)
        self.status: dict[str, Any] = {
            "ok": False,
            "symbol": SYMBOL,
            "market": "futures",
            "mode": "websocket_depth_delta",
            "websocket_url": WS_URL,
            "root": str(AUCTION_ROOT),
            "updated_at": iso_from_ms(now_ms()),
            "trades": 0,
            "depth_updates": 0,
            "force_orders": 0,
            "book_resyncs": 0,
            "sequence_gaps": 0,
            "stale_depth_updates": 0,
            "reconnects": 0,
        }
        self.last_status_write = 0.0
        self.last_flush = 0.0
        self.last_event_ms: dict[str, int] = {}

    def write_status(self, *, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last_status_write < STATUS_INTERVAL_SEC:
            return
        self.last_status_write = now
        latest = max(self.last_event_ms.values(), default=0)
        payload = {
            **self.status,
            "updated_at": iso_from_ms(now_ms()),
            "last_event_time": iso_from_ms(latest) if latest else None,
            "event_age_ms": max(0, now_ms() - latest) if latest else None,
            "last_update_id": self.book.last_update_id,
            "book_synced": self.book.last_update_id is not None and not self.book.needs_bridge,
            "book": self.book.summary(),
            "streams": {
                name: {"time": iso_from_ms(value), "age_ms": max(0, now_ms() - value)}
                for name, value in self.last_event_ms.items()
            },
        }
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temporary = STATUS_FILE.with_suffix(f".json.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, STATUS_FILE)

    def fetch_snapshot(self) -> None:
        response = requests.get(
            REST_DEPTH_URL,
            params={"symbol": SYMBOL, "limit": DEPTH_LIMIT},
            timeout=HTTP_TIMEOUT_SEC,
        )
        response.raise_for_status()
        snapshot = response.json()
        self.book.reset(snapshot)
        self.status["book_resyncs"] += 1
        self.status["last_snapshot_time"] = iso_from_ms(now_ms())

    def ensure_book(self) -> bool:
        if self.book.last_update_id is not None:
            return True
        try:
            self.fetch_snapshot()
            return True
        except Exception as exc:
            self.status.update({"ok": False, "error": f"snapshot_failed: {exc}"})
            self.write_status(force=True)
            return False

    def _append(self, stream: str, event_time_ms: int, payload: dict[str, Any]) -> None:
        self.writer.append(stream, event_time_ms, payload)
        self.last_event_ms[stream] = event_time_ms
        if time.time() - self.last_flush >= 1.0:
            self.writer.flush()
            self.last_flush = time.time()

    def handle_trade(self, event: dict[str, Any]) -> None:
        event_time = int(event.get("T") or event.get("E") or now_ms())
        buyer_is_maker = bool(event.get("m"))
        self._append(
            "trades",
            event_time,
            {
                "event_time": iso_from_ms(event_time),
                "event_time_ms": event_time,
                "symbol": event.get("s", SYMBOL),
                "trade_id": event.get("t"),
                "price": event.get("p"),
                "quantity": event.get("q"),
                "buyer_is_maker": buyer_is_maker,
                "aggressor": "SELL" if buyer_is_maker else "BUY",
            },
        )
        self.status["trades"] += 1

    def handle_force_order(self, event: dict[str, Any]) -> None:
        order = event.get("o") or {}
        event_time = int(order.get("T") or event.get("E") or now_ms())
        self._append(
            "force_orders",
            event_time,
            {
                "event_time": iso_from_ms(event_time),
                "event_time_ms": event_time,
                "symbol": order.get("s", SYMBOL),
                "side": order.get("S"),
                "order_type": order.get("o"),
                "time_in_force": order.get("f"),
                "status": order.get("X"),
                "price": order.get("p"),
                "average_price": order.get("ap"),
                "original_quantity": order.get("q"),
                "filled_quantity": order.get("z"),
                "last_filled_quantity": order.get("l"),
            },
        )
        self.status["force_orders"] += 1

    def handle_depth(self, event: dict[str, Any]) -> None:
        event_time = int(event.get("E") or event.get("T") or now_ms())
        if not self.ensure_book():
            return
        outcome, delta = self.book.apply(event)
        if outcome == "gap":
            self.status["sequence_gaps"] += 1
            try:
                self.fetch_snapshot()
            except Exception as exc:
                self.status.update({"ok": False, "error": f"resync_failed: {exc}"})
                return
            outcome, delta = self.book.apply(event)
        if outcome == "stale":
            self.status["stale_depth_updates"] += 1
            return
        if outcome != "applied":
            self.status["sequence_gaps"] += 1
            return
        self._append(
            "depth_updates",
            event_time,
            {
                "event_time": iso_from_ms(event_time),
                "event_time_ms": event_time,
                "symbol": event.get("s", SYMBOL),
                "first_update_id": event.get("U"),
                "last_update_id": event.get("u"),
                "previous_update_id": event.get("pu"),
                "bid_updates": event.get("b", []),
                "ask_updates": event.get("a", []),
                **delta,
                **self.book.summary(),
            },
        )
        self.status["depth_updates"] += 1

    def handle_message(self, message: str) -> None:
        raw = json.loads(message)
        event = raw.get("data") if isinstance(raw, dict) and isinstance(raw.get("data"), dict) else raw
        if not isinstance(event, dict):
            return
        event_type = str(event.get("e") or "")
        if event_type == "trade":
            self.handle_trade(event)
        elif event_type == "depthUpdate":
            self.handle_depth(event)
        elif event_type == "forceOrder":
            self.handle_force_order(event)
        self.status.update({"ok": True, "error": None})
        self.write_status()

    def run(self) -> None:
        print(f"[Auction] Starting {SYMBOL} auction collector {WS_URL}", flush=True)
        while True:
            def on_open(_ws):
                self.status.update({"ok": True, "error": None, "reconnects": self.status["reconnects"] + 1})
                self.write_status(force=True)
                print("[Auction] WebSocket connected", flush=True)

            def on_message(_ws, message):
                try:
                    self.handle_message(message)
                except Exception as exc:
                    self.status.update({"ok": False, "error": f"message_failed: {exc}"})
                    self.write_status(force=True)
                    print(f"[Auction] message error: {exc}", flush=True)

            def on_error(_ws, error):
                self.status.update({"ok": False, "error": str(error)})
                self.write_status(force=True)
                print(f"[Auction] websocket error: {error}", flush=True)

            ws = websocket.WebSocketApp(WS_URL, on_open=on_open, on_message=on_message, on_error=on_error)
            ws.run_forever(ping_interval=PING_INTERVAL_SEC, ping_timeout=PING_TIMEOUT_SEC, **proxy_args())
            self.writer.flush()
            time.sleep(3)


def main() -> None:
    acquire_singleton_lock()
    collector = AuctionCollector()
    atexit.register(collector.writer.close)
    collector.run()


if __name__ == "__main__":
    main()
