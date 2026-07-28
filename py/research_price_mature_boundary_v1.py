"""Price-only audit of mature boundary acceptance versus rejection."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from research_price_only_deceleration_v1 import metrics, select_non_overlapping


ROOT = Path(__file__).resolve().parents[1]
FREEZE_AT = pd.Timestamp("2026-07-13T03:45:00Z")
MATURE_AGE_SEC = 120.0


def decision(row: pd.Series, mode: str) -> str | None:
    age = float(row.feature_sideOutsideAgeSec)
    if age < MATURE_AGE_SEC:
        return None
    if mode == "upper_mature_continue" and row.side == "UPPER":
        return "UP"
    if mode == "lower_mature_continue" and row.side == "LOWER":
        return "DOWN"
    return None


def evaluate(data: pd.DataFrame, mode: str) -> pd.DataFrame:
    rows = select_non_overlapping(data)
    rows = rows.copy()
    rows["signal"] = rows.apply(lambda row: decision(row, mode), axis=1)
    rows = rows[rows.signal.notna()].copy()
    direction = np.where(rows.signal == "UP", 1.0, -1.0)
    signed = rows.settleMoveBps.to_numpy(float) * direction
    rows["won"] = signed > 0.0
    rows["pnlU"] = np.where(rows.won, 4.0, -5.0)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(ROOT / "tmp" / "price_support_resistance_forward_v1_samples.csv"))
    parser.add_argument("--output", default=str(ROOT / "tmp" / "price_mature_boundary_v1.json"))
    args = parser.parse_args()
    data = pd.read_csv(args.input)
    data["eventTime"] = pd.to_datetime(data.eventTime, utc=True)
    report = {
        "status": "price_only_mature_boundary_audit",
        "input": str(Path(args.input).resolve()),
        "freezeAt": FREEZE_AT.isoformat(),
        "matureAgeSec": MATURE_AGE_SEC,
        "rule": "触边后在同一侧边界外持续至少120秒，视为旧价值区被接受，沿迁移方向预测；上下沿分开统计。",
        "branches": {},
        "warning": "This is a fixed research branch, not deployment approval; the current forward slice is short and has been inspected descriptively.",
    }
    for mode in ("upper_mature_continue", "lower_mature_continue"):
        trades = evaluate(data, mode)
        discovery = trades[trades.eventTime < FREEZE_AT]
        forward = trades[trades.eventTime >= FREEZE_AT]
        report["branches"][mode] = {
            "discovery": metrics(discovery),
            "forward": metrics(forward),
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
