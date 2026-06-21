from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from second_backtest.execution import execute_signals
from second_backtest.metrics import summarize_trades


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT / "data" / "btcusdt_1m.csv"
DEFAULT_OUT = ROOT / "tmp" / "fibonacci_1m_research_latest.json"
FIB_LEVELS = (0.236, 0.382, 0.5, 0.618, 0.786)


def bps(a: float, b: float) -> float:
    if not np.isfinite(a) or not np.isfinite(b) or b <= 0:
        return float("nan")
    return float(math.log(a / b) * 10000.0)


def load_1m(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    time_col = "open_time" if "open_time" in df.columns else "timestamp"
    df[time_col] = pd.to_datetime(df[time_col], utc=True)
    df = df.sort_values(time_col).drop_duplicates(time_col).set_index(time_col)
    for col in ("open", "high", "low", "close", "volume"):
      if col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["close"])


def build_signals(
    bars: pd.DataFrame,
    *,
    lookback_min: int,
    tolerance_bps: float,
    min_swing_bps: float,
    mode: str,
    signal_gap_min: int,
) -> list[dict]:
    close = bars["close"].to_numpy(float)
    high_series = pd.Series(bars["high"].to_numpy(float) if "high" in bars else close)
    low_series = pd.Series(bars["low"].to_numpy(float) if "low" in bars else close)
    roll_high = high_series.rolling(lookback_min, min_periods=lookback_min).max().to_numpy(float)
    roll_low = low_series.rolling(lookback_min, min_periods=lookback_min).min().to_numpy(float)
    past = np.roll(close, lookback_min)
    horizon = 10
    rows = []
    last_idx = -10**12
    for i in range(lookback_min, len(close) - horizon):
        if i - last_idx < signal_gap_min:
            continue
        low = float(roll_low[i])
        high = float(roll_high[i])
        price = float(close[i])
        if not np.isfinite(low) or not np.isfinite(high) or high <= low or low <= 0:
            continue
        swing = bps(high, low)
        if swing < min_swing_bps:
            continue
        trend = "UP" if close[i] >= past[i] else "DOWN"
        span = high - low
        best = None
        for level in FIB_LEVELS:
            fib_price = high - span * level if trend == "UP" else low + span * level
            dist = abs(bps(price, fib_price))
            if dist <= tolerance_bps and (best is None or dist < best["distance_bps"]):
                best = {"level": level, "price": fib_price, "distance_bps": dist}
        if not best:
            continue
        if mode == "continuation":
            signal = "DOWN" if trend == "UP" else "UP"
        elif mode == "reversal":
            signal = "UP" if trend == "UP" else "DOWN"
        else:
            raise ValueError(mode)
        settle = float(close[i + horizon])
        won = bool(settle > price if signal == "UP" else settle < price)
        rows.append({
            "strategy_id": f"FIB_1M_{lookback_min}_{mode}_{best['level']}",
            "model_type": "fibonacci_1m",
            "idx": int(i),
            "time": bars.index[i],
            "signal": signal,
            "entry": price,
            "settle_time": bars.index[i + horizon],
            "settle": settle,
            "won": won,
            "horizon_sec": 600,
            "amount": 5.0,
            "fib_level": float(best["level"]),
            "fib_price": round(float(best["price"]), 4),
            "distance_bps": round(float(best["distance_bps"]), 4),
            "swing_bps": round(float(swing), 4),
            "trend": trend,
            "lookback_min": int(lookback_min),
            "tolerance_bps": float(tolerance_bps),
            "min_swing_bps": float(min_swing_bps),
            "mode": mode,
        })
        last_idx = i
    return rows


def by_level(rows, start, end):
    return {
        str(level): summarize_trades([r for r in rows if abs(r["fib_level"] - level) < 1e-9], start, end, amount=5, payout_rate=0.8)
        for level in FIB_LEVELS
    }


def run_grid(bars: pd.DataFrame, *, fast: bool = False) -> dict:
    start, end = bars.index.min(), bars.index.max()
    cases = []
    lookbacks = (60, 120, 240, 360) if fast else (30, 60, 120, 240, 360, 720)
    tolerances = (3.0, 5.0) if fast else (2.0, 3.0, 5.0, 8.0)
    swings = (40.0, 70.0, 100.0) if fast else (20.0, 40.0, 70.0, 100.0, 150.0)
    for lookback_min in lookbacks:
        for tolerance_bps in tolerances:
            for min_swing_bps in swings:
                for mode in ("reversal", "continuation"):
                    raw = build_signals(
                        bars,
                        lookback_min=lookback_min,
                        tolerance_bps=tolerance_bps,
                        min_swing_bps=min_swing_bps,
                        mode=mode,
                        signal_gap_min=10,
                    )
                    executed, rejected = execute_signals(
                        raw,
                        per_strategy_lock=True,
                        cooldown_sec=600,
                        use_horizon_as_lock=True,
                    )
                    metrics = summarize_trades(executed, start, end, amount=5, payout_rate=0.8)
                    cases.append({
                        "lookbackMin": lookback_min,
                        "toleranceBps": tolerance_bps,
                        "minSwingBps": min_swing_bps,
                        "mode": mode,
                        "rawSignals": len(raw),
                        "rejected": len(rejected),
                        "metrics": metrics,
                        "byLevel": by_level(executed, start, end),
                    })
    ranked = sorted(
        [case for case in cases if case["metrics"]["trades"] >= 30],
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
    p.add_argument("--csv", default=str(DEFAULT_CSV))
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--fast", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    bars = load_1m(Path(args.csv))
    result = run_grid(bars, fast=args.fast)
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "source": str(Path(args.csv).resolve()),
        "sample": {
            "start": bars.index.min().isoformat(),
            "end": bars.index.max().isoformat(),
            "hours": round((bars.index.max() - bars.index.min()).total_seconds() / 3600.0, 2),
            "rows": int(len(bars)),
        },
        "method": "1-minute BTC candles only. Prior swing high/low uses past bars only; signals settle 10 bars later.",
        "fastMode": bool(args.fast),
        **result,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report["sample"], ensure_ascii=False))
    for item in report["ranked"][:20]:
        print(json.dumps({
            "lookbackMin": item["lookbackMin"],
            "toleranceBps": item["toleranceBps"],
            "minSwingBps": item["minSwingBps"],
            "mode": item["mode"],
            "metrics": item["metrics"],
        }, ensure_ascii=False))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
