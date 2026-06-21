from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from research_arrival_forecast import DEFAULT_OLD_CSV, DEFAULT_SHARD_DIR, load_bars
from second_backtest.execution import execute_signals
from second_backtest.metrics import summarize_trades


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "tmp" / "fibonacci_level_research_latest.json"
FIB_LEVELS = (0.236, 0.382, 0.5, 0.618, 0.786)


def bps(a: float, b: float) -> float:
    if not np.isfinite(a) or not np.isfinite(b) or b <= 0:
        return float("nan")
    return float(math.log(a / b) * 10000.0)


def build_fib_signals(
    bars: pd.DataFrame,
    *,
    lookback_sec: int,
    tolerance_bps: float,
    min_swing_bps: float,
    mode: str,
    signal_gap_sec: int,
) -> list[dict]:
    close = bars["close"].to_numpy(float)
    horizon_sec = 600
    series = pd.Series(close)
    roll_low = series.rolling(lookback_sec, min_periods=lookback_sec).min().to_numpy(float)
    roll_high = series.rolling(lookback_sec, min_periods=lookback_sec).max().to_numpy(float)
    past = np.roll(close, lookback_sec)
    rows: list[dict] = []
    last_idx = -10**12
    for i in range(lookback_sec, len(close) - horizon_sec):
        if i - last_idx < signal_gap_sec:
            continue
        low = float(roll_low[i])
        high = float(roll_high[i])
        if not np.isfinite(low) or not np.isfinite(high) or low <= 0 or high <= low:
            continue
        swing_bps = bps(high, low)
        if swing_bps < min_swing_bps:
            continue
        trend = "UP" if close[i] >= past[i] else "DOWN"
        span = high - low
        price = float(close[i])
        best = None
        for level in FIB_LEVELS:
            if trend == "UP":
                fib_price = high - span * level
            else:
                fib_price = low + span * level
            distance = abs(bps(price, fib_price))
            if distance <= tolerance_bps and (best is None or distance < best["distance_bps"]):
                best = {"level": level, "price": fib_price, "distance_bps": distance}
        if best is None:
            continue
        if mode == "reversal":
            signal = "UP" if trend == "UP" else "DOWN"
        elif mode == "continuation":
            signal = "DOWN" if trend == "UP" else "UP"
        elif mode == "level_reject":
            # At a pullback level, bet that price rejects the level and resumes the prior swing.
            signal = "UP" if trend == "UP" else "DOWN"
        else:
            raise ValueError(f"unknown mode: {mode}")
        entry = price
        settle = float(close[i + horizon_sec])
        won = bool(settle > entry if signal == "UP" else settle < entry)
        rows.append(
            {
                "strategy_id": f"FIB_{lookback_sec}_{mode}_{best['level']}",
                "model_type": "fibonacci_level",
                "idx": int(i),
                "time": bars.index[i],
                "signal": signal,
                "entry": entry,
                "settle_time": bars.index[i + horizon_sec],
                "settle": settle,
                "won": won,
                "horizon_sec": horizon_sec,
                "amount": 5.0,
                "fib_level": float(best["level"]),
                "fib_price": round(float(best["price"]), 4),
                "distance_bps": round(float(best["distance_bps"]), 4),
                "swing_bps": round(float(swing_bps), 4),
                "trend": trend,
                "lookback_sec": int(lookback_sec),
                "tolerance_bps": float(tolerance_bps),
                "min_swing_bps": float(min_swing_bps),
                "mode": mode,
            }
        )
        last_idx = i
    return rows


def side_metrics(rows: list[dict], start: pd.Timestamp, end: pd.Timestamp) -> dict:
    return {
        side: summarize_trades([row for row in rows if row["signal"] == side], start, end, amount=5, payout_rate=0.8)
        for side in ("UP", "DOWN")
    }


def by_level_metrics(rows: list[dict], start: pd.Timestamp, end: pd.Timestamp) -> dict:
    out = {}
    for level in FIB_LEVELS:
        subset = [row for row in rows if abs(float(row["fib_level"]) - level) < 1e-9]
        out[str(level)] = summarize_trades(subset, start, end, amount=5, payout_rate=0.8)
    return out


def run_grid(bars: pd.DataFrame) -> dict:
    start, end = bars.index.min(), bars.index.max()
    cases = []
    for lookback_sec in (900, 1800, 2700, 3600):
        for tolerance_bps in (2.0, 3.0, 5.0):
            for min_swing_bps in (25.0, 40.0, 60.0):
                for mode in ("reversal", "continuation"):
                    raw = build_fib_signals(
                        bars,
                        lookback_sec=lookback_sec,
                        tolerance_bps=tolerance_bps,
                        min_swing_bps=min_swing_bps,
                        mode=mode,
                        signal_gap_sec=600,
                    )
                    executed, rejected = execute_signals(
                        raw,
                        per_strategy_lock=True,
                        cooldown_sec=600,
                        use_horizon_as_lock=True,
                    )
                    metrics = summarize_trades(executed, start, end, amount=5, payout_rate=0.8)
                    cases.append(
                        {
                            "lookbackSec": lookback_sec,
                            "toleranceBps": tolerance_bps,
                            "minSwingBps": min_swing_bps,
                            "mode": mode,
                            "rawSignals": len(raw),
                            "rejected": len(rejected),
                            "metrics": metrics,
                            "bySide": side_metrics(executed, start, end),
                            "byLevel": by_level_metrics(executed, start, end),
                            "sampleTrades": [
                                {
                                    "time": row["time"].isoformat(),
                                    "signal": row["signal"],
                                    "won": row["won"],
                                    "fib_level": row["fib_level"],
                                    "trend": row["trend"],
                                    "entry": round(float(row["entry"]), 2),
                                    "settle": round(float(row["settle"]), 2),
                                    "swing_bps": row["swing_bps"],
                                }
                                for row in executed[-8:]
                            ],
                        }
                    )
    ranked = sorted(
        [case for case in cases if case["metrics"]["trades"] >= 20],
        key=lambda case: (
            case["metrics"]["pnl"],
            case["metrics"]["winRate"] or 0,
            -case["metrics"]["maxLoss"],
            case["metrics"]["trades"],
        ),
        reverse=True,
    )
    return {"cases": cases, "ranked": ranked}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--old-csv", default=str(DEFAULT_OLD_CSV))
    p.add_argument("--shard-dir", default=str(DEFAULT_SHARD_DIR))
    p.add_argument("--out", default=str(DEFAULT_OUT))
    return p.parse_args()


def main() -> None:
    args = parse_args()
    bars = load_bars(Path(args.old_csv), Path(args.shard_dir))
    result = run_grid(bars)
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "sample": {
            "start": bars.index.min().isoformat(),
            "end": bars.index.max().isoformat(),
            "hours": round((bars.index.max() - bars.index.min()).total_seconds() / 3600.0, 2),
            "rows": int(len(bars)),
            "observedPct": round(float(bars["observed"].mean() * 100), 2),
        },
        "method": "Use only past 1-second bars. Detect prior swing high/low in lookback window, trade at Fibonacci retracement levels, settle after 600 seconds.",
        **result,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report["sample"], ensure_ascii=False))
    print("RANKED")
    for item in report["ranked"][:20]:
        print(json.dumps({
            "lookbackSec": item["lookbackSec"],
            "toleranceBps": item["toleranceBps"],
            "minSwingBps": item["minSwingBps"],
            "mode": item["mode"],
            "metrics": item["metrics"],
        }, ensure_ascii=False))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
