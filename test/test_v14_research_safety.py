from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from v14_research_safety import (  # noqa: E402
    assert_shadow_only_candidate,
    replay_prefix_only,
    shadow_only_candidate_metadata,
)
from v14_validation import (  # noqa: E402
    apply_family_cooldown,
    deduplicate_candidates,
    normalize_candidates,
    normalize_futures_ticks,
    resolve_candidate_trades,
)


START = pd.Timestamp("2026-01-01T00:00:00Z")


def test_candidate_metadata_is_explicitly_shadow_only_and_never_real():
    metadata = shadow_only_candidate_metadata(
        "V14_CORE",
        "V14 Core",
        extra={"parameterVersion": "frozen-001"},
    )

    assert metadata["status"] == "research_shadow_only"
    assert metadata["researchOnly"] is True
    assert metadata["observationMode"] == "shadow"
    assert metadata["shadowOnly"] is True
    assert metadata["tradeEnabled"] is False
    assert metadata["realTradingEnabled"] is False
    assert metadata["realTradingAllowed"] is False
    assert metadata["autoTrade"] is False
    assert metadata["deployable"] is False
    assert_shadow_only_candidate(metadata)


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("observationMode", "live"),
        ("shadowOnly", False),
        ("tradeEnabled", True),
        ("realTradingEnabled", True),
        ("realTradingAllowed", True),
        ("autoTrade", True),
        ("deployable", True),
    ],
)
def test_candidate_metadata_rejects_every_unsafe_override(field: str, unsafe_value: object):
    with pytest.raises(ValueError, match="cannot be overridden"):
        shadow_only_candidate_metadata(
            "V14_ROBUST",
            "V14 Robust",
            extra={field: unsafe_value},
        )


def test_duplicate_candidates_do_not_increase_trade_count():
    one = pd.DataFrame(
        [{"time": START, "signal": "UP", "family": "V14", "branch": "core"}]
    )
    duplicated = pd.concat([one.assign(source="snapshot_a"), one.assign(source="snapshot_b")])
    ticks = normalize_futures_ticks(
        pd.DataFrame(
            {
                "time": [START + pd.Timedelta(seconds=1), START + pd.Timedelta(seconds=601)],
                "price": [100.0, 101.0],
                "market": ["futures", "futures"],
            }
        )
    )

    def resolved(frame: pd.DataFrame) -> pd.DataFrame:
        normalized = normalize_candidates(frame)
        deduped = deduplicate_candidates(normalized)
        cooled = apply_family_cooldown(deduped, cooldown_sec=600)
        return resolve_candidate_trades(cooled, ticks, delays_sec=(0,), horizon_sec=600)

    baseline = resolved(one)
    repeated = resolved(duplicated)

    assert len(baseline) == len(repeated) == 1
    assert repeated.iloc[0]["trade_key"] == baseline.iloc[0]["trade_key"]
    assert repeated.iloc[0]["duplicate_count"] == 2


def test_six_hundred_second_cooldown_is_shared_by_v14_aliases():
    raw = pd.DataFrame(
        [
            {
                "time": START,
                "signal": "UP",
                "family": "V14",
                "branch": "core",
                "strategy_id": "V14_CORE",
            },
            {
                "time": START + pd.Timedelta(seconds=599),
                "signal": "DOWN",
                "family": "V14",
                "branch": "robust",
                "strategy_id": "V14_ROBUST",
            },
            {
                "time": START + pd.Timedelta(seconds=600),
                "signal": "DOWN",
                "family": "V14",
                "branch": "regime",
                "strategy_id": "V14_REGIME",
            },
        ]
    )

    kept = apply_family_cooldown(normalize_candidates(raw), cooldown_sec=600)

    assert list(kept["strategy_id"]) == ["V14_CORE", "V14_REGIME"]
    assert list(kept["time"]) == [START, START + pd.Timedelta(seconds=600)]


def test_settlement_ignores_pre_target_tick_and_uses_first_futures_tick_after_target():
    candidate = normalize_candidates(
        pd.DataFrame(
            [{"time": START, "signal": "UP", "family": "V14", "branch": "core"}]
        )
    )
    ticks = normalize_futures_ticks(
        pd.DataFrame(
            {
                "time": [
                    START,
                    START + pd.Timedelta(seconds=2),
                    START + pd.Timedelta(seconds=601),
                    START + pd.Timedelta(seconds=603),
                ],
                "price": [50.0, 100.0, 80.0, 110.0],
                "market": ["futures"] * 4,
            }
        )
    )

    trade = resolve_candidate_trades(
        candidate,
        ticks,
        delays_sec=(0,),
        horizon_sec=600,
    ).iloc[0]

    # V14 execution starts one second after detection.  Entry resolves at +2;
    # settlement target is therefore +602, so the +601 tick is forbidden.
    assert trade["entry_target_time"] == START + pd.Timedelta(seconds=1)
    assert trade["entry_time"] == START + pd.Timedelta(seconds=2)
    assert trade["settle_target_time"] == START + pd.Timedelta(seconds=602)
    assert trade["settle_time"] == START + pd.Timedelta(seconds=603)
    assert trade["settle_price"] == 110.0
    assert trade["status"] == "won"


def test_causal_replay_never_exposes_rows_after_the_decision_timestamp():
    index = pd.date_range(START, periods=6, freq="1s")
    frame = pd.DataFrame({"close": [100, 101, 102, 103, 104, 105]}, index=index)
    observed_prefix_ends: list[pd.Timestamp] = []

    def evaluator(prefix: pd.DataFrame) -> dict[str, object]:
        observed_prefix_ends.append(pd.Timestamp(prefix.index.max()))
        return {
            "signal": "UP" if float(prefix["close"].iloc[-1]) >= float(prefix["close"].iloc[0]) else "DOWN",
            "prefix_sum": float(prefix["close"].sum()),
        }

    decisions = replay_prefix_only(frame, evaluator, start_pos=1, end_pos=4)

    assert observed_prefix_ends == list(index[1:5])
    assert list(decisions["as_of"]) == list(index[1:5])
    assert list(decisions["history_rows"]) == [2, 3, 4, 5]


def test_mutating_future_rows_cannot_change_already_emitted_decisions():
    index = pd.date_range(START, periods=8, freq="1s")
    original = pd.DataFrame({"close": [100, 101, 102, 103, 104, 105, 106, 107]}, index=index)
    mutated = original.copy()
    mutated.loc[index[5]:, "close"] = [-10_000, 50_000, -90_000]

    def evaluator(prefix: pd.DataFrame) -> dict[str, object]:
        return {
            "signal": "UP" if float(prefix["close"].tail(3).mean()) >= 100.0 else "DOWN",
            "rolling_mean": float(prefix["close"].tail(3).mean()),
        }

    before = replay_prefix_only(original, evaluator, start_pos=2, end_pos=4)
    after = replay_prefix_only(mutated, evaluator, start_pos=2, end_pos=4)

    pd.testing.assert_frame_equal(before, after)


def test_causal_replay_rejects_a_future_dated_decision():
    frame = pd.DataFrame(
        {"close": [100.0, 101.0]},
        index=pd.date_range(START, periods=2, freq="1s"),
    )

    with pytest.raises(ValueError, match="future decision timestamp"):
        replay_prefix_only(
            frame,
            lambda prefix: {"time": prefix.index[-1] + pd.Timedelta(seconds=1), "signal": "UP"},
            start_pos=1,
            end_pos=1,
        )
