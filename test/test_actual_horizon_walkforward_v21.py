from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_actual_horizon_walkforward_v21 import training_summary  # noqa: E402


def test_shifted_horizon_summary_uses_delay_one_label_not_fixed_settlement() -> None:
    frame = pd.DataFrame(
        {
            "signal_time": [
                pd.Timestamp("2024-01-10T00:00:00Z"),
                pd.Timestamp("2024-02-10T00:00:00Z"),
                pd.Timestamp("2024-03-10T00:00:00Z"),
            ],
            "status_h10_d0": ["lost", "lost", "lost"],
            "pnl_u_h10_d0": [-5.0, -5.0, -5.0],
            "status_h10_d1": ["won", "won", "won"],
            "pnl_u_h10_d1": [4.0, 4.0, 4.0],
            "status_h10_fixed_d1": ["lost", "lost", "lost"],
            "pnl_u_h10_fixed_d1": [-5.0, -5.0, -5.0],
        }
    )
    shifted = training_summary(
        frame,
        1,
        ["2024-01", "2024-02", "2024-03"],
        seed_key="shifted",
    )
    assert shifted["wins"] == 3
    assert shifted["pnlU"] == 12.0
    assert shifted["positiveMonthPctFixedDenominator"] == 100.0

