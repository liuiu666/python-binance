"""Distribution-free price path audit for ten-minute events."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]


def metrics(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {"trades": 0, "wins": 0, "winRate": None, "pnlU": 0.0, "maxDrawdownU": 0.0}
    pnl = frame.pnlU.to_numpy(float)
    curve = np.cumsum(pnl)
    peak = np.maximum.accumulate(np.r_[0.0, curve])[:-1]
    return {
        "trades": int(len(frame)),
        "wins": int(frame.won.sum()),
        "winRate": round(float(frame.won.mean() * 100.0), 2),
        "pnlU": round(float(frame.pnlU.sum()), 2),
        "maxDrawdownU": round(float((peak - curve).max()), 2),
    }


def audit(data: pd.DataFrame, name: str, mask: pd.Series, prediction: pd.Series) -> dict:
    frame = data[mask].copy()
    frame["prediction"] = prediction[mask]
    frame["won"] = frame.prediction * frame.raw_move_bps > 0.0
    frame["pnlU"] = np.where(frame.won, 4.0, -5.0)
    return {
        "rule": name,
        "summary": metrics(frame),
        "byUtcDate": {
            str(day): metrics(group)
            for day, group in frame.groupby(frame.time.dt.date)
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(ROOT / "tmp" / "unified_long_price_events_10m.csv"))
    parser.add_argument("--output", default=str(ROOT / "tmp" / "research_distribution_free_v1.json"))
    args = parser.parse_args()
    data = pd.read_csv(args.input)
    data["time"] = pd.to_datetime(data.time, utc=True)
    ret = data[["ret_60", "ret_120", "ret_300", "ret_600"]].apply(pd.to_numeric, errors="coerce")
    signs = np.sign(ret.to_numpy(float))
    sign_sum = signs.sum(axis=1)
    strength = pd.to_numeric(data.trend_strength_600, errors="coerce")
    rules = [
        audit(data, "direction_consensus_3_of_4", abs(sign_sum) >= 3, pd.Series(np.sign(sign_sum), index=data.index)),
        audit(data, "direction_consensus_4_of_4", abs(sign_sum) == 4, pd.Series(np.sign(sign_sum), index=data.index)),
        audit(
            data,
            "efficiency_proxy_1_plus_long_agreement",
            (strength >= 1.0) & (np.sign(data.ret_300) == np.sign(data.ret_600)),
            pd.Series(np.sign(data.ret_600), index=data.index),
        ),
        audit(
            data,
            "long_trend_pullback",
            (np.sign(data.ret_300) == np.sign(data.ret_600))
            & (np.sign(data.ret_60) == -np.sign(data.ret_600)),
            pd.Series(np.sign(data.ret_600), index=data.index),
        ),
        audit(
            data,
            "short_long_disagreement_reversal",
            (np.sign(data.ret_60) == -np.sign(data.ret_300))
            & (np.sign(data.ret_300) != 0),
            pd.Series(-np.sign(data.ret_300), index=data.index),
        ),
    ]
    report = {
        "status": "distribution_free_price_audit",
        "input": str(Path(args.input).resolve()),
        "windowSec": 600,
        "horizonSec": 600,
        "payout": "win +4U, loss -5U",
        "formulas": {
            "directionConsensus": "sign(ret60)+sign(ret120)+sign(ret300)+sign(ret600), require absolute vote >= 3",
            "efficiencyProxy": "abs(ret600) / trend_strength denominator, require trend_strength_600 >= 1 and ret300/ret600 agree",
            "longTrendPullback": "ret300 and ret600 agree, ret60 temporarily opposes; follow the long direction",
            "shortLongDisagreement": "ret60 opposes ret300; forecast the short reversal",
        },
        "rows": int(len(data)),
        "branches": rules,
        "breakevenWinRatePct": 55.56,
        "warning": "These are fixed distribution-free audits, not parameter selection or deployment approval.",
    }
    output = Path(args.output)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
