"""Real-time order-flow data recorder for BTCUSDT (Binance USDT-M perp).

Records four streams in parallel to daily-rolled Parquet files under
``user_data/data/flow/``:

1. WebSocket ``btcusdt@forceOrder``     -> liquidations.YYYY-MM-DD.parquet
2. WebSocket ``btcusdt@markPrice@1s``   -> markprice.YYYY-MM-DD.parquet
3. REST 1m ``/futures/data/openInterestHist?period=5m``  -> oi.YYYY-MM-DD.parquet
4. REST 1m ``/futures/data/topLongShortPositionRatio?period=5m``
   + ``globalLongShortAccountRatio`` + ``topLongShortAccountRatio``
   + ``takerlongshortRatio``           -> ratios.YYYY-MM-DD.parquet

Designed to run alongside ``live_signal_runner.py`` on the same VPS.
Reconnects on disconnect, flushes every BATCH bars.

Run:
    .venv\\Scripts\\python -u user_data/notebooks/flow_data_recorder.py
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import pandas as pd
import ssl
import websockets
from python_socks.async_.asyncio import Proxy
from urllib.parse import urlparse


# =============================== Config ===============================

SYMBOL = "BTCUSDT"
WS_BASE = "wss://fstream.binance.com/ws"
FAPI_BASE = "https://fapi.binance.com"

OUT_DIR = Path("user_data/data/flow")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# REST endpoints use HTTP proxy (works fine through Clash mixed port)
PROXY = os.environ.get("FLOW_PROXY", "http://127.0.0.1:7897")
# WebSocket streams: SOCKS5 / HTTP proxy URL. aiohttp drops WS frames
# through Clash, so we use python-socks + websockets library instead.
WS_PROXY = os.environ.get("FLOW_WS_PROXY", "socks5://127.0.0.1:7897")

REST_PERIOD = "5m"      # finest granularity Binance offers for these endpoints
REST_POLL_SEC = 60      # poll every minute, dedupe by timestamp
BATCH_FLUSH = 50        # write to disk every N records per stream


# ============================== Buffers ==============================

_buffers: dict[str, list[dict[str, Any]]] = {
    "liquidations": [],
    "markprice": [],
    "oi": [],
    "ratios": [],
}
_seen_keys: dict[str, set] = {k: set() for k in _buffers}
_stop = asyncio.Event()


def _today_path(name: str) -> Path:
    d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return OUT_DIR / f"{name}.{d}.parquet"


def _flush(name: str, force: bool = False) -> None:
    buf = _buffers[name]
    if not buf:
        return
    if not force and len(buf) < BATCH_FLUSH:
        return
    df_new = pd.DataFrame(buf)
    path = _today_path(name)
    if path.exists():
        df_old = pd.read_parquet(path)
        df = pd.concat([df_old, df_new], ignore_index=True)
        df = df.drop_duplicates(subset=list(df_new.columns)[:2])
    else:
        df = df_new
    df.to_parquet(path, index=False)
    _buffers[name].clear()
    print(f"[flush] {name:<12} +{len(df_new):<4} total={len(df)}  {path.name}", flush=True)


# ========================== WebSocket streams ==========================

async def _open_ws(url: str):
    """Open a wss connection, optionally tunneled through python-socks proxy."""
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or 443
    if WS_PROXY:
        proxy = Proxy.from_url(WS_PROXY)
        sock = await proxy.connect(dest_host=host, dest_port=port)
        ssl_ctx = ssl.create_default_context()
        return await websockets.connect(
            url, sock=sock, server_hostname=host, ssl=ssl_ctx,
            ping_interval=30, ping_timeout=20, max_size=2**22,
        )
    return await websockets.connect(url, ping_interval=30, ping_timeout=20)


async def ws_consumer(stream: str, name: str, handler) -> None:
    url = f"{WS_BASE}/{stream}"
    while not _stop.is_set():
        try:
            ws = await _open_ws(url)
        except Exception as e:
            print(f"[ws-err] {stream}: {e!r}; reconnect in 5s", flush=True)
            await asyncio.sleep(5)
            continue
        print(f"[ws] connected {stream}", flush=True)
        try:
            async for raw in ws:
                data = json.loads(raw)
                row = handler(data)
                if row is None:
                    continue
                _buffers[name].append(row)
                _flush(name)
        except Exception as e:
            print(f"[ws-err] {stream}: {e!r}; reconnect in 5s", flush=True)
            await asyncio.sleep(5)
        finally:
            try:
                await ws.close()
            except Exception:
                pass


def _parse_force(data: dict) -> dict | None:
    # https://binance-docs.github.io/apidocs/futures/en/#liquidation-order-streams
    o = data.get("o") or {}
    if not o:
        return None
    return {
        "ts": int(o["T"]),
        "side": o["S"],
        "price": float(o["p"]),
        "qty": float(o["q"]),
        "avg_price": float(o["ap"]),
        "filled": float(o["z"]),
        "status": o["X"],
    }


def _parse_mark(data: dict) -> dict | None:
    if data.get("e") != "markPriceUpdate":
        return None
    return {
        "ts": int(data["E"]),
        "mark": float(data["p"]),
        "index": float(data["i"]),
        "funding": float(data["r"]),
        "next_funding_ts": int(data["T"]),
    }


# =========================== REST polling ===========================

REST_ENDPOINTS = {
    "openInterestHist":              ("oi",     {}),
    "topLongShortPositionRatio":     ("ratios", {"prefix": "top_pos"}),
    "topLongShortAccountRatio":      ("ratios", {"prefix": "top_acc"}),
    "globalLongShortAccountRatio":   ("ratios", {"prefix": "global_acc"}),
    "takerlongshortRatio":           ("ratios", {"prefix": "taker"}),
}


async def rest_poller() -> None:
    async with aiohttp.ClientSession() as sess:
        while not _stop.is_set():
            for ep, (bucket, meta) in REST_ENDPOINTS.items():
                url = f"{FAPI_BASE}/futures/data/{ep}"
                params = {"symbol": SYMBOL, "period": REST_PERIOD, "limit": 5}
                try:
                    async with sess.get(url, params=params, proxy=PROXY, timeout=15) as r:
                        rows = await r.json()
                except Exception as e:
                    print(f"[rest-err] {ep}: {e!r}", flush=True)
                    continue
                if not isinstance(rows, list):
                    print(f"[rest-warn] {ep}: {rows}", flush=True)
                    continue
                seen = _seen_keys[bucket]
                prefix = meta.get("prefix")
                for raw in rows:
                    ts = int(raw.get("timestamp"))
                    key = (ep, ts)
                    if key in seen:
                        continue
                    seen.add(key)
                    row: dict[str, Any] = {"ts": ts, "endpoint": ep}
                    for k, v in raw.items():
                        if k in ("symbol", "timestamp"):
                            continue
                        col = f"{prefix}_{k}" if prefix else k
                        try:
                            row[col] = float(v)
                        except (TypeError, ValueError):
                            row[col] = v
                    _buffers[bucket].append(row)
                _flush(bucket)
            await asyncio.sleep(REST_POLL_SEC)


# ============================ Periodic flush ============================

async def periodic_flush() -> None:
    while not _stop.is_set():
        await asyncio.sleep(30)
        for k in _buffers:
            _flush(k, force=True)


# =============================== Main ===============================

async def main() -> None:
    print(f"flow_data_recorder started  OUT={OUT_DIR.resolve()}", flush=True)
    tasks = [
        asyncio.create_task(ws_consumer(f"{SYMBOL.lower()}@forceOrder", "liquidations", _parse_force)),
        asyncio.create_task(ws_consumer(f"{SYMBOL.lower()}@markPrice@1s", "markprice", _parse_mark)),
        asyncio.create_task(rest_poller()),
        asyncio.create_task(periodic_flush()),
    ]

    def _on_signal(*_: Any) -> None:
        print("[signal] stop requested", flush=True)
        _stop.set()
        for k in _buffers:
            _flush(k, force=True)

    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _on_signal)

    try:
        await _stop.wait()
    except KeyboardInterrupt:
        _on_signal()
    finally:
        for t in tasks:
            t.cancel()
        for k in _buffers:
            _flush(k, force=True)
        print("flow_data_recorder stopped", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        for k in _buffers:
            _flush(k, force=True)
