"""Apply causal completed-2m context to existing high-frequency trades.

The 2-minute layer never replaces second/order-book entry logic.  It uses only
the most recently completed 2-minute bar to classify market state and optionally
confirm that reversal has started.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MINUTES = ROOT / "data" / "server_latest" / "btcusdt_1m.csv"
DEFAULT_TRADES = ROOT / "tmp" / "multi_normal_hf_stable_v1_trades_latest.csv"
DEFAULT_OUT = ROOT / "tmp" / "two_min_hf_overlay_latest.json"
DEFAULT_2M = ROOT / "tmp" / "btcusdt_2m.csv"
DEFAULT_OVERLAY = ROOT / "tmp" / "two_min_hf_overlay_trades.csv"


def aggregate_two_minutes(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True, errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna().set_index("open_time").sort_index()
    return frame.resample("2min", label="left", closed="left").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).dropna()


def trend_state(ret10: float, ret30: float, ret60: float) -> str:
    up = int(ret10 >= 8.0) + int(ret30 >= 18.0) + int(ret60 >= 28.0)
    down = int(ret10 <= -8.0) + int(ret30 <= -18.0) + int(ret60 <= -28.0)
    if up >= 2 and down == 0:
        return "trend_up"
    if down >= 2 and up == 0:
        return "trend_down"
    if abs(ret10) <= 7.0 and abs(ret30) <= 18.0 and abs(ret60) <= 28.0:
        return "flat"
    if up > down:
        return "drift_up"
    if down > up:
        return "drift_down"
    return "transition"


def build_features(two: pd.DataFrame) -> pd.DataFrame:
    out = two.copy()
    close = out["close"].astype(float)
    for minutes, bars in ((2, 1), (10, 5), (30, 15), (60, 30)):
        out[f"ret{minutes}_bps"] = (close / close.shift(bars) - 1.0) * 10000.0
    out["sigma10_bps"] = close.rolling(5, min_periods=5).std(ddof=1) / close * 10000.0
    out["range10_bps"] = (out["high"].rolling(5, min_periods=5).max() / out["low"].rolling(5, min_periods=5).min() - 1.0) * 10000.0
    center = close.rolling(15, min_periods=15).mean()
    sigma = close.rolling(15, min_periods=15).std(ddof=1)
    out["center30"] = center
    out["z30"] = (close - center) / sigma.replace(0.0, np.nan)
    out["center_slope10_bps"] = (center / center.shift(5) - 1.0) * 10000.0
    out["volume_ratio30"] = out["volume"] / out["volume"].rolling(15, min_periods=15).mean()
    out["regime"] = [
        trend_state(float(a), float(b), float(c)) if np.isfinite(a) and np.isfinite(b) and np.isfinite(c) else "unknown"
        for a, b, c in zip(out["ret10_bps"], out["ret30_bps"], out["ret60_bps"])
    ]
    return out


def metrics(frame: pd.DataFrame, hours: float) -> dict[str, Any]:
    if frame.empty:
        return {"trades": 0, "winRate": None, "pnlU": 0.0, "maxDrawdownU": 0.0, "maxLossStreak": 0, "tradesPerDay": 0.0}
    ordered = frame.sort_values("entry_time")
    pnls = np.where(ordered["won"].astype(bool), 4.0, -5.0)
    equity = np.cumsum(pnls)
    peaks = np.maximum.accumulate(np.maximum(equity, 0.0))
    wins = int(ordered["won"].astype(bool).sum())
    current = maximum = 0
    for won in ordered["won"].astype(bool):
        current = 0 if won else current + 1
        maximum = max(maximum, current)
    return {
        "trades": int(len(ordered)),
        "winRate": round(wins / len(ordered) * 100.0, 2),
        "pnlU": round(float(pnls.sum()), 2),
        "maxDrawdownU": round(float(np.max(peaks - equity)), 2),
        "maxLossStreak": maximum,
        "tradesPerDay": round(len(ordered) / max(hours, 1e-9) * 24.0, 2),
    }


def context_pass(row: pd.Series) -> bool:
    module = str(row["module"])
    regime = str(row["two_regime"])
    if module == "lowvol_normal_reversion":
        return (
            regime == "flat"
            and float(row["two_sigma10_bps"]) < 3.0
            and float(row["two_range10_bps"]) <= 20.0
            and abs(float(row["two_ret10_bps"])) <= 5.0
        )
    if module == "mature_trend_exhaustion":
        original_trend = str(row["trend"])
        direction = 1.0 if original_trend == "trend_up" else -1.0
        required_z = 0.5 if float(row["two_sigma10_bps"]) >= 8.0 else 1.2
        return regime == original_trend and direction * float(row["two_z30"]) >= required_z
    return False


def run(minutes_path: Path, trades_path: Path) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame]:
    two = aggregate_two_minutes(minutes_path)
    features = build_features(two)
    trades = pd.read_csv(trades_path)
    for column in ("entry_time", "settle_time", "detected_time"):
        trades[column] = pd.to_datetime(trades[column], utc=True)
    trades["two_bar_time"] = trades["entry_time"].dt.floor("2min") - pd.Timedelta(minutes=2)
    selected = features.reindex(pd.DatetimeIndex(trades["two_bar_time"])).reset_index(drop=True)
    selected.columns = [f"two_{column}" for column in selected.columns]
    joined = pd.concat([trades.reset_index(drop=True), selected], axis=1)
    required = ["two_regime", "two_sigma10_bps", "two_range10_bps", "two_ret10_bps", "two_ret2_bps", "two_z30"]
    joined["two_available"] = joined[required].notna().all(axis=1) & joined["two_regime"].ne("unknown")
    covered = joined[joined["two_available"]].copy()
    covered["two_context_pass"] = covered.apply(context_pass, axis=1)
    signal_sign = np.where(covered["signal"].eq("UP"), 1.0, -1.0)
    covered["two_turn_pass"] = covered["two_context_pass"] & (signal_sign * covered["two_ret2_bps"].astype(float) > 0.0)

    hours = max((covered["entry_time"].max() - covered["entry_time"].min()).total_seconds() / 3600.0, 1e-9) if not covered.empty else 0.0
    report = {
        "method": {
            "purpose": "Use completed 2-minute OHLCV only as context for the existing second/order-book high-frequency strategy.",
            "causalAlignment": "Each trade uses the last fully completed 2-minute bar before entry.",
            "contextRule": "Low-volatility signals require 2m flat/low-vol context; trend-exhaustion signals require matching 2m mature trend and directional 30m z-extension.",
            "turnRule": "The strict layer additionally requires the last completed 2m return to point in the predicted reversal direction.",
            "parameterSearch": False,
        },
        "twoMinuteData": {
            "rows": len(two),
            "start": two.index.min().isoformat(),
            "end": two.index.max().isoformat(),
        },
        "coverage": {"hfTrades": len(joined), "coveredTrades": len(covered), "missingTrades": int((~joined["two_available"]).sum())},
        "coveredBase": metrics(covered, hours),
        "contextConfirmed": metrics(covered[covered["two_context_pass"]], hours),
        "contextRejected": metrics(covered[~covered["two_context_pass"]], hours),
        "contextAndTurnConfirmed": metrics(covered[covered["two_turn_pass"]], hours),
        "byRole": {
            role: {
                "base": metrics(group, hours),
                "context": metrics(group[group["two_context_pass"]], hours),
                "contextAndTurn": metrics(group[group["two_turn_pass"]], hours),
            }
            for role, group in covered.groupby("role")
        },
        "byModule": {
            module: {
                "base": metrics(group, hours),
                "context": metrics(group[group["two_context_pass"]], hours),
                "contextAndTurn": metrics(group[group["two_turn_pass"]], hours),
            }
            for module, group in covered.groupby("module")
        },
        "byShanghaiDay": {
            day: {
                "base": metrics(group, 24.0),
                "context": metrics(group[group["two_context_pass"]], 24.0),
                "rejected": metrics(group[~group["two_context_pass"]], 24.0),
            }
            for day, group in covered.assign(
                day_shanghai=covered["entry_time"].dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
            ).groupby("day_shanghai")
        },
    }
    return report, two, covered


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=Path, default=DEFAULT_MINUTES)
    parser.add_argument("--hf-trades", type=Path, default=DEFAULT_TRADES)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--two-minutes-out", type=Path, default=DEFAULT_2M)
    parser.add_argument("--overlay-trades-out", type=Path, default=DEFAULT_OVERLAY)
    args = parser.parse_args()
    report, two, overlay = run(args.minutes, args.hf_trades)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    two.to_csv(args.two_minutes_out, index_label="open_time")
    overlay.to_csv(args.overlay_trades_out, index=False, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
