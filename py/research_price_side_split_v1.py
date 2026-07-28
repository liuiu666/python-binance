"""Fixed side-split audit for price-only 600-second normal states."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]


def metrics(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {"trades": 0, "wins": 0, "winRate": None, "pnlU": 0.0, "maxDrawdownU": 0.0, "maxLossStreak": 0}
    pnl = frame.pnlU.to_numpy(float)
    curve = np.cumsum(pnl)
    peak = np.maximum.accumulate(np.r_[0.0, curve])[:-1]
    losses = 0
    max_losses = 0
    for won in frame.won:
        losses = 0 if won else losses + 1
        max_losses = max(max_losses, losses)
    return {
        "trades": int(len(frame)),
        "wins": int(frame.won.sum()),
        "winRate": round(float(frame.won.mean() * 100.0), 2),
        "pnlU": round(float(frame.pnlU.sum()), 2),
        "maxDrawdownU": round(float((peak - curve).max()), 2),
        "maxLossStreak": int(max_losses),
    }


def audit(frame: pd.DataFrame, name: str, mask: pd.Series, prediction: int) -> dict:
    selected = frame[mask].copy()
    selected["won"] = prediction * selected.raw_move_bps > 0.0
    selected["pnlU"] = np.where(selected.won, 4.0, -5.0)
    return {
        "rule": name,
        "summary": metrics(selected),
        "byUtcDate": {
            str(day): metrics(group)
            for day, group in selected.groupby(selected.time.dt.date)
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(ROOT / "tmp" / "unified_long_price_events_10m.csv"))
    parser.add_argument("--output", default=str(ROOT / "tmp" / "price_side_split_v1.json"))
    args = parser.parse_args()
    frame = pd.read_csv(args.input)
    frame["time"] = pd.to_datetime(frame.time, utc=True)
    z = pd.to_numeric(frame.z_60, errors="coerce")
    ret60 = pd.to_numeric(frame.ret_60, errors="coerce")
    ret300 = pd.to_numeric(frame.ret_300, errors="coerce")
    report = {
        "status": "price_only_side_split_audit",
        "input": str(Path(args.input).resolve()),
        "windowSec": 600,
        "horizonSec": 600,
        "payout": "win +4U, loss -5U",
        "fixedRules": {
            "lowerMigration": "z60 <= -1 and ret60 < 0 and ret300 < 0; forecast DOWN",
            "upperMigration": "z60 >= +1 and ret60 > 0 and ret300 > 0; forecast UP",
            "lowerReversion": "z60 <= -1 and ret60 > 0 and ret300 > 0; forecast UP",
            "upperReversion": "z60 >= +1 and ret60 < 0 and ret300 < 0; forecast DOWN",
        },
        "rows": int(len(frame)),
        "branches": [
            audit(frame, "lowerMigration", (z <= -1) & (ret60 < 0) & (ret300 < 0), -1),
            audit(frame, "upperMigration", (z >= 1) & (ret60 > 0) & (ret300 > 0), 1),
            audit(frame, "lowerReversion", (z <= -1) & (ret60 > 0) & (ret300 > 0), 1),
            audit(frame, "upperReversion", (z >= 1) & (ret60 < 0) & (ret300 < 0), -1),
        ],
        "warning": "Fixed side split only; no rule was selected from these outcomes and this is not deployment approval.",
    }
    output = Path(args.output)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
