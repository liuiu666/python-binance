"""Price-only state classification after a 600-second band reclaim.

The input files are reclaim events, not touch events.  Rules are fixed before
outcomes are read:

* signed 30s and 60s returns both positive: the move is returning toward the
  old center, so forecast reversion;
* both negative: the move is leaving the old center, so forecast continuation;
* mixed signs: no trade.

``signedMoveBps`` is positive for the reversion direction and negative for
continuation, so the same signed convention is used for every side.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUTS = [
    ROOT / "tmp" / "price_reclaim_0705_06_nonoverlap.csv",
    ROOT / "tmp" / "price_reclaim_0709_10_nonoverlap.csv",
    ROOT / "tmp" / "price_reclaim_0710_11_nonoverlap.csv",
    ROOT / "tmp" / "price_reclaim_forward_v1_nonoverlap.csv",
]


def metrics(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {"trades": 0, "wins": 0, "winRate": None, "pnlU": 0.0, "maxDrawdownU": 0.0, "maxLossStreak": 0}
    pnl = frame.pnlU.to_numpy(float)
    curve = np.cumsum(pnl)
    peaks = np.maximum.accumulate(np.r_[0.0, curve])[:-1]
    drawdown = peaks - curve
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
        "maxDrawdownU": round(float(drawdown.max()), 2) if len(drawdown) else 0.0,
        "maxLossStreak": int(max_losses),
        "meanSignedMoveBps": round(float(frame.signedMoveBps.mean()), 4),
    }


def classify(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["signedRet60Bps"] = pd.to_numeric(out.signedRet60Bps, errors="coerce")
    out["signedRet30Bps"] = pd.to_numeric(out.signedRet30Bps, errors="coerce")
    out["signedMoveBps"] = pd.to_numeric(out.signedMoveBps, errors="coerce")
    out["state"] = np.select(
        [
            (out.signedRet30Bps > 0.0) & (out.signedRet60Bps > 0.0),
            (out.signedRet30Bps < 0.0) & (out.signedRet60Bps < 0.0),
        ],
        ["reversion", "continuation"],
        default="mixed",
    )
    out["predictedSigned"] = np.select(
        [out.state.eq("reversion"), out.state.eq("continuation")],
        [1.0, -1.0],
        default=0.0,
    )
    out["won"] = out.predictedSigned * out.signedMoveBps > 0.0
    out["pnlU"] = np.where(out.won, 4.0, -5.0)
    return out


def load(paths: list[Path]) -> pd.DataFrame:
    frames = []
    for path in paths:
        if not path.exists():
            continue
        frame = pd.read_csv(path)
        frame["source"] = path.stem
        frame["eventTime"] = pd.to_datetime(frame.eventTime, utc=True)
        frames.append(frame)
    if not frames:
        raise FileNotFoundError("no reclaim event files found")
    return pd.concat(frames, ignore_index=True).sort_values("eventTime")


def summary(frame: pd.DataFrame) -> dict:
    tradeable = frame[frame.state != "mixed"]
    return {
        "allReclaims": metrics(frame),
        "tradeable": metrics(tradeable),
        "reversion": metrics(frame[frame.state == "reversion"]),
        "continuation": metrics(frame[frame.state == "continuation"]),
        "mixedNoTrade": {"events": int((frame.state == "mixed").sum())},
        "bySide": {
            side: {
                "tradeable": metrics(tradeable[tradeable.side == side]),
                "reversion": metrics(frame[(frame.side == side) & (frame.state == "reversion")]),
                "continuation": metrics(frame[(frame.side == side) & (frame.state == "continuation")]),
            }
            for side in ("LOWER", "UPPER")
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", default=None)
    parser.add_argument("--output", default=str(ROOT / "tmp" / "price_reclaim_state_transition_v1.json"))
    args = parser.parse_args()
    paths = [Path(value) for value in args.input] if args.input else DEFAULT_INPUTS
    data = classify(load(paths))
    report = {
        "status": "price_only_reclaim_state_transition_audit",
        "inputs": [str(path.resolve()) for path in paths if path.exists()],
        "windowSec": 600,
        "horizonSec": 600,
        "rules": {
            "reversion": "signedRet30Bps > 0 and signedRet60Bps > 0",
            "continuation": "signedRet30Bps < 0 and signedRet60Bps < 0",
            "mixed": "otherwise; no trade",
            "signedConvention": "positive means movement toward the old center",
            "payout": "win +4U, loss -5U",
        },
        "rows": int(len(data)),
        "summary": summary(data),
        "bySource": {
            source: {
                "rows": int(len(group)),
                "summary": summary(group),
            }
            for source, group in data.groupby("source")
        },
        "warning": "Fixed audit only; no thresholds were selected from outcomes and this is not a deployment approval.",
    }
    output = Path(args.output)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
