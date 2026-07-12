"""Research a multi-period normal distribution + speed strategy.

This is an exploratory script. It is intentionally independent from live
strategy code and is meant to answer: if we add 180s speed and 1800s trend
context around the 600s normal window, what kind of signals appear in one day?
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


def rolling_sum(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=max(30, window // 3)).sum()


def add_normal_features(data: pd.DataFrame, out: pd.DataFrame, window: int, prefix: str) -> None:
    close = data["close"].astype(float)
    volume = data["volume"].astype(float).clip(lower=0.0)
    sw = rolling_sum(volume, window)
    sx = rolling_sum(close * volume, window)
    sx2 = rolling_sum(close * close * volume, window)
    mean = close.rolling(window, min_periods=max(30, window // 3)).mean()
    std = close.rolling(window, min_periods=max(30, window // 3)).std(ddof=1)
    vwap = sx / sw.replace(0.0, np.nan)
    var = sx2 / sw.replace(0.0, np.nan) - vwap * vwap
    sigma = np.sqrt(var.clip(lower=0.0)).where(lambda s: s > 1e-9, std)
    center = vwap.fillna(mean)
    z = (close - center) / sigma.replace(0.0, np.nan)
    out[f"center_{prefix}"] = center
    out[f"sigma_{prefix}"] = sigma
    out[f"z_{prefix}"] = z
    out[f"sigma_{prefix}_bps"] = sigma / close * 10000.0
    out[f"inside1_{prefix}"] = z.abs().le(1.0).astype(float).rolling(window, min_periods=max(30, window // 3)).mean()
    out[f"slope_{prefix}_bps"] = (center / center.shift(max(30, min(window // 2, 300))) - 1.0) * 10000.0
    high = close.rolling(window, min_periods=max(30, window // 3)).max()
    low = close.rolling(window, min_periods=max(30, window // 3)).min()
    out[f"pos_{prefix}"] = (close - low) / (high - low).replace(0.0, np.nan)
    out[f"range_{prefix}_bps"] = (high / low - 1.0) * 10000.0


def build_features(data: pd.DataFrame) -> pd.DataFrame:
    close = data["close"].astype(float)
    buy = data["buy_qty"].astype(float).clip(lower=0.0)
    sell = data["sell_qty"].astype(float).clip(lower=0.0)
    out = pd.DataFrame(index=data.index)
    out["close"] = close
    out["observed_pct_600"] = data["observed"].astype(float).rolling(600, min_periods=120).mean() * 100.0
    for window, prefix in ((180, "180"), (600, "600"), (1800, "1800")):
        add_normal_features(data, out, window, prefix)
    for sec in (30, 60, 180, 300, 600, 1800):
        out[f"ret_{sec}s_bps"] = np.log(close / close.shift(sec)) * 10000.0
    buy_60 = buy.rolling(60, min_periods=10).sum()
    sell_60 = sell.rolling(60, min_periods=10).sum()
    buy_180 = buy.rolling(180, min_periods=30).sum()
    sell_180 = sell.rolling(180, min_periods=30).sum()
    out["flow_60"] = (buy_60 - sell_60) / (buy_60 + sell_60).replace(0.0, np.nan)
    out["flow_180"] = (buy_180 - sell_180) / (buy_180 + sell_180).replace(0.0, np.nan)
    out["sigma_speed_ratio"] = out["sigma_180_bps"] / out["sigma_600_bps"].replace(0.0, np.nan)
    for col in (
        "ob_available", "ob_age_sec", "imbalance_20", "microprice_edge_bps",
        "bid_qty_20", "ask_qty_20", "spread_bps",
    ):
        if col in data:
            out[col] = data[col]
    out["micro_bps"] = out.get("microprice_edge_bps", np.nan)
    out["bid_ask_ratio"] = out["bid_qty_20"] / out["ask_qty_20"].replace(0.0, np.nan)
    out["ask_bid_ratio"] = out["ask_qty_20"] / out["bid_qty_20"].replace(0.0, np.nan)
    return out


def finite(row: pd.Series, keys: list[str]) -> bool:
    return all(np.isfinite(float(row.get(key, np.nan))) for key in keys)


def book_ok(row: pd.Series, signal: str, *, loose: bool = False) -> bool:
    sign = 1.0 if signal == "UP" else -1.0
    imb_min = 0.04 if loose else 0.07
    micro_min = 0.0004 if loose else 0.0008
    flow_guard = -0.18 if loose else -0.05
    support_ratio = float(row.get("bid_ask_ratio", np.nan)) if signal == "UP" else float(row.get("ask_bid_ratio", np.nan))
    return bool(
        bool(row.get("ob_available", False))
        and float(row.get("ob_age_sec", 99.0)) <= 3.0
        and sign * float(row.get("imbalance_20", np.nan)) >= imb_min
        and sign * float(row.get("micro_bps", np.nan)) >= micro_min
        and sign * float(row.get("flow_60", np.nan)) >= flow_guard
        and support_ratio >= 0.85
    )


def decide(row: pd.Series) -> tuple[str | None, str | None, dict[str, Any]]:
    keys = [
        "z_180", "z_600", "sigma_180_bps", "sigma_600_bps", "sigma_1800_bps",
        "inside1_600", "slope_180_bps", "slope_600_bps", "ret_60s_bps",
        "ret_180s_bps", "ret_600s_bps", "ret_1800s_bps", "flow_60",
        "flow_180", "sigma_speed_ratio", "pos_1800", "observed_pct_600",
    ]
    if not finite(row, keys) or float(row["observed_pct_600"]) < 90.0:
        return None, None, {}

    z600 = float(row["z_600"])
    z180 = float(row["z_180"])
    sigma600 = float(row["sigma_600_bps"])
    inside600 = float(row["inside1_600"])
    slope180 = float(row["slope_180_bps"])
    slope600 = float(row["slope_600_bps"])
    ret60 = float(row["ret_60s_bps"])
    ret180 = float(row["ret_180s_bps"])
    ret600 = float(row["ret_600s_bps"])
    ret1800 = float(row["ret_1800s_bps"])
    speed_ratio = float(row["sigma_speed_ratio"])
    pos1800 = float(row["pos_1800"])
    flow60 = float(row["flow_60"])
    flow180 = float(row["flow_180"])

    ctx = {
        "z180": round(z180, 4),
        "z600": round(z600, 4),
        "sigma600": round(sigma600, 4),
        "inside600": round(inside600, 4),
        "slope180": round(slope180, 4),
        "slope600": round(slope600, 4),
        "ret60": round(ret60, 4),
        "ret180": round(ret180, 4),
        "ret600": round(ret600, 4),
        "ret1800": round(ret1800, 4),
        "speedRatio": round(speed_ratio, 4),
        "pos1800": round(pos1800, 4),
        "flow60": round(flow60, 6),
        "flow180": round(flow180, 6),
    }

    # 1) Mature normal range: use 600s normal band, but reject fast 180s expansion.
    normal_background = (
        2.4 <= sigma600 <= 10.5
        and inside600 >= 0.58
        and abs(slope600) <= 8.0
        and speed_ratio <= 1.35
        and abs(ret180) <= 14.0
    )
    if normal_background and z600 >= 0.85 and z180 <= 1.15 and ret60 <= 3.0 and book_ok(row, "DOWN", loose=True):
        return "DOWN", "normal_upper_reversion", ctx
    if normal_background and z600 <= -0.85 and z180 >= -1.15 and ret60 >= -3.0 and book_ok(row, "UP", loose=True):
        return "UP", "normal_lower_reversion", ctx

    # 2) Fast volatility expansion: short window is moving faster than 600s normal.
    up_start = (
        speed_ratio >= 1.18
        and ret180 >= 8.0
        and ret60 >= 2.0
        and slope180 >= 2.0
        and flow60 >= 0.03
        and book_ok(row, "UP", loose=True)
    )
    down_start = (
        speed_ratio >= 1.18
        and ret180 <= -8.0
        and ret60 <= -2.0
        and slope180 <= -2.0
        and flow60 <= -0.03
        and book_ok(row, "DOWN", loose=True)
    )
    if up_start:
        return "UP", "speed_up_breakout", ctx
    if down_start:
        return "DOWN", "speed_down_breakout", ctx

    # 3) Long trend pullback: 1800s trend is clear, 180s pulls back and then flow re-aligns.
    long_up = ret1800 >= 18.0 and pos1800 >= 0.62 and slope600 >= 1.5
    long_down = ret1800 <= -18.0 and pos1800 <= 0.38 and slope600 <= -1.5
    if long_up and z180 <= -0.65 and ret60 >= -4.0 and flow180 >= 0.0 and book_ok(row, "UP", loose=True):
        return "UP", "trend_up_pullback_follow", ctx
    if long_down and z180 >= 0.65 and ret60 <= 4.0 and flow180 <= 0.0 and book_ok(row, "DOWN", loose=True):
        return "DOWN", "trend_down_pullback_follow", ctx

    return None, None, ctx


def summarize(rows: list[dict[str, Any]], hours: float, amount: float = 5.0, payout: float = 0.8) -> dict[str, Any]:
    equity = peak = max_dd = 0.0
    wins = loss_streak = max_loss_streak = 0
    signed = []
    for row in sorted(rows, key=lambda r: r["time"]):
        won = bool(row["won"])
        equity += amount * payout if won else -amount
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        wins += int(won)
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
    out = {}
    for value in sorted({str(row.get(key)) for row in rows}):
        part = [row for row in rows if str(row.get(key)) == value]
        out[value] = summarize(part, hours)
    return out


def run(args: argparse.Namespace) -> dict[str, Any]:
    bars = load_second_bars(Path(args.seconds), include_shards=not args.no_shards)
    orderbook = read_orderbook(Path(args.orderbook), bars.index, max_age_sec=3)
    data = bars.join(orderbook, how="left").sort_index()
    features = build_features(data)
    start = pd.Timestamp(args.start, tz="UTC") if args.start else data.index.max() - pd.Timedelta(hours=24)
    end = pd.Timestamp(args.end, tz="UTC") if args.end else data.index.max() - pd.Timedelta(seconds=600)
    start = max(start, data.index.min() + pd.Timedelta(seconds=2400))
    end = min(end, data.index.max() - pd.Timedelta(seconds=605))
    first = int(data.index.searchsorted(start))
    last = int(data.index.searchsorted(end))
    close = data["close"].to_numpy(float)
    horizon = int(args.horizon_sec)
    gap = int(args.gap_sec)
    rows: list[dict[str, Any]] = []
    raw_candidates = 0
    by_raw_reason: dict[str, int] = {}
    last_emit = -10**12
    for idx in range(first, min(last, len(data) - horizon)):
        signal, reason, ctx = decide(features.iloc[idx])
        if not signal:
            continue
        raw_candidates += 1
        by_raw_reason[reason] = by_raw_reason.get(reason, 0) + 1
        if idx - last_emit < gap:
            continue
        entry = float(close[idx])
        settle = float(close[idx + horizon])
        sign = 1.0 if signal == "UP" else -1.0
        signed_bps = (settle / entry - 1.0) * 10000.0 * sign
        rows.append({
            "time": data.index[idx],
            "settle_time": data.index[idx + horizon],
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
    frame = pd.DataFrame(clean(rows))
    if not frame.empty:
        frame.to_csv(args.trades_out, index=False, encoding="utf-8-sig")
    else:
        pd.DataFrame().to_csv(args.trades_out, index=False, encoding="utf-8-sig")
    report = {
        "method": "multi-period normal + speed exploratory strategy",
        "source": {
            "seconds": str(Path(args.seconds).resolve()),
            "orderbook": str(Path(args.orderbook).resolve()),
            "dataStart": data.index.min(),
            "dataEnd": data.index.max(),
            "testStart": start,
            "testEnd": end,
            "hours": round(hours, 4),
        },
        "logic": {
            "normal_reversion": "600s normal range stable, 180s speed not expanding, fade upper/lower reclaim with loose order-book confirmation",
            "speed_breakout": "180s sigma expands vs 600s, 60/180s returns and flow align, follow speed direction",
            "trend_pullback": "1800s trend clear, 180s pulls back, flow/order-book re-align, follow larger trend",
            "cooldownSec": gap,
            "horizonSec": horizon,
        },
        "rawCandidates": raw_candidates,
        "rawByReason": by_raw_reason,
        "executed": summarize(rows, hours),
        "byReason": group_metrics(rows, "reason", hours),
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
    parser.add_argument("--no-shards", action="store_true")
    parser.add_argument("--out", default=str(ROOT / "tmp" / "multiperiod_normal_speed_strategy.json"))
    parser.add_argument("--trades-out", default=str(ROOT / "tmp" / "multiperiod_normal_speed_strategy_trades.csv"))
    args = parser.parse_args()
    report = run(args)
    print(json.dumps(clean({
        "source": report["source"],
        "rawCandidates": report["rawCandidates"],
        "rawByReason": report["rawByReason"],
        "executed": report["executed"],
        "byReason": report["byReason"],
        "bySignal": report["bySignal"],
    }), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
