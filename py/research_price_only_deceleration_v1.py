"""Pre-registered price-only deceleration/continuation audit.

The decision uses only features available at eventTime. Future prices are used
only for the binary-contract outcome. Events are de-duplicated by a 600-second
minimum gap to match one-open-window execution.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
STAKE_U = 5.0
PAYOUT_RATE = 0.8
HORIZON_SEC = 600
MIN_GAP_SEC = 600
DEFAULT_FREEZE = "2026-07-13T03:45:00Z"


def select_non_overlapping(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    ordered = frame.sort_values("eventTime").copy()
    selected = []
    last_time = None
    for _, row in ordered.iterrows():
        current = pd.Timestamp(row.eventTime)
        if last_time is not None and (current - last_time).total_seconds() < MIN_GAP_SEC:
            continue
        selected.append(row)
        last_time = current
    return pd.DataFrame(selected)


def decide(row: pd.Series, mode: str) -> str | None:
    away_60 = float(row.feature_signedRet60Bps) < 0.0
    slowing = float(row.feature_signedSpeed10Minus60BpsPerSec) > 0.0
    if not away_60:
        return None
    if mode == "revert_deceleration" and not slowing:
        return None
    if mode == "continue_no_deceleration" and slowing:
        return None
    if mode == "revert_deceleration":
        return "UP" if row.side == "LOWER" else "DOWN"
    return "DOWN" if row.side == "LOWER" else "UP"


def evaluate(frame: pd.DataFrame, mode: str) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    out = frame.copy()
    out["signal"] = out.apply(lambda row: decide(row, mode), axis=1)
    out = out[out.signal.notna()].copy()
    direction = np.where(out.signal == "UP", 1.0, -1.0)
    signed_move = out.settleMoveBps.to_numpy(float) * direction
    out["won"] = signed_move > 0.0
    out["pnlU"] = np.where(out.won, STAKE_U * PAYOUT_RATE, -STAKE_U)
    return out


def metrics(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {"trades": 0, "wins": 0, "winRate": None, "pnlU": 0.0, "maxDrawdownU": 0.0, "maxLossStreak": 0}
    wins = frame.won.to_numpy(bool)
    pnl = frame.pnlU.to_numpy(float)
    equity = np.cumsum(pnl)
    peak = np.maximum.accumulate(np.r_[0.0, equity])[1:]
    streak = 0
    max_streak = 0
    for win in wins:
        streak = 0 if win else streak + 1
        max_streak = max(max_streak, streak)
    return {
        "trades": int(len(frame)),
        "wins": int(wins.sum()),
        "winRate": round(float(wins.mean()) * 100.0, 2),
        "pnlU": round(float(pnl.sum()), 2),
        "maxDrawdownU": round(float((peak - equity).max()), 2),
        "maxLossStreak": int(max_streak),
        "meanSignedMoveBps": round(float((frame.settleMoveBps * np.where(frame.signal == "UP", 1.0, -1.0)).mean()), 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(ROOT / "tmp" / "price_support_resistance_forward_v1_samples.csv"))
    parser.add_argument("--freeze-at", default=DEFAULT_FREEZE)
    parser.add_argument("--output", default=str(ROOT / "tmp" / "price_only_deceleration_v1.json"))
    args = parser.parse_args()

    data = pd.read_csv(args.input)
    data["eventTime"] = pd.to_datetime(data.eventTime, utc=True)
    freeze = pd.Timestamp(args.freeze_at)
    discovery = data[data.eventTime < freeze].copy()
    forward = data[data.eventTime >= freeze].copy()
    reports = {}
    trades_for_csv = []
    for mode in ("revert_deceleration", "continue_no_deceleration"):
        all_trades = evaluate(select_non_overlapping(data), mode)
        discovery_trades = all_trades[all_trades.eventTime < freeze]
        forward_trades = all_trades[all_trades.eventTime >= freeze]
        reports[mode] = {
            "rule": (
                "触边后最近60秒仍向外，但最近10秒相对60秒均速减速，做回归"
                if mode == "revert_deceleration"
                else "触边后最近60秒仍向外且没有减速，做继续迁移"
            ),
            "discovery": metrics(discovery_trades),
            "forward": metrics(forward_trades),
            "forwardByUtcDate": {
                str(day): metrics(group)
                for day, group in forward_trades.groupby(forward_trades.eventTime.dt.date)
            },
        }
        labeled = all_trades.copy()
        labeled["mode"] = mode
        trades_for_csv.append(labeled)

    report = {
        "status": "price_only_pre_registered_audit",
        "input": str(Path(args.input).resolve()),
        "freezeAt": freeze.isoformat(),
        "horizonSec": HORIZON_SEC,
        "minGapSec": MIN_GAP_SEC,
        "stakeU": STAKE_U,
        "payoutRate": PAYOUT_RATE,
        "breakevenWinRatePct": round(100.0 * STAKE_U / (STAKE_U + STAKE_U * PAYOUT_RATE), 4),
        "rawSamples": int(len(data)),
        "nonOverlappingSamples": int(len(select_non_overlapping(data))),
        "rules": reports,
        "warning": "The feature definitions are fixed, but this forward slice is still too short for deployment approval; no online strategy was changed.",
    }
    output = Path(args.output)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.concat(trades_for_csv, ignore_index=True).to_csv(
        output.with_name(output.stem + "_trades.csv"), index=False, encoding="utf-8-sig"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
