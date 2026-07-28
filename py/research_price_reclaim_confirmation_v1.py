"""Price-only confirmation after reclaim, with fixed 30s/60s persistence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from research_price_only_deceleration_v1 import metrics, select_non_overlapping


ROOT = Path(__file__).resolve().parents[1]
FREEZE_AT = pd.Timestamp("2026-07-13T03:45:00Z")


def with_outcome(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["signal"] = np.where(out.side == "LOWER", "UP", "DOWN")
    out["won"] = out.signedMoveBps > 0.0
    out["pnlU"] = np.where(out.won, 4.0, -5.0)
    return out


def select_confirmation(frame: pd.DataFrame, mode: str) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    confirmed = (frame.signedRet30Bps > 0.0) & (frame.signedRet60Bps > 0.0)
    if mode == "confirmed_both":
        return frame[confirmed].copy()
    if mode == "confirmed_three":
        return frame[confirmed & (frame.signedRet10Bps > 0.0)].copy()
    return frame.copy()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(ROOT / "tmp" / "price_reclaim_forward_v1_nonoverlap.csv"))
    parser.add_argument("--output", default=str(ROOT / "tmp" / "price_reclaim_confirmation_v1.json"))
    args = parser.parse_args()
    raw = pd.read_csv(args.input)
    raw["eventTime"] = pd.to_datetime(raw.eventTime, utc=True)
    data = with_outcome(select_non_overlapping(raw))
    report = {
        "status": "price_only_reclaim_confirmation_audit",
        "input": str(Path(args.input).resolve()),
        "freezeAt": FREEZE_AT.isoformat(),
        "fixedRule": "回收后过去30秒和60秒的价格都朝中心移动；三段确认版本额外要求过去10秒也朝中心移动。",
        "rawNonOverlapping": int(len(data)),
        "branches": {},
        "warning": "The confirmation definitions are fixed for this audit; the forward slice is not a deployment approval.",
    }
    for mode in ("baseline_all_reclaims", "confirmed_both", "confirmed_three"):
        selected = data if mode == "baseline_all_reclaims" else select_confirmation(data, mode)
        discovery = selected[selected.eventTime < FREEZE_AT]
        forward = selected[selected.eventTime >= FREEZE_AT]
        report["branches"][mode] = {
            "eligiblePct": round(len(selected) / len(data) * 100.0, 2) if len(data) else 0.0,
            "discovery": metrics(discovery),
            "forward": metrics(forward),
            "forwardBySide": {
                side: metrics(group)
                for side, group in forward.groupby("side")
            },
            "forwardByUtcDate": {
                str(day): metrics(group)
                for day, group in forward.groupby(forward.eventTime.dt.date)
            },
        }
    output = Path(args.output)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
