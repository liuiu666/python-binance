from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from second_backtest.data import load_second_bars
from second_backtest.execution import execute_signals
from second_backtest.metrics import max_loss_streak


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(float(x) / math.sqrt(2.0)))


def bps_ret(close: np.ndarray, i: int, sec: int) -> float:
    j = i - int(sec)
    if j < 0 or close[j] <= 0:
        return float("nan")
    return (close[i] / close[j] - 1.0) * 10000.0


def rolling_position(close: np.ndarray, i: int, sec: int) -> tuple[float, float]:
    start = max(0, i - int(sec) + 1)
    w = close[start : i + 1]
    w = w[np.isfinite(w) & (w > 0)]
    if len(w) < max(60, min(sec, 300)):
        return float("nan"), float("nan")
    lo = float(np.min(w))
    hi = float(np.max(w))
    width = (hi - lo) / max(float(close[i]), 1e-12) * 10000.0
    if hi <= lo:
        return 0.5, width
    return (float(close[i]) - lo) / (hi - lo), width


def flow_imbalance(buy: np.ndarray, sell: np.ndarray, i: int, sec: int) -> float:
    start = max(0, i - int(sec) + 1)
    b = float(np.nansum(buy[start : i + 1]))
    s = float(np.nansum(sell[start : i + 1]))
    return (b - s) / max(b + s, 1e-12)


def settle_row(
    bars: pd.DataFrame,
    i: int,
    signal: str,
    horizon_sec: int,
    extra: dict,
) -> dict:
    close = bars["close"].to_numpy(float)
    entry = float(close[i])
    settle_idx = i + int(horizon_sec)
    settle = float(close[settle_idx])
    return {
        "strategy_id": "DYNAMIC_NORMAL_VOL",
        "model_type": "dynamic_normal_volatility",
        "idx": int(i),
        "time": bars.index[i],
        "signal": signal,
        "entry": entry,
        "settle_time": bars.index[settle_idx],
        "settle": settle,
        "won": bool(settle > entry if signal == "UP" else settle < entry),
        "horizon_sec": int(horizon_sec),
        "amount": 5.0,
        **extra,
    }


def generate_dynamic_normal_vol_signals(
    bars: pd.DataFrame,
    *,
    horizon_sec: int = 600,
    signal_gap_sec: int = 600,
    apply_gap: bool = True,
    mode: str = "hybrid",
) -> list[dict]:
    close = bars["close"].to_numpy(float)
    buy = bars["buy_qty"].to_numpy(float) if "buy_qty" in bars else np.zeros(len(bars))
    sell = bars["sell_qty"].to_numpy(float) if "sell_qty" in bars else np.zeros(len(bars))
    observed = bars["observed"].to_numpy(bool) if "observed" in bars else np.ones(len(bars), dtype=bool)
    if len(close) <= 7200 + horizon_sec:
        return []

    logp = np.log(np.maximum(close, 1e-12))
    lr = np.diff(logp, prepend=np.nan)
    lr_s = pd.Series(lr, index=bars.index)
    sig600 = lr_s.rolling(600, min_periods=240).std(ddof=1).to_numpy(float) * math.sqrt(horizon_sec) * 10000.0
    sig1800 = lr_s.rolling(1800, min_periods=600).std(ddof=1).to_numpy(float) * math.sqrt(horizon_sec) * 10000.0
    sig3600 = lr_s.rolling(3600, min_periods=1200).std(ddof=1).to_numpy(float) * math.sqrt(horizon_sec) * 10000.0
    obs600 = pd.Series(observed.astype(float), index=bars.index).rolling(600, min_periods=600).mean().to_numpy(float)

    rows: list[dict] = []
    last_i = -10**12
    start = 7200
    end = len(close) - horizon_sec
    for i in range(start, end):
        if apply_gap and i - last_i < signal_gap_sec:
            continue
        if not np.isfinite(sig600[i]) or not np.isfinite(sig1800[i]) or not np.isfinite(sig3600[i]):
            continue
        if obs600[i] < 0.98:
            continue

        vol_fast = float(sig600[i])
        vol_mid = float(sig1800[i])
        vol_long = float(sig3600[i])
        vol_ratio = vol_fast / max(vol_long, 1e-12)
        trend5 = bps_ret(close, i, 300)
        trend10 = bps_ret(close, i, 600)
        trend30 = bps_ret(close, i, 1800)
        trend60 = bps_ret(close, i, 3600)
        trend120 = bps_ret(close, i, 7200)
        pos10, range10 = rolling_position(close, i, 600)
        pos30, range30 = rolling_position(close, i, 1800)
        flow5 = flow_imbalance(buy, sell, i, 300)
        flow10 = flow_imbalance(buy, sell, i, 600)

        if not all(np.isfinite(v) for v in (trend5, trend10, trend30, trend60, pos10, pos30, range10, range30)):
            continue
        if vol_mid < 10 or vol_mid > 45:
            continue
        if range10 < 10 or range10 > 90:
            continue

        regime = "normal"
        if abs(trend60) > 100 or abs(trend120) > 160 or vol_ratio > 1.55:
            regime = "strong_trend_or_vol_spike"
        elif vol_ratio < 0.75 and abs(trend30) < 45:
            regime = "compressed_range"

        signal = None
        reason = None

        if mode == "balanced_core":
            down_ok = (
                regime == "compressed_range"
                and pos10 <= 0.15
                and trend5 < -3
                and flow5 <= -0.08
                and vol_ratio <= 0.75
                and 15 <= range10 <= 35
                and abs(trend60) <= 50
            )
            up_ok = (
                regime == "normal"
                and 0.05 <= pos30 <= 0.16
                and trend30 < -5
                and abs(trend60) <= 80
                and 12 <= vol_mid <= 26
                and range10 <= 35
            )
            if down_ok:
                signal = "DOWN"
                reason = "compressed_down_breakout"
            elif up_ok:
                signal = "UP"
                reason = "normal_lower_reversion"
            else:
                continue

        if mode == "compression_down":
            if (
                regime == "compressed_range"
                and pos10 <= 0.15
                and trend5 < -3
                and flow5 <= -0.08
                and vol_ratio <= 0.75
                and 15 <= range10 <= 35
                and abs(trend60) <= 50
            ):
                signal = "DOWN"
                reason = "compressed_down_breakout"
            else:
                continue

        if mode in ("hybrid", "reversion") and regime == "normal":
            if 0.60 <= pos30 < 0.80 and trend30 > 5 and trend60 <= 100 and flow5 <= 0.12:
                signal = "DOWN"
                reason = "normal_upper_reversion"
            elif 0.05 <= pos30 <= 0.20 and trend30 < -5 and trend60 >= -100 and flow5 >= -0.12:
                signal = "UP"
                reason = "normal_lower_reversion"

        if mode in ("hybrid", "breakout") and signal is None and regime == "compressed_range":
            if pos10 >= 0.85 and trend5 > 3 and flow5 > 0.08:
                signal = "UP"
                reason = "compressed_up_breakout"
            elif pos10 <= 0.15 and trend5 < -3 and flow5 < -0.08:
                signal = "DOWN"
                reason = "compressed_down_breakout"

        if mode in ("hybrid", "trend") and signal is None and regime == "strong_trend_or_vol_spike":
            if trend60 > 100 and trend5 > 0 and pos30 < 0.85 and flow5 >= -0.05:
                signal = "UP"
                reason = "strong_uptrend_follow"
            elif trend60 < -100 and trend5 < 0 and pos30 > 0.15 and flow5 <= 0.05:
                signal = "DOWN"
                reason = "strong_downtrend_follow"

        if not signal:
            continue

        z = trend10 / max(vol_fast, 1e-12)
        p_up = normal_cdf(z)
        if signal == "UP" and p_up > 0.72 and reason.endswith("reversion"):
            continue
        if signal == "DOWN" and p_up < 0.28 and reason.endswith("reversion"):
            continue

        last_i = i
        rows.append(
            settle_row(
                bars,
                i,
                signal,
                horizon_sec,
                {
                    "reason": reason,
                    "regime": regime,
                    "p_up": round(float(p_up), 6),
                    "z_score": round(float(z), 6),
                    "vol_fast_bps": round(vol_fast, 6),
                    "vol_mid_bps": round(vol_mid, 6),
                    "vol_long_bps": round(vol_long, 6),
                    "vol_ratio": round(float(vol_ratio), 6),
                    "trend_300s_bps": round(float(trend5), 6),
                    "trend_600s_bps": round(float(trend10), 6),
                    "trend_1800s_bps": round(float(trend30), 6),
                    "trend_3600s_bps": round(float(trend60), 6),
                    "trend_7200s_bps": round(float(trend120), 6),
                    "pos_10m": round(float(pos10), 6),
                    "pos_30m": round(float(pos30), 6),
                    "range_10m_bps": round(float(range10), 6),
                    "range_30m_bps": round(float(range30), 6),
                    "flow_5m": round(float(flow5), 6),
                    "flow_10m": round(float(flow10), 6),
                },
            )
        )
    return rows


def summarize(rows: list[dict], bars: pd.DataFrame) -> dict:
    if not rows:
        return {"trades": 0, "wins": 0, "losses": 0, "winRate": 0.0, "pnl": 0, "maxLoss": 0}
    wins = sum(1 for r in rows if r["won"])
    pnl = sum(4 if r["won"] else -5 for r in rows)
    by_day = []
    frame = pd.DataFrame(rows)
    frame["day_cn"] = pd.to_datetime(frame["time"], utc=True).dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
    for day, g in frame.groupby("day_cn"):
        day_wins = int(g["won"].sum())
        by_day.append({
            "day": day,
            "trades": int(len(g)),
            "winRate": round(day_wins / len(g) * 100.0, 2),
            "pnl": int(sum(4 if bool(x) else -5 for x in g["won"])),
            "maxLoss": int(max_loss_streak([bool(x) for x in g["won"]])),
        })
    return {
        "trades": int(len(rows)),
        "wins": int(wins),
        "losses": int(len(rows) - wins),
        "winRate": round(wins / len(rows) * 100.0, 2),
        "pnl": int(pnl),
        "maxLoss": int(max_loss_streak([bool(r["won"]) for r in rows])),
        "byDay": by_day,
        "byReason": frame.groupby("reason")["won"].agg(
            trades="size",
            wins="sum",
            winRate=lambda s: round(float(s.mean()) * 100.0, 2),
        ).reset_index().to_dict("records"),
    }


def run(args: argparse.Namespace) -> dict:
    bars = load_second_bars(args.csv)
    if args.tail:
        bars = bars.tail(args.tail)
    raw = generate_dynamic_normal_vol_signals(
        bars,
        horizon_sec=args.horizon_sec,
        signal_gap_sec=args.signal_gap_sec,
        apply_gap=True,
        mode=args.mode,
    )
    executed, rejected = execute_signals(
        raw,
        per_strategy_lock=True,
        global_lock_sec=args.global_lock_sec,
        cooldown_sec=args.horizon_sec,
        use_horizon_as_lock=True,
    )
    return {
        "source": str(Path(args.csv).resolve()),
        "tailRows": args.tail,
        "period": {
            "start": bars.index.min().isoformat(),
            "end": bars.index.max().isoformat(),
            "hours": round((bars.index.max() - bars.index.min()).total_seconds() / 3600.0, 2),
            "rows": int(len(bars)),
        },
        "params": {
            "mode": args.mode,
            "horizonSec": args.horizon_sec,
            "signalGapSec": args.signal_gap_sec,
            "globalLockSec": args.global_lock_sec,
        },
        "rawSignals": len(raw),
        "rejected": len(rejected),
        "executed": summarize(executed, bars),
        "sampleTrades": [
            {
                **{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items() if k in {
                    "time", "signal", "won", "reason", "regime", "p_up", "vol_mid_bps",
                    "vol_ratio", "trend_3600s_bps", "pos_30m", "range_10m_bps", "flow_5m",
                }},
            }
            for row in executed[-20:]
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default=str(ROOT / "tmp" / "research_20260626" / "merged_1s_clean_valid_price.csv"))
    parser.add_argument("--out", default=str(ROOT / "tmp" / "dynamic_normal_volatility_research.json"))
    parser.add_argument("--tail", type=int, default=0)
    parser.add_argument("--mode", choices=["hybrid", "reversion", "breakout", "trend", "compression_down", "balanced_core"], default="hybrid")
    parser.add_argument("--horizon-sec", type=int, default=600)
    parser.add_argument("--signal-gap-sec", type=int, default=600)
    parser.add_argument("--global-lock-sec", type=int, default=600)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = run(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
