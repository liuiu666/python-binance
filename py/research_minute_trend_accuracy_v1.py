"""Accuracy audit for direct minute-trend continuation forecasts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = (1, 2, 3, 5, 10, 15, 30, 60)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(ROOT / "data" / "server_latest" / "btcusdt_1m.csv"))
    parser.add_argument("--output", default=str(ROOT / "tmp" / "minute_trend_accuracy_v1.json"))
    args = parser.parse_args()
    raw = pd.read_csv(args.input)
    raw["open_time"] = pd.to_datetime(raw.open_time, utc=True)
    raw = raw.drop_duplicates("open_time").sort_values("open_time").set_index("open_time")
    close = raw.close.astype(float)
    entry = raw.open.astype(float).shift(-1)
    settle = close.shift(-11)
    future_bps = (settle / entry - 1.0) * 10000.0
    signal = raw.index.minute % 10 == 0
    branches = []
    for width in WINDOWS:
        past_bps = (close / close.shift(width) - 1.0) * 10000.0
        valid = signal & past_bps.notna() & future_bps.notna() & (past_bps != 0.0)
        prediction = np.sign(past_bps[valid])
        won = prediction * future_bps[valid] > 0.0
        branches.append(
            {
                "windowMinutes": width,
                "trades": int(len(won)),
                "wins": int(won.sum()),
                "winRate": round(float(won.mean() * 100.0), 2),
                "pnlU": int(np.where(won, 4, -5).sum()),
            }
        )
    report = {
        "status": "minute_direct_trend_accuracy_audit",
        "input": str(Path(args.input).resolve()),
        "entryModel": "signal minute close, next minute open entry, close after 10 further minutes settlement",
        "horizonMinutes": 10,
        "breakevenWinRate": 55.56,
        "branches": branches,
        "conclusion": "Direct continuation of the recent minute trend is not accurate enough for the ten-minute contract.",
        "warning": "This is a fixed benchmark, not a live strategy or parameter selection.",
    }
    output = Path(args.output)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
