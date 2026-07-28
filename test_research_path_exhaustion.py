from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from pandas.testing import assert_frame_equal


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "py"))

from research_long_minute_consensus_v1 import read_minutes  # noqa: E402
from research_path_exhaustion_reclaim_v2 import build_candidates  # noqa: E402
from current_v2_augmented_v9_core import (  # noqa: E402
    AugmentedV9Rules,
    build_confirmed_supplement_candidates,
    latest_confirmed_supplement,
)
from research_normal_liquidity_orderbook import read_orderbook  # noqa: E402
from second_backtest.data import load_second_bars  # noqa: E402


def test_exhaustion_signal_is_unchanged_when_future_is_removed() -> None:
    minutes = read_minutes()
    cutoff = pd.Timestamp("2026-07-14T12:00:00Z")
    full = build_candidates(minutes)
    truncated = build_candidates(minutes.loc[:cutoff + pd.Timedelta(minutes=11)])
    columns = [
        "time", "signal", "efficiency_10", "trend_strength", "z_30",
        "ret_1", "ret_3", "ret_10", "volume_ratio",
    ]
    expected = full[full.time.le(cutoff)][columns].reset_index(drop=True)
    actual = truncated[truncated.time.le(cutoff)][columns].reset_index(drop=True)
    assert not expected.empty
    assert_frame_equal(actual, expected, check_exact=True)


def test_shared_v9_core_uses_continuous_minutes_and_is_causal() -> None:
    folder = ROOT / "tmp" / "frozen_position_forward"
    bars = load_second_bars(folder / "btcusdt_1s_trades.csv", include_shards=False)
    data = bars.join(
        read_orderbook(folder / "btcusdt_orderbook_1s.csv", bars.index),
        how="left",
    ).sort_index()
    rules = AugmentedV9Rules()
    candidates = build_confirmed_supplement_candidates(data, rules)
    assert len(candidates) == 15
    # The old research path incorrectly bridged a multi-day gap into 00:01.
    assert pd.Timestamp("2026-07-14T00:01:59Z") not in set(candidates.detected_time)

    detected = pd.Timestamp("2026-07-14T02:28:59Z")
    truncated = data.loc[:detected + pd.Timedelta(seconds=1)]
    latest = latest_confirmed_supplement(
        truncated,
        detected + pd.Timedelta(seconds=1),
        rules,
    )
    assert latest is not None
    assert latest["signal"] == "UP"
    assert latest["votes"] >= 2
