from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_structure_gate_v18 import (  # noqa: E402
    GATES,
    STATE_ACTIONS,
    build_structure_features,
    select_variant,
    structure_gate_masks,
)


START = pd.Timestamp("2026-01-01T00:00:00Z")


def _minutes(rows: int = 12_000) -> pd.DataFrame:
    rng = np.random.default_rng(1818)
    returns = rng.normal(0.0, 0.0003, rows)
    close = 100.0 * np.exp(np.cumsum(returns))
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


def test_structure_features_are_unchanged_when_future_is_removed() -> None:
    minutes = _minutes()
    cutoff = minutes.index[10_500]
    full = build_structure_features(minutes).loc[:cutoff]
    truncated = build_structure_features(minutes.loc[:cutoff])
    assert_frame_equal(full, truncated, check_exact=True)


def test_stable_mean_reversion_gate_requires_chop_crossing_and_no_shock() -> None:
    row = pd.DataFrame(
        {
            "efficiency60": [0.1],
            "efficiency60_q33": [0.2],
            "efficiency60_q50": [0.3],
            "efficiency60_q67": [0.4],
            "trend_score120": [0.2],
            "trend_score120_q50": [0.3],
            "trend_score120_q67": [0.4],
            "variance_ratio5": [0.6],
            "variance_ratio5_q33": [0.7],
            "variance_ratio5_q50": [0.8],
            "crossing_rate120": [0.2],
            "crossing_rate120_q50": [0.1],
            "crossing_rate120_q67": [0.15],
            "volatility_shock": [0.8],
            "volatility_shock_q67": [1.0],
        }
    )
    masks = structure_gate_masks(row)
    assert bool(masks["stable_mr"].iloc[0])

    shocked = row.copy()
    shocked["volatility_shock"] = 1.2
    assert not bool(structure_gate_masks(shocked)["stable_mr"].iloc[0])


def test_v18_grid_excludes_unconfirmed_high_volatility_continuation() -> None:
    assert set(GATES) == {
        "none",
        "not_trending",
        "choppy",
        "mr_signature",
        "balanced_mr",
        "stable_mr",
    }
    high = [action for action in STATE_ACTIONS if action.state == "high"]
    assert {action.research_role for action in high} == {
        "confirmed_reversal",
        "exhaustion_reversal",
    }


def _selection_rows(boundary_won: bool) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for month in (1, 2):
        month_start = pd.Timestamp(year=2026, month=month, day=1, tz="UTC")
        for index in range(30):
            won = index < 18
            signal_time = month_start + pd.Timedelta(minutes=10 * index)
            settle = 101.0 if won else 99.0
            rows.append(
                {
                    "variant": "low|normal_edge_w60_z2p5|choppy",
                    "profile": "normal_edge_w60_z2p5",
                    "vol_state": "low",
                    "state_action": "direct_reversion",
                    "structure_gate": "choppy",
                    "signal": "UP",
                    "signal_time": signal_time,
                    "settle_time_h10_d0": signal_time + pd.Timedelta(minutes=10),
                    "settle_time_h10_d1": signal_time + pd.Timedelta(minutes=11),
                    "status_h10_d0": "won" if won else "lost",
                    "pnl_u_h10_d0": 4.0 if won else -5.0,
                    "status_h10_d1": "won" if won else "lost",
                    "pnl_u_h10_d1": 4.0 if won else -5.0,
                    "entry_time_h10_d1": signal_time + pd.Timedelta(minutes=1),
                    "entry_h10_d1": 100.0,
                    "settle_h10_d0": settle,
                }
            )
    train_end = pd.Timestamp("2026-04-01T00:00:00Z")
    rows.append(
        {
            **rows[0],
            "signal_time": train_end - pd.Timedelta(minutes=10),
            "settle_time_h10_d0": train_end,
            "settle_time_h10_d1": train_end + pd.Timedelta(minutes=1),
            "status_h10_d0": "won" if boundary_won else "lost",
            "pnl_u_h10_d0": 4.0 if boundary_won else -5.0,
            "status_h10_d1": "won" if boundary_won else "lost",
            "pnl_u_h10_d1": 4.0 if boundary_won else -5.0,
            "settle_h10_d0": 101.0 if boundary_won else 99.0,
        }
    )
    return pd.DataFrame(rows)


def test_variant_selection_purges_every_execution_label_at_fold_boundary() -> None:
    train_end = pd.Timestamp("2026-04-01T00:00:00Z")
    lost = select_variant(
        _selection_rows(False), "low", train_end, tier="exploratory"
    )
    won = select_variant(
        _selection_rows(True), "low", train_end, tier="exploratory"
    )
    assert lost == won
    assert lost is not None
    assert lost["variant"] == "low|normal_edge_w60_z2p5|choppy"

