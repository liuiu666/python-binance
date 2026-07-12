"""Backtest a strategy combining 600s normal-belonging labels and multi-period context.

Research-only:
- 600s normal label decides whether price still belongs to the range.
- 180s speed decides whether an edge is a false escape or possible breakout.
- 1800s context blocks obvious counter-trend fades and allows pullback follows.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_belong_600_normal import build_features, label_row  # noqa: E402
from backtest_io import read_orderbook  # noqa: E402
from second_backtest.data import load_second_bars  # noqa: E402


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        v = float(value)
        return v if math.isfinite(v) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def add_long_context(data: pd.DataFrame, f: pd.DataFrame) -> pd.DataFrame:
    close = data["close"].astype(float)
    buy = data["buy_qty"].astype(float).clip(lower=0.0)
    sell = data["sell_qty"].astype(float).clip(lower=0.0)
    high = close.rolling(1800, min_periods=600).max()
    low = close.rolling(1800, min_periods=600).min()
    f["ret_1800s_bps"] = np.log(close / close.shift(1800)) * 10000.0
    f["pos_1800s"] = (close - low) / (high - low).replace(0.0, np.nan)
    buy_60 = buy.rolling(60, min_periods=10).sum()
    sell_60 = sell.rolling(60, min_periods=10).sum()
    buy_180 = buy.rolling(180, min_periods=30).sum()
    sell_180 = sell.rolling(180, min_periods=30).sum()
    f["flow_60"] = (buy_60 - sell_60) / (buy_60 + sell_60).replace(0.0, np.nan)
    f["flow_180"] = (buy_180 - sell_180) / (buy_180 + sell_180).replace(0.0, np.nan)
    for col in (
        "ob_available", "ob_age_sec", "imbalance_20", "microprice_edge_bps",
        "bid_qty_20", "ask_qty_20",
    ):
        if col in data:
            f[col] = data[col]
    f["micro_bps"] = f.get("microprice_edge_bps", np.nan)
    f["bid_ask_ratio"] = f["bid_qty_20"] / f["ask_qty_20"].replace(0.0, np.nan)
    f["ask_bid_ratio"] = f["ask_qty_20"] / f["bid_qty_20"].replace(0.0, np.nan)
    return f


def num(row: pd.Series, key: str, default: float = np.nan) -> float:
    value = row.get(key, default)
    try:
        return float(value)
    except Exception:
        return default


def book_ok(row: pd.Series, signal: str, loose: bool = True) -> bool:
    if not bool(row.get("ob_available", False)) or num(row, "ob_age_sec", 99.0) > 3.0:
        return False
    sign = 1.0 if signal == "UP" else -1.0
    imb_min = 0.04 if loose else 0.08
    micro_min = 0.0004 if loose else 0.001
    flow_guard = -0.15 if loose else -0.03
    ratio = num(row, "bid_ask_ratio") if signal == "UP" else num(row, "ask_bid_ratio")
    return bool(
        sign * num(row, "imbalance_20") >= imb_min
        and sign * num(row, "micro_bps") >= micro_min
        and sign * num(row, "flow_60") >= flow_guard
        and ratio >= 0.85
    )


def long_context(row: pd.Series) -> str:
    ret = num(row, "ret_1800s_bps")
    pos = num(row, "pos_1800s")
    slope600 = num(row, "slope_600_bps")
    if ret >= 18.0 and pos >= 0.62 and slope600 >= 1.0:
        return "long_up"
    if ret <= -18.0 and pos <= 0.38 and slope600 <= -1.0:
        return "long_down"
    return "range"


def decide(row: pd.Series) -> tuple[str | None, str | None, dict[str, Any]]:
    label, label_reason = label_row(row)
    ctx = long_context(row)
    z600 = num(row, "z_600")
    z180 = num(row, "z_180")
    ret60 = num(row, "ret_60s_bps")
    ret180 = num(row, "ret_180s_bps")
    flow60 = num(row, "flow_60")
    speed_ratio = num(row, "sigma_speed_ratio")
    payload = {
        "belongLabel": label,
        "labelReason": label_reason,
        "longContext": ctx,
        "z600": round(z600, 4) if np.isfinite(z600) else None,
        "z180": round(z180, 4) if np.isfinite(z180) else None,
        "ret60": round(ret60, 4) if np.isfinite(ret60) else None,
        "ret180": round(ret180, 4) if np.isfinite(ret180) else None,
        "flow60": round(flow60, 6) if np.isfinite(flow60) else None,
        "speedRatio": round(speed_ratio, 4) if np.isfinite(speed_ratio) else None,
        "ret1800": round(num(row, "ret_1800s_bps"), 4) if np.isfinite(num(row, "ret_1800s_bps")) else None,
        "pos1800": round(num(row, "pos_1800s"), 4) if np.isfinite(num(row, "pos_1800s")) else None,
    }

    if label == "edge_belong":
        if z600 > 0 and ctx != "long_up" and ret60 <= 3.5 and book_ok(row, "DOWN", loose=True):
            return "DOWN", "belong_edge_upper_fade", payload
        if z600 < 0 and ctx != "long_down" and ret60 >= -3.5 and book_ok(row, "UP", loose=True):
            return "UP", "belong_edge_lower_fade", payload

    if label == "outside_no_escape":
        if z600 > 0 and ctx != "long_up" and flow60 <= 0.05 and book_ok(row, "DOWN", loose=True):
            return "DOWN", "outside_no_escape_upper_fade", payload
        if z600 < 0 and ctx != "long_down" and flow60 >= -0.05 and book_ok(row, "UP", loose=True):
            return "UP", "outside_no_escape_lower_fade", payload

    if label == "escape_up":
        # If strong long context and flow confirms, follow; otherwise treat as false escape and fade.
        if ctx == "long_up" and ret180 >= 10.0 and flow60 >= 0.10 and book_ok(row, "UP", loose=True):
            return "UP", "true_escape_up_follow", payload
        if ctx != "long_up" and flow60 <= 0.35 and z180 <= 1.8 and book_ok(row, "DOWN", loose=True):
            return "DOWN", "false_escape_up_fade", payload

    if label == "escape_down":
        if ctx == "long_down" and ret180 <= -10.0 and flow60 <= -0.10 and book_ok(row, "DOWN", loose=True):
            return "DOWN", "true_escape_down_follow", payload
        if ctx != "long_down" and flow60 >= -0.35 and z180 >= -1.8 and book_ok(row, "UP", loose=True):
            return "UP", "false_escape_down_fade", payload

    if label == "core_inside":
        # Only trade core when long trend pulls back to short-window edge.
        if ctx == "long_up" and z180 <= -0.70 and ret60 >= -4.0 and book_ok(row, "UP", loose=True):
            return "UP", "core_long_up_pullback", payload
        if ctx == "long_down" and z180 >= 0.70 and ret60 <= 4.0 and book_ok(row, "DOWN", loose=True):
            return "DOWN", "core_long_down_pullback", payload

    return None, None, payload


def summarize(rows: list[dict[str, Any]], hours: float, amount: float = 5.0, payout: float = 0.8) -> dict[str, Any]:
    equity = peak = max_dd = 0.0
    wins = loss_streak = max_loss_streak = 0
    signed = []
    for row in sorted(rows, key=lambda r: r["time"]):
        won = bool(row["won"])
        equity += amount * payout if won else -amount
        wins += int(won)
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        loss_streak = 0 if won else loss_streak + 1
        max_loss_streak = max(max_loss_streak, loss_streak)
        signed.append(float(row["signed_outcome_bps"]))
    n = len(rows)
    return {
        "trades": n,
        "wins": wins,
        "winRate": round(wins / n * 100.0, 2) if n else 0.0,
        "pnlU": round(equity, 2),
        "maxDrawdownU": round(max_dd, 2),
        "maxLossStreak": max_loss_streak,
        "tradesPerDay": round(n / max(hours, 1e-9) * 24.0, 2),
        "avgSignedBps": round(float(np.mean(signed)), 4) if signed else None,
        "medianSignedBps": round(float(np.median(signed)), 4) if signed else None,
    }


def group_metrics(rows: list[dict[str, Any]], key: str, hours: float) -> dict[str, Any]:
    return {
        str(value): summarize([row for row in rows if str(row.get(key)) == str(value)], hours)
        for value in sorted({str(row.get(key)) for row in rows})
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    bars = load_second_bars(Path(args.seconds), include_shards=not args.no_shards)
    orderbook = read_orderbook(Path(args.orderbook), bars.index, max_age_sec=3)
    data = bars.join(orderbook, how="left").sort_index()
    features = add_long_context(data, build_features(data))
    start = pd.Timestamp(args.start, tz="UTC") if args.start else data.index.max() - pd.Timedelta(hours=24)
    end = pd.Timestamp(args.end, tz="UTC") if args.end else data.index.max() - pd.Timedelta(seconds=args.horizon_sec)
    start = max(start, data.index.min() + pd.Timedelta(seconds=2400))
    end = min(end, data.index.max() - pd.Timedelta(seconds=args.horizon_sec + 5))
    first = int(data.index.searchsorted(start))
    last = min(int(data.index.searchsorted(end)), len(data) - args.horizon_sec)
    close = data["close"].to_numpy(float)
    rows = []
    raw_counts: dict[str, int] = {}
    last_emit = -10**12
    for idx in range(first, last, max(1, args.scan_step_sec)):
        signal, reason, ctx = decide(features.iloc[idx])
        if not signal:
            continue
        raw_counts[reason] = raw_counts.get(reason, 0) + 1
        if idx - last_emit < args.gap_sec:
            continue
        entry = float(close[idx])
        settle = float(close[idx + args.horizon_sec])
        sign = 1.0 if signal == "UP" else -1.0
        signed_bps = (settle / entry - 1.0) * 10000.0 * sign
        rows.append({
            "time": data.index[idx],
            "settle_time": data.index[idx + args.horizon_sec],
            "signal": signal,
            "reason": reason,
            "entry": entry,
            "settle": settle,
            "signed_outcome_bps": signed_bps,
            "won": bool(signed_bps > 0.0),
            **ctx,
        })
        last_emit = idx
    hours = max(0.0, (end - start).total_seconds() / 3600.0)
    pd.DataFrame(clean(rows)).to_csv(args.trades_out, index=False, encoding="utf-8-sig")
    report = {
        "method": "600s belong label + 180s speed + 1800s context",
        "source": {
            "seconds": str(Path(args.seconds).resolve()),
            "orderbook": str(Path(args.orderbook).resolve()),
            "start": start,
            "end": end,
            "hours": round(hours, 4),
        },
        "rawCounts": raw_counts,
        "overall": summarize(rows, hours),
        "byReason": group_metrics(rows, "reason", hours),
        "byBelongLabel": group_metrics(rows, "belongLabel", hours),
        "byLongContext": group_metrics(rows, "longContext", hours),
        "bySignal": group_metrics(rows, "signal", hours),
        "sampleTrades": clean(rows[:10] + rows[-10:] if len(rows) > 20 else rows),
    }
    Path(args.out).write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", required=True)
    parser.add_argument("--orderbook", required=True)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--horizon-sec", type=int, default=600)
    parser.add_argument("--gap-sec", type=int, default=600)
    parser.add_argument("--scan-step-sec", type=int, default=5)
    parser.add_argument("--no-shards", action="store_true")
    parser.add_argument("--out", default=str(ROOT / "tmp" / "belong_multiperiod_strategy.json"))
    parser.add_argument("--trades-out", default=str(ROOT / "tmp" / "belong_multiperiod_strategy_trades.csv"))
    args = parser.parse_args()
    report = run(args)
    print(json.dumps(clean({
        "source": report["source"],
        "rawCounts": report["rawCounts"],
        "overall": report["overall"],
        "byReason": report["byReason"],
        "byBelongLabel": report["byBelongLabel"],
        "byLongContext": report["byLongContext"],
        "bySignal": report["bySignal"],
    }), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
