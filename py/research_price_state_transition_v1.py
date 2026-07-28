"""Fixed price-only state classification around a 600-second normal band.

This is a research audit, not a live strategy.  The state rules are fixed
before reading outcomes:

* both 60s and 300s signed returns point toward the old center -> reversion;
* both point away from the old center -> migration/continuation;
* mixed evidence -> no trade.

The signed convention is positive for a move toward the center and negative
for a move farther outside the touched side.  All samples are de-duplicated
with a 600-second spacing before metrics are calculated.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUTS = [
    ROOT / "tmp" / "price_support_resistance_0705_06_samples.csv",
    ROOT / "tmp" / "price_support_resistance_0709_10_samples.csv",
    ROOT / "tmp" / "price_support_resistance_0710_11_samples.csv",
    ROOT / "tmp" / "price_support_resistance_forward_v1_samples.csv",
]
HORIZON_SEC = 600


def select_non_overlapping(frame: pd.DataFrame) -> pd.DataFrame:
    selected = []
    last_time: pd.Timestamp | None = None
    for row in frame.sort_values("eventTime").itertuples(index=False):
        if last_time is not None and row.eventTime < last_time + pd.Timedelta(seconds=HORIZON_SEC):
            continue
        selected.append(row)
        last_time = row.eventTime
    return pd.DataFrame(selected, columns=frame.columns) if selected else frame.iloc[0:0].copy()


def signed_column(frame: pd.DataFrame, name: str) -> pd.Series:
    if name == "ret600":
        sign = np.where(frame.side.eq("LOWER"), 1.0, -1.0)
        return sign * pd.to_numeric(frame["feature_retBps"], errors="coerce")
    # The support/resistance producer already stores these fields in signed
    # convention: positive means movement toward the touched-side center.
    return pd.to_numeric(frame[f"feature_{name}"], errors="coerce")


def add_state(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["signedRet60"] = signed_column(out, "signedRet60Bps")
    out["signedRet300"] = signed_column(out, "signedRet300Bps")
    out["signedRet600"] = signed_column(out, "ret600")
    out["reversionState"] = (out.signedRet60 > 0.0) & (out.signedRet300 > 0.0)
    out["migrationState"] = (out.signedRet60 < 0.0) & (out.signedRet300 < 0.0)
    out["state"] = np.select(
        [out.reversionState, out.migrationState],
        ["reversion", "migration"],
        default="mixed",
    )
    out["predictedSigned"] = np.select(
        [out.reversionState, out.migrationState],
        [1.0, -1.0],
        default=0.0,
    )
    out["won"] = out.predictedSigned * out.signedFinalBps > 0.0
    out["pnlU"] = np.where(out.won, 4.0, -5.0)
    return out


def metrics(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {"trades": 0, "wins": 0, "winRate": None, "pnlU": 0.0, "maxDrawdownU": 0.0, "maxLossStreak": 0}
    pnl = frame.pnlU.to_numpy(float)
    curve = np.cumsum(pnl)
    drawdown = np.maximum.accumulate(np.r_[0.0, curve])[:-1] - curve
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
        "meanSignedMoveBps": round(float(frame.signedFinalBps.mean()), 4),
    }


def summarize(frame: pd.DataFrame) -> dict:
    out = {"all": metrics(frame)}
    for state in ("reversion", "migration", "mixed"):
        group = frame[frame.state == state]
        out[state] = metrics(group)
    for side in ("LOWER", "UPPER"):
        out[side] = metrics(frame[frame.side == side])
    return out


def load_inputs(paths: list[Path]) -> pd.DataFrame:
    frames = []
    for path in paths:
        if not path.exists():
            continue
        frame = pd.read_csv(path)
        frame["source"] = path.stem
        frame["eventTime"] = pd.to_datetime(frame.eventTime, utc=True)
        frames.append(frame)
    if not frames:
        raise FileNotFoundError("no support/resistance sample files found")
    return pd.concat(frames, ignore_index=True).sort_values("eventTime")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", default=None)
    parser.add_argument("--output", default=str(ROOT / "tmp" / "price_state_transition_v1.json"))
    args = parser.parse_args()
    paths = [Path(value) for value in args.input] if args.input else DEFAULT_INPUTS
    raw = load_inputs(paths)
    data = add_state(select_non_overlapping(raw))
    tradeable = data[data.state != "mixed"].copy()

    report = {
        "status": "price_only_state_transition_audit",
        "inputs": [str(path.resolve()) for path in paths if path.exists()],
        "windowSec": 600,
        "horizonSec": 600,
        "deDupSec": 600,
        "fixedRules": {
            "reversion": "signedRet60 > 0 and signedRet300 > 0",
            "migration": "signedRet60 < 0 and signedRet300 < 0",
            "mixed": "otherwise; no trade",
            "signedConvention": "positive means movement toward the touched band center",
        },
        "rawRows": int(len(raw)),
        "nonOverlappingRows": int(len(data)),
        "tradeablePct": round(float(len(tradeable) / len(data) * 100.0), 2) if len(data) else 0.0,
        "all": summarize(data),
        "tradeable": summarize(tradeable),
        "bySource": {
            source: {
                "rows": int(len(group)),
                "tradeablePct": round(float((group.state != "mixed").mean() * 100.0), 2),
                "all": summarize(group),
                "tradeable": summarize(group[group.state != "mixed"]),
            }
            for source, group in data.groupby("source")
        },
        "warning": "Fixed research audit only. No parameter was selected from outcome data and no deployment approval is implied.",
    }
    output = Path(args.output)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
