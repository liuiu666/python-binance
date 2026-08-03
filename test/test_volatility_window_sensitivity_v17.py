from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_volatility_window_sensitivity_v17 import (  # noqa: E402
    build_volatility_states,
    fixed_settlement_delay_metrics,
    profile_stability_rows,
    remap_states,
)


START = pd.Timestamp("2026-01-01T00:00:00Z")


def _minutes(rows: int = 6_000) -> pd.DataFrame:
    rng = np.random.default_rng(1717)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.0003, rows)))
    index = pd.date_range(START, periods=rows, freq="1min")
    return pd.DataFrame(
        {
            "open": np.r_[100.0, close[:-1]],
            "high": close * 1.0002,
            "low": close * 0.9998,
            "close": close,
            "volume": 1.0,
            "market": "futures",
        },
        index=index,
    )


def test_each_volatility_window_is_causal() -> None:
    minutes = _minutes()
    cutoff = minutes.index[5_500]
    for window in (15, 30, 60, 120):
        full = build_volatility_states(minutes, window).loc[:cutoff]
        truncated = build_volatility_states(minutes.loc[:cutoff], window)
        assert_frame_equal(full, truncated, check_exact=True)


def test_state_mapping_uses_completed_signal_bar() -> None:
    index = pd.date_range(START, periods=3, freq="1min")
    volatility = pd.DataFrame(
        {"vol_state": ["low", "mid", "high"], "rv10m_bps": [1.0, 2.0, 3.0]},
        index=index,
    )
    candidates = pd.DataFrame(
        {
            "signal_bar_time": [index[0], index[1]],
            "signal_time": [index[1], index[2]],
            "profile": ["a", "a"],
        }
    )
    mapped = remap_states(candidates, volatility)
    assert list(mapped["vol_state"]) == ["low", "mid"]
    assert list(mapped["rv10m_bps"]) == [1.0, 2.0]


def test_fixed_delay_keeps_original_contract_settlement() -> None:
    frame = pd.DataFrame(
        {
            "signal": ["UP", "DOWN"],
            "signal_time": [START, START + pd.Timedelta(minutes=10)],
            "entry_time_h10_d1": [
                START + pd.Timedelta(minutes=1),
                START + pd.Timedelta(minutes=11),
            ],
            "entry_h10_d1": [101.0, 101.0],
            "settle_time_h10_d0": [
                START + pd.Timedelta(minutes=10),
                START + pd.Timedelta(minutes=20),
            ],
            "settle_h10_d0": [102.0, 100.0],
        }
    )
    result = fixed_settlement_delay_metrics(frame)
    assert result["trades"] == 2
    assert result["wins"] == 2
    assert result["pnlU"] == 8.0


def test_profile_stability_retrospective_periods_use_full_candidate_history() -> None:
    signal_times = [
        pd.Timestamp("2026-02-10T00:00:00Z"),
        pd.Timestamp("2026-04-10T00:00:00Z"),
    ]
    frame = pd.DataFrame(
        {
            "profile": ["normal_edge_w10_z1p5"] * 2,
            "vol_state": ["mid"] * 2,
            "signal": ["UP"] * 2,
            "signal_time": signal_times,
            "entry_time_h10_d0": signal_times,
            "settle_time_h10_d0": [time + pd.Timedelta(minutes=10) for time in signal_times],
            "status_h10_d0": ["won", "won"],
            "pnl_u_h10_d0": [4.0, 4.0],
            "entry_time_h10_d1": [time + pd.Timedelta(minutes=1) for time in signal_times],
            "entry_h10_d1": [100.0, 100.0],
            "settle_h10_d0": [101.0, 101.0],
        }
    )

    row = profile_stability_rows(frame, 120)[0]
    assert row["2026-02_retrospective_trades"] == 1
    assert row["2026-02_retrospective_pnl_u"] == 4.0
