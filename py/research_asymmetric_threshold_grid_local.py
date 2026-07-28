"""Local robustness grid for asymmetric BTC 30-minute thresholds."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

import research_stable_winrate_local as stable


ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "tmp" / "stable_winrate_local" / "stable_winrate_report.json"
CACHE_PATH = ROOT / "tmp" / "stable_winrate_local" / "walkforward_BTC_30min_resumed.npz"
OUT_PATH = ROOT / "tmp" / "stable_winrate_local" / "asymmetric_threshold_grid.json"


def main() -> None:
    baseline_report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    boundary = pd.to_datetime(baseline_report["method"]["sourceCacheEnd"], utc=True)
    stable.enhanced.OUT = str(stable.DATA_DIR)
    data = stable.enhanced.load_symbol("btcusdt")
    raw = np.load(CACHE_PATH, allow_pickle=False)
    predictions = {key: raw[key] for key in raw.files}
    frame = stable.build_frame(data, predictions)

    down_thresholds = [0.52, 0.55, 0.58, 0.60, 0.62, 0.65]
    up_thresholds = [0.60, 0.62, 0.65, 0.68, 0.70]
    rows = []
    for down_threshold in down_thresholds:
        for up_threshold in up_thresholds:
            policy = stable.combine(
                frame,
                [
                    stable.signal_candidate(
                        frame,
                        threshold=down_threshold,
                        direction_only="DOWN",
                    ),
                    stable.signal_candidate(
                        frame,
                        threshold=up_threshold,
                        direction_only="UP",
                    ),
                ],
            )
            result = stable.evaluate_policy(frame, *policy, boundary)
            row = {
                "downThreshold": down_threshold,
                "upThreshold": up_threshold,
                "trades": result["trades"],
                "winRate": result["winRate"],
                "pnl5U": result["pnl5U"],
                "maxLossStreak": result["maxLossStreak"],
                "positiveBlocks": result["positiveBlocks"],
                "minBlockWinRate": result["minBlockWinRate"],
                "secondHalf": result["secondHalf"],
                "recent20Pct": result["recent20Pct"],
                "untouchedAfterCache": result["untouchedAfterCache"],
            }
            rows.append(row)

    rows.sort(
        key=lambda row: (
            row["untouchedAfterCache"]["edgeOverBreakeven"] or -999,
            row["positiveBlocks"],
            -(row["maxLossStreak"] or 999),
            row["trades"],
        ),
        reverse=True,
    )
    robust = [
        row
        for row in rows
        if row["winRate"] is not None
        and row["winRate"] >= 57.0
        and row["secondHalf"]["winRate"] is not None
        and row["secondHalf"]["winRate"] >= 57.0
        and row["untouchedAfterCache"]["winRate"] is not None
        and row["untouchedAfterCache"]["winRate"] >= 57.0
        and row["positiveBlocks"] >= 8
        and row["maxLossStreak"] <= 8
    ]
    report = {
        "method": {
            "execution": "local_only",
            "downThresholds": down_thresholds,
            "upThresholds": up_thresholds,
            "cooldownBars": stable.COOLDOWN_BARS,
            "untouchedStart": str(boundary),
            "robustCriteria": {
                "overallWinRateMin": 57.0,
                "secondHalfWinRateMin": 57.0,
                "untouchedWinRateMin": 57.0,
                "positiveBlocksMin": 8,
                "maxLossStreakMax": 8,
            },
        },
        "robustCount": len(robust),
        "robustCandidates": robust,
        "allCandidates": rows,
    }
    OUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"robustCount": len(robust), "top": rows[:10]}, ensure_ascii=False, indent=2))
    print(f"Saved {OUT_PATH}")


if __name__ == "__main__":
    main()
