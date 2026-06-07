"""Fetch one BTCUSDT order book snapshot and append derived features.

This is the first-stage order-book data pipeline. It records current Binance
spot depth/bookTicker data to JSONL so future research can test whether spread
and imbalance features improve signal quality. It does not affect trading.
"""
import json
import os
import time

import requests

OUT = "E:/codex/data"
SNAPSHOT_FILE = os.path.join(OUT, "orderbook_snapshots.jsonl")
BASE_URLS = [
    "https://api.binance.com",
    "https://data-api.binance.vision",
]


def get_json(path, params):
    last_err = None
    for base in BASE_URLS:
        try:
            r = requests.get(f"{base}{path}", params=params, timeout=10)
            r.raise_for_status()
            return r.json(), base
        except Exception as e:
            last_err = e
    raise last_err


def levels_notional(levels, mid, count):
    total_qty = 0.0
    total_notional = 0.0
    for price, qty in levels[:count]:
        price = float(price)
        qty = float(qty)
        total_qty += qty
        total_notional += price * qty
    return total_qty, total_notional / mid if mid else 0.0


def weighted_price(levels, count):
    qty_sum = 0.0
    px_qty = 0.0
    for price, qty in levels[:count]:
        price = float(price)
        qty = float(qty)
        qty_sum += qty
        px_qty += price * qty
    return px_qty / qty_sum if qty_sum else None


def imbalance(bids, asks, count):
    bid_qty = sum(float(q) for _, q in bids[:count])
    ask_qty = sum(float(q) for _, q in asks[:count])
    total = bid_qty + ask_qty
    return (bid_qty - ask_qty) / total if total else 0.0


def build_features(depth, book):
    bids = depth.get("bids") or []
    asks = depth.get("asks") or []
    if not bids or not asks:
        raise RuntimeError("depth response has no bid/ask levels")
    best_bid = float(book.get("bidPrice") or bids[0][0])
    best_ask = float(book.get("askPrice") or asks[0][0])
    bid_qty_top1 = float(book.get("bidQty") or bids[0][1])
    ask_qty_top1 = float(book.get("askQty") or asks[0][1])
    mid = (best_bid + best_ask) / 2
    spread = best_ask - best_bid
    bid_qty_5, bid_notional_5 = levels_notional(bids, mid, 5)
    ask_qty_5, ask_notional_5 = levels_notional(asks, mid, 5)
    bid_qty_20, bid_notional_20 = levels_notional(bids, mid, 20)
    ask_qty_20, ask_notional_20 = levels_notional(asks, mid, 20)
    weighted_bid_5 = weighted_price(bids, 5)
    weighted_ask_5 = weighted_price(asks, 5)
    microprice_top1 = (
        (best_ask * bid_qty_top1 + best_bid * ask_qty_top1) / (bid_qty_top1 + ask_qty_top1)
        if bid_qty_top1 + ask_qty_top1
        else mid
    )
    return {
        "symbol": "BTCUSDT",
        "time": int(time.time() * 1000),
        "lastUpdateId": depth.get("lastUpdateId"),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread": spread,
        "spread_bps": spread / mid * 10000 if mid else None,
        "bid_qty_top1": bid_qty_top1,
        "ask_qty_top1": ask_qty_top1,
        "imbalance_1": imbalance(bids, asks, 1),
        "imbalance_5": imbalance(bids, asks, 5),
        "imbalance_20": imbalance(bids, asks, 20),
        "bid_qty_5": bid_qty_5,
        "ask_qty_5": ask_qty_5,
        "bid_notional_mid_5": bid_notional_5,
        "ask_notional_mid_5": ask_notional_5,
        "bid_qty_20": bid_qty_20,
        "ask_qty_20": ask_qty_20,
        "bid_notional_mid_20": bid_notional_20,
        "ask_notional_mid_20": ask_notional_20,
        "weighted_bid_5": weighted_bid_5,
        "weighted_ask_5": weighted_ask_5,
        "microprice_top1": microprice_top1,
        "microprice_deviation_bps": (microprice_top1 - mid) / mid * 10000 if mid else None,
    }


def append_jsonl(path, row):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main():
    depth, depth_base = get_json("/api/v3/depth", {"symbol": "BTCUSDT", "limit": 100})
    book, book_base = get_json("/api/v3/ticker/bookTicker", {"symbol": "BTCUSDT"})
    row = build_features(depth, book)
    row["source"] = {"depth": depth_base, "bookTicker": book_base}
    append_jsonl(SNAPSHOT_FILE, row)
    print(json.dumps(row, indent=2, ensure_ascii=False))
    print(f"Saved {SNAPSHOT_FILE}")


if __name__ == "__main__":
    main()
