"""Build compact causal auction features with constant memory usage."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
from collections import deque
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = (10, 30, 60)


def number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def iso_second(second: int) -> str:
    return datetime.fromtimestamp(second, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def iter_events(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and number(row.get("event_time_ms")) > 0:
                    yield row
    except EOFError:
        return


def second_groups(path: Path) -> Iterator[tuple[int, list[dict[str, Any]]]]:
    for second, rows in groupby(iter_events(path), key=lambda row: int(number(row["event_time_ms"])) // 1000):
        yield second, list(rows)


def summed(rows: list[dict[str, Any]], key: str) -> float:
    return sum(number(row.get(key)) for row in rows)


def window_sum(history: deque[dict[str, Any]], key: str, width: int) -> float:
    rows = list(history)[-width:]
    return sum(number(row.get(key)) for row in rows)


def aggregate_second(
    second: int,
    trades: list[dict[str, Any]],
    depths: list[dict[str, Any]],
    last_book: dict[str, float],
    last_depth_ms: int | None,
) -> tuple[dict[str, Any], dict[str, float], int | None]:
    buy_qty = sell_qty = buy_notional = sell_notional = 0.0
    for trade in trades:
        quantity = number(trade.get("quantity"))
        notional = number(trade.get("price")) * quantity
        if trade.get("aggressor") == "BUY":
            buy_qty += quantity
            buy_notional += notional
        else:
            sell_qty += quantity
            sell_notional += notional

    near_fields = (
        "bid_added_near_qty",
        "bid_removed_near_qty",
        "ask_added_near_qty",
        "ask_removed_near_qty",
    )
    near_available = bool(depths) and all(all(field in row for field in near_fields) for row in depths)
    if depths:
        latest = max(depths, key=lambda row: number(row.get("event_time_ms")))
        last_depth_ms = int(number(latest.get("event_time_ms")))
        last_book = {
            "mid": number(latest.get("mid")),
            "spread_bps": number(latest.get("spread_bps")),
            "bid_depth_n": number(latest.get("bid_depth_n")),
            "ask_depth_n": number(latest.get("ask_depth_n")),
            "book_imbalance": number(latest.get("imbalance_n")),
        }
    bid_added = summed(depths, "bid_added_qty")
    bid_removed = summed(depths, "bid_removed_qty")
    ask_added = summed(depths, "ask_added_qty")
    ask_removed = summed(depths, "ask_removed_qty")
    near_bid = summed(depths, "bid_added_near_qty") - summed(depths, "bid_removed_near_qty")
    near_ask = summed(depths, "ask_added_near_qty") - summed(depths, "ask_removed_near_qty")
    depth_age_ms = (second + 1) * 1000 - 1 - last_depth_ms if last_depth_ms is not None else None
    return ({
        "timestamp": iso_second(second),
        "timestamp_ms": second * 1000,
        "trade_count": len(trades),
        "buy_qty": buy_qty,
        "sell_qty": sell_qty,
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "signed_flow_notional": buy_notional - sell_notional,
        "depth_update_count": len(depths),
        "net_bid_liquidity": bid_added - bid_removed,
        "net_ask_liquidity": ask_added - ask_removed,
        "liquidity_pressure": (bid_added - bid_removed) - (ask_added - ask_removed),
        "near_liquidity_available": int(near_available),
        "near_net_bid_liquidity": near_bid,
        "near_net_ask_liquidity": near_ask,
        "near_liquidity_pressure": near_bid - near_ask,
        "depth_age_ms": depth_age_ms,
        "depth_available": int(depth_age_ms is not None and 0 <= depth_age_ms <= 1500),
        **last_book,
    }, last_book, last_depth_ms)


def add_windows(row: dict[str, Any], history: deque[dict[str, Any]]) -> None:
    for width in WINDOWS:
        sample = list(history)[-width:]
        buy = sum(number(item.get("buy_notional")) for item in sample)
        sell = sum(number(item.get("sell_notional")) for item in sample)
        total = buy + sell
        row[f"flow_imbalance_{width}s"] = (buy - sell) / total if total > 0.0 else 0.0
        row[f"net_bid_liquidity_{width}s"] = window_sum(history, "net_bid_liquidity", width)
        row[f"net_ask_liquidity_{width}s"] = window_sum(history, "net_ask_liquidity", width)
        row[f"liquidity_pressure_{width}s"] = window_sum(history, "liquidity_pressure", width)
        near_bid = window_sum(history, "near_net_bid_liquidity", width)
        near_ask = window_sum(history, "near_net_ask_liquidity", width)
        bid_depth = number(row.get("bid_depth_n"))
        ask_depth = number(row.get("ask_depth_n"))
        row[f"near_liquidity_pressure_{width}s"] = near_bid - near_ask
        row[f"near_liquidity_pressure_ratio_{width}s"] = (
            near_bid / bid_depth - near_ask / ask_depth
            if bid_depth > 0.0 and ask_depth > 0.0 and any(number(item.get("near_liquidity_available")) for item in sample)
            else None
        )
        row[f"near_liquidity_coverage_{width}s"] = (
            sum(number(item.get("near_liquidity_available")) for item in sample) / len(sample) if sample else 0.0
        )
        row[f"depth_coverage_{width}s"] = (
            sum(number(item.get("depth_available")) for item in sample) / len(sample) if sample else 0.0
        )
        prior_mid = number(sample[0].get("mid")) if sample else 0.0
        current_mid = number(row.get("mid"))
        row[f"ret_{width}s_bps"] = (
            (current_mid / prior_mid - 1.0) * 10000.0 if prior_mid > 0.0 and current_mid > 0.0 else None
        )


def build_day(input_root: Path, output_root: Path, day: str) -> dict[str, Any]:
    trade_path = input_root / "trades" / f"date={day}" / "events.jsonl.gz"
    depth_path = input_root / "depth_updates" / f"date={day}" / "events.jsonl.gz"
    trade_groups = iter(second_groups(trade_path))
    depth_groups = iter(second_groups(depth_path))
    next_trade = next(trade_groups, None)
    next_depth = next(depth_groups, None)
    available = [item[0] for item in (next_trade, next_depth) if item is not None]
    if not available:
        raise RuntimeError(f"no auction events for {day}")
    second = min(available)
    history: deque[dict[str, Any]] = deque(maxlen=max(WINDOWS))
    last_book: dict[str, float] = {}
    last_depth_ms: int | None = None
    output = output_root / f"date={day}" / "features.csv.gz"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".gz.tmp")
    writer: csv.DictWriter | None = None
    count = 0
    with gzip.open(temporary, "wt", encoding="utf-8", newline="") as handle:
        while True:
            trades = next_trade[1] if next_trade is not None and next_trade[0] == second else []
            depths = next_depth[1] if next_depth is not None and next_depth[0] == second else []
            if trades:
                next_trade = next(trade_groups, None)
            if depths:
                next_depth = next(depth_groups, None)
            row, last_book, last_depth_ms = aggregate_second(
                second, trades, depths, last_book, last_depth_ms
            )
            history.append(row)
            add_windows(row, history)
            if writer is None:
                writer = csv.DictWriter(handle, fieldnames=list(row))
                writer.writeheader()
            writer.writerow(row)
            count += 1
            if next_trade is None and next_depth is None:
                break
            second += 1
    temporary.replace(output)
    return {"day": day, "rows": count, "output": str(output), "bytes": output.stat().st_size}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--input-root", type=Path, default=ROOT / "tmp" / "auction_raw")
    parser.add_argument("--output-root", type=Path, default=ROOT / "tmp" / "auction_features")
    args = parser.parse_args()
    print(json.dumps(build_day(args.input_root, args.output_root, args.date), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
