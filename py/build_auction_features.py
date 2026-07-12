"""Build causal one-second auction features from raw futures event partitions.

The collector stores the unmodified event order. This tool converts a selected
UTC day into compact one-second rows for research. It deliberately creates no
signal or label: every value is known by the end of that second only.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


APP_DIR = Path(os.environ.get("APP_DIR") or Path(__file__).resolve().parents[1])
DATA_DIR = Path(os.environ.get("DATA_DIR") or APP_DIR / "data")
SYMBOL = os.environ.get("AUCTION_SYMBOL", "BTCUSDT").upper()
WINDOWS_SEC = (10, 30, 60)


def number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def iso_second(second: int) -> str:
    return datetime.fromtimestamp(second, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], 0
    rows: list[dict[str, Any]] = []
    invalid = 0
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt", encoding="utf-8") as handle:
            while True:
                try:
                    line = handle.readline()
                except EOFError:
                    # The collector may be writing the final gzip member. All
                    # earlier completed members remain valid causal input.
                    invalid += 1
                    break
                if not line:
                    break
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    invalid += 1
                    continue
                if isinstance(item, dict) and number(item.get("event_time_ms")) > 0:
                    rows.append(item)
                else:
                    invalid += 1
    except EOFError:
        invalid += 1
    return rows, invalid


def seconds_by_event(rows: Iterable[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        timestamp = int(number(row.get("event_time_ms")))
        if timestamp > 0:
            grouped[timestamp // 1000].append(row)
    return grouped


def window_sum(rows: list[dict[str, Any]], index: int, key: str, width: int) -> float:
    start = max(0, index - width + 1)
    return sum(number(item.get(key)) for item in rows[start:index + 1])


def build_features(
    trades: Iterable[dict[str, Any]],
    depths: Iterable[dict[str, Any]],
    force_orders: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return causal one-second features sorted by event time."""

    trade_seconds = seconds_by_event(trades)
    depth_seconds = seconds_by_event(depths)
    force_seconds = seconds_by_event(force_orders)
    all_seconds = sorted(set(trade_seconds) | set(depth_seconds) | set(force_seconds))
    if not all_seconds:
        return []

    rows: list[dict[str, Any]] = []
    last_book: dict[str, float] = {}
    last_depth_ms: int | None = None
    for second in range(all_seconds[0], all_seconds[-1] + 1):
        trade_items = trade_seconds.get(second, [])
        depth_items = depth_seconds.get(second, [])
        force_items = force_seconds.get(second, [])
        buy_qty = sell_qty = buy_notional = sell_notional = 0.0
        for item in trade_items:
            quantity = number(item.get("quantity"))
            notional = number(item.get("price")) * quantity
            if item.get("aggressor") == "BUY":
                buy_qty += quantity
                buy_notional += notional
            else:
                sell_qty += quantity
                sell_notional += notional

        bid_added = sum(number(item.get("bid_added_qty")) for item in depth_items)
        bid_removed = sum(number(item.get("bid_removed_qty")) for item in depth_items)
        ask_added = sum(number(item.get("ask_added_qty")) for item in depth_items)
        ask_removed = sum(number(item.get("ask_removed_qty")) for item in depth_items)
        near_fields = (
            "bid_added_near_qty",
            "bid_removed_near_qty",
            "ask_added_near_qty",
            "ask_removed_near_qty",
        )
        near_liquidity_available = bool(depth_items) and all(
            all(field in item for field in near_fields) for item in depth_items
        )
        near_bid_added = sum(number(item.get("bid_added_near_qty")) for item in depth_items)
        near_bid_removed = sum(number(item.get("bid_removed_near_qty")) for item in depth_items)
        near_ask_added = sum(number(item.get("ask_added_near_qty")) for item in depth_items)
        near_ask_removed = sum(number(item.get("ask_removed_near_qty")) for item in depth_items)
        if depth_items:
            latest_depth = max(depth_items, key=lambda item: number(item.get("event_time_ms")))
            last_depth_ms = int(number(latest_depth.get("event_time_ms")))
            last_book = {
                "mid": number(latest_depth.get("mid")),
                "spread_bps": number(latest_depth.get("spread_bps")),
                "bid_depth_n": number(latest_depth.get("bid_depth_n")),
                "ask_depth_n": number(latest_depth.get("ask_depth_n")),
                "book_imbalance": number(latest_depth.get("imbalance_n")),
            }

        force_buy_qty = force_sell_qty = 0.0
        for item in force_items:
            quantity = number(item.get("filled_quantity")) or number(item.get("original_quantity"))
            if item.get("side") == "BUY":
                force_buy_qty += quantity
            elif item.get("side") == "SELL":
                force_sell_qty += quantity

        end_ms = (second + 1) * 1000 - 1
        depth_age_ms = end_ms - last_depth_ms if last_depth_ms is not None else None
        row = {
            "timestamp": iso_second(second),
            "timestamp_ms": second * 1000,
            "trade_count": len(trade_items),
            "buy_qty": buy_qty,
            "sell_qty": sell_qty,
            "buy_notional": buy_notional,
            "sell_notional": sell_notional,
            "signed_flow_qty": buy_qty - sell_qty,
            "signed_flow_notional": buy_notional - sell_notional,
            "depth_update_count": len(depth_items),
            "bid_added_qty": bid_added,
            "bid_removed_qty": bid_removed,
            "ask_added_qty": ask_added,
            "ask_removed_qty": ask_removed,
            "net_bid_liquidity": bid_added - bid_removed,
            "net_ask_liquidity": ask_added - ask_removed,
            "liquidity_pressure": (bid_added - bid_removed) - (ask_added - ask_removed),
            "near_liquidity_available": near_liquidity_available,
            "near_net_bid_liquidity": near_bid_added - near_bid_removed,
            "near_net_ask_liquidity": near_ask_added - near_ask_removed,
            "near_liquidity_pressure": (near_bid_added - near_bid_removed) - (near_ask_added - near_ask_removed),
            "force_buy_qty": force_buy_qty,
            "force_sell_qty": force_sell_qty,
            "force_signed_qty": force_buy_qty - force_sell_qty,
            "depth_age_ms": depth_age_ms,
            "depth_available": depth_age_ms is not None and 0 <= depth_age_ms <= 1500,
            **last_book,
        }
        rows.append(row)

    for index, row in enumerate(rows):
        for width in WINDOWS_SEC:
            buy = window_sum(rows, index, "buy_notional", width)
            sell = window_sum(rows, index, "sell_notional", width)
            flow_total = buy + sell
            row[f"flow_{width}s"] = buy - sell
            row[f"flow_imbalance_{width}s"] = (buy - sell) / flow_total if flow_total else 0.0
            row[f"net_bid_liquidity_{width}s"] = window_sum(rows, index, "net_bid_liquidity", width)
            row[f"net_ask_liquidity_{width}s"] = window_sum(rows, index, "net_ask_liquidity", width)
            row[f"liquidity_pressure_{width}s"] = window_sum(rows, index, "liquidity_pressure", width)
            near_bid = window_sum(rows, index, "near_net_bid_liquidity", width)
            near_ask = window_sum(rows, index, "near_net_ask_liquidity", width)
            bid_depth = number(row.get("bid_depth_n"))
            ask_depth = number(row.get("ask_depth_n"))
            row[f"near_net_bid_liquidity_{width}s"] = near_bid
            row[f"near_net_ask_liquidity_{width}s"] = near_ask
            row[f"near_liquidity_pressure_{width}s"] = near_bid - near_ask
            row[f"near_liquidity_pressure_ratio_{width}s"] = (
                near_bid / bid_depth - near_ask / ask_depth
                if bid_depth > 0.0 and ask_depth > 0.0 and row["near_liquidity_available"]
                else None
            )
            near_observed = sum(
                1 for item in rows[max(0, index - width + 1):index + 1]
                if item["near_liquidity_available"]
            )
            row[f"near_liquidity_coverage_{width}s"] = near_observed / min(width, index + 1)
            row[f"force_signed_qty_{width}s"] = window_sum(rows, index, "force_signed_qty", width)
            prior = rows[max(0, index - width + 1)]
            prior_mid = number(prior.get("mid"))
            current_mid = number(row.get("mid"))
            row[f"ret_{width}s_bps"] = (current_mid - prior_mid) / prior_mid * 10000.0 if prior_mid > 0 and current_mid > 0 else None
            observed = sum(1 for item in rows[max(0, index - width + 1):index + 1] if item["depth_available"])
            row[f"depth_coverage_{width}s"] = observed / min(width, index + 1)
    return rows


def partition_path(root: Path, stream: str, day: str) -> Path:
    return root / stream / f"date={day}" / "events.jsonl"


def read_partition(root: Path, stream: str, day: str) -> tuple[list[dict[str, Any]], int]:
    plain = partition_path(root, stream, day)
    paths = [plain, plain.with_suffix(plain.suffix + ".gz")]
    rows: list[dict[str, Any]] = []
    invalid = 0
    for path in paths:
        loaded, rejected = read_jsonl(path)
        rows.extend(loaded)
        invalid += rejected
    return rows, invalid


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".jsonl.{os.getpid()}.tmp")
    count = 0
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
            count += 1
    os.replace(temporary, path)
    return count


def build_day(input_root: Path, output_root: Path, day: str) -> dict[str, Any]:
    trades, invalid_trades = read_partition(input_root, "trades", day)
    depths, invalid_depths = read_partition(input_root, "depth_updates", day)
    force_orders, invalid_force = read_partition(input_root, "force_orders", day)
    rows = build_features(trades, depths, force_orders)
    output = output_root / f"date={day}" / "features.jsonl"
    written = write_jsonl(output, rows)
    return {
        "day": day,
        "input": {"trades": len(trades), "depth_updates": len(depths), "force_orders": len(force_orders)},
        "invalid": invalid_trades + invalid_depths + invalid_force,
        "output": str(output),
        "rows": written,
        "depth_coverage_60s_last": rows[-1].get("depth_coverage_60s") if rows else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="UTC day, for example 2026-07-12")
    parser.add_argument("--input-root", type=Path, default=DATA_DIR / "auction" / SYMBOL / "futures")
    parser.add_argument("--output-root", type=Path, default=DATA_DIR / "auction_features" / SYMBOL / "futures")
    args = parser.parse_args()
    print(json.dumps(build_day(args.input_root, args.output_root, args.date), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
