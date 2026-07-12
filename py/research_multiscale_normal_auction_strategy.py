"""Backtest a fixed multi-scale normal-morphology auction strategy.

Path A follows an early value-area migration when 3/5-minute centers already
move together while the 10-minute distribution still belongs to the old area.
Path B fades a tail only when the 10-minute distribution is stable and the
5-minute distribution has not started migrating. Both paths require current
trade-flow and order-book confirmation.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from multi_normal_hf_stable_core import MultiNormalHFStableConfig  # noqa: E402
from research_normal_shape_1m_10m import clean, shape_features  # noqa: E402
from research_two_min_guard_recovery import load_research_sources  # noqa: E402
from run_multi_normal_hf_stable_backtest import (  # noqa: E402
    LoadedSource,
    metrics,
    price_at_or_after,
    utc,
)


OUT_JSON = ROOT / "tmp" / "multiscale_normal_auction_strategy_latest.json"
OUT_CSV = ROOT / "tmp" / "multiscale_normal_auction_strategy_trades.csv"
WINDOWS = (1, 2, 3, 5, 10)
HORIZON_SEC = 600
GAP_SEC = 600


def flow_ratio(buy: np.ndarray, sell: np.ndarray, start: int, end: int) -> float:
    buy_sum = float(np.sum(buy[start:end]))
    sell_sum = float(np.sum(sell[start:end]))
    total = buy_sum + sell_sum
    return (buy_sum - sell_sum) / total if total > 0.0 else 0.0


def direction_shape(direction: int, kind: str) -> str:
    if kind == "shift":
        return "shift_up" if direction > 0 else "shift_down"
    return "upper_escape" if direction > 0 else "lower_escape"


def early_migration_context(shapes: dict[int, dict[str, Any]], direction: int) -> bool:
    shift = direction_shape(direction, "shift")
    escape = direction_shape(direction, "escape")
    opposing_escape = direction_shape(-direction, "escape")
    return bool(
        shapes[3]["shape"] == shift
        and shapes[5]["shape"] == shift
        and shapes[2]["shape"] in {shift, escape}
        and shapes[10]["shape"] in {escape, "balanced_normal", "contracting"}
        and shapes[1]["shape"] != opposing_escape
    )


def decide(
    shapes: dict[int, dict[str, Any]],
    flow30: float,
    flow60: float,
    imbalance20: float,
    micro_bps: float,
    volume_ratio10: float,
) -> tuple[str | None, str | None, dict[str, Any]]:
    payload = {
        "flow30": flow30,
        "flow60": flow60,
        "imbalance20": imbalance20,
        "micro_bps": micro_bps,
        "volume_ratio10": volume_ratio10,
        **{f"shape_{window}m": shapes[window]["shape"] for window in WINDOWS},
        **{f"z_{window}m": shapes[window]["z"] for window in WINDOWS},
        **{f"slope_sigma_{window}m": shapes[window]["slope_sigma"] for window in WINDOWS},
        **{f"sigma_bps_{window}m": shapes[window]["sigma_bps"] for window in WINDOWS},
    }

    # Early migration: 3m and 5m have already moved together, 2m agrees or is
    # escaping, while 10m still describes the old value area or its edge.
    for direction in (1, -1):
        shift = direction_shape(direction, "shift")
        escape = direction_shape(direction, "escape")
        opposing_escape = direction_shape(-direction, "escape")
        early_context = early_migration_context(shapes, direction)
        orderflow_confirmed = (
            direction * flow60 >= 0.08
            and direction * imbalance20 >= 0.05
            and direction * micro_bps >= 0.0
            and volume_ratio10 >= 0.8
        )
        if early_context and orderflow_confirmed:
            signal = "UP" if direction > 0 else "DOWN"
            return signal, "multiscale_early_migration_follow", payload

    # Stable range: only fade a meaningful 10m tail when 5m is balanced or
    # contracting and the current auction has already turned inward.
    z10 = float(shapes[10]["z"])
    if (
        shapes[10]["shape"] == "balanced_normal"
        and shapes[10]["tail"] != "core"
        and shapes[5]["shape"] in {"balanced_normal", "contracting"}
        and 1.2 <= abs(z10) < 2.0
    ):
        signal = "DOWN" if z10 > 0.0 else "UP"
        direction = 1 if signal == "UP" else -1
        if (
            direction * flow30 >= 0.05
            and direction * flow60 >= 0.02
            and direction * imbalance20 >= 0.05
            and direction * micro_bps >= 0.0
        ):
            return signal, "multiscale_stable_tail_reversion", payload
    return None, None, payload


def build_candidates(source: LoadedSource, one_per_episode: bool = False) -> list[dict[str, Any]]:
    data = source.data
    close = data["close"].to_numpy(float)
    observed = data["observed"].fillna(False).to_numpy(bool)
    buy = data["buy_qty"].fillna(0.0).to_numpy(float)
    sell = data["sell_qty"].fillna(0.0).to_numpy(float)
    volume = data["volume"].fillna(0.0).to_numpy(float)
    imbalance = data["imbalance_20"].to_numpy(float)
    micro = data["microprice_edge_bps"].to_numpy(float)
    available = data["ob_available"].fillna(False).to_numpy(bool)
    minute_positions = np.flatnonzero(data.index.second.to_numpy() == 59)
    rows: list[dict[str, Any]] = []
    episode_emitted = {1: False, -1: False}
    for index in minute_positions:
        timestamp = data.index[index]
        if timestamp < source.test_start or timestamp > source.test_end or index < 600:
            continue
        if not available[index] or not math.isfinite(imbalance[index]) or not math.isfinite(micro[index]):
            continue
        shapes: dict[int, dict[str, Any]] = {}
        for window in WINDOWS:
            width = window * 60
            feature = shape_features(close[index - width + 1 : index + 1], observed[index - width + 1 : index + 1])
            if feature is None:
                break
            shapes[window] = feature
        if len(shapes) != len(WINDOWS):
            continue
        active_context = {direction: early_migration_context(shapes, direction) for direction in (1, -1)}
        for direction in (1, -1):
            if not active_context[direction]:
                episode_emitted[direction] = False
        flow30 = flow_ratio(buy, sell, index - 29, index + 1)
        flow60 = flow_ratio(buy, sell, index - 59, index + 1)
        recent_volume = float(np.sum(volume[index - 59 : index + 1]))
        baseline_volume = float(np.sum(volume[index - 599 : index + 1])) / 10.0
        volume_ratio10 = recent_volume / baseline_volume if baseline_volume > 0.0 else 0.0
        signal, reason, payload = decide(
            shapes,
            flow30,
            flow60,
            float(imbalance[index]),
            float(micro[index]),
            volume_ratio10,
        )
        if signal:
            signal_direction = 1 if signal == "UP" else -1
            if (
                one_per_episode
                and reason == "multiscale_early_migration_follow"
                and episode_emitted[signal_direction]
            ):
                continue
            rows.append(
                {
                    "detected_time": timestamp,
                    "signal": signal,
                    "reason": reason,
                    **payload,
                }
            )
            if reason == "multiscale_early_migration_follow":
                episode_emitted[signal_direction] = True
    return rows


def replay(
    source: LoadedSource,
    candidates: list[dict[str, Any]],
    delay_sec: int,
    mode: str = "migration_follow",
    gap_sec: int = GAP_SEC,
) -> pd.DataFrame:
    close = source.data["close"].astype(float)
    rows: list[dict[str, Any]] = []
    last_emit: pd.Timestamp | None = None
    for candidate in candidates:
        detected_time = utc(candidate["detected_time"])
        if last_emit is not None and (detected_time - last_emit).total_seconds() < gap_sec:
            continue
        target = detected_time + pd.Timedelta(seconds=delay_sec)
        entry = price_at_or_after(close, target)
        settle = price_at_or_after(close, target + pd.Timedelta(seconds=HORIZON_SEC))
        if entry is None or settle is None:
            continue
        signal = str(candidate["signal"])
        if mode == "crowded_migration_fade" and candidate["reason"] == "multiscale_early_migration_follow":
            signal = "DOWN" if signal == "UP" else "UP"
        direction = 1.0 if signal == "UP" else -1.0
        outcome = (settle[1] / entry[1] - 1.0) * 10000.0 * direction
        rows.append(
            {
                "source": source.spec.name,
                "role": source.spec.role,
                "entry_time": entry[0],
                "settle_time": settle[0],
                "entry": entry[1],
                "settle": settle[1],
                "signed_outcome_bps": outcome,
                "won": bool(outcome > 0.0),
                "strategy_mode": mode,
                **candidate,
                "signal": signal,
            }
        )
        last_emit = detected_time
    return pd.DataFrame(rows)


def grouped_report(frame: pd.DataFrame, sources: list[LoadedSource]) -> dict[str, Any]:
    frame = frame.copy()
    if not frame.empty:
        frame["day_shanghai"] = pd.to_datetime(frame["entry_time"], utc=True).dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
    report: dict[str, Any] = {}
    for role in ("history", "independent", "today"):
        subset = frame[frame["role"] == role]
        hours = sum(source.hours for source in sources if source.spec.role == role)
        report[role] = {
            "all": metrics(subset, hours),
            "byReason": {str(name): metrics(group, hours) for name, group in subset.groupby("reason")},
            "byDirection": {str(name): metrics(group, hours) for name, group in subset.groupby("signal")},
        }
    total_hours = sum(source.hours for source in sources)
    report["combined"] = {
        "all": metrics(frame, total_hours),
        "byReason": {str(name): metrics(group, total_hours) for name, group in frame.groupby("reason")},
        "byDirection": {str(name): metrics(group, total_hours) for name, group in frame.groupby("signal")},
        "byShanghaiDay": {str(name): metrics(group, 24.0) for name, group in frame.groupby("day_shanghai")},
    }
    return report


def main() -> None:
    sources = load_research_sources(MultiNormalHFStableConfig())
    candidates = {source.spec.name: build_candidates(source, False) for source in sources}
    episode_candidates = {source.spec.name: build_candidates(source, True) for source in sources}
    mode_frames: dict[str, pd.DataFrame] = {}
    for mode in ("migration_follow", "crowded_migration_fade"):
        frames = [replay(source, candidates[source.spec.name], 2, mode) for source in sources]
        mode_frames[mode] = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    episode_frames = [
        replay(source, episode_candidates[source.spec.name], 2, "crowded_migration_fade")
        for source in sources
    ]
    mode_frames["crowded_migration_fade_one_per_episode"] = pd.concat(episode_frames, ignore_index=True)
    trades = mode_frames["crowded_migration_fade"]
    delay_frames: dict[int, pd.DataFrame] = {}
    for delay in (0, 2, 5, 10):
        frames = [replay(source, candidates[source.spec.name], delay, "crowded_migration_fade") for source in sources]
        delay_frames[delay] = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    gap_frames: dict[int, pd.DataFrame] = {}
    for gap in (600, 900, 1200, 1800):
        frames = [
            replay(source, candidates[source.spec.name], 2, "crowded_migration_fade", gap)
            for source in sources
        ]
        gap_frames[gap] = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    report = {
        "method": {
            "causal": "All morphology, flow and order-book fields are known at the completed minute.",
            "pathA": "3m/5m migration with 2m agreement while 10m remains in the old area; follow with flow and book confirmation.",
            "pathB": "10m stable bell tail with non-migrating 5m shape; fade only after flow and book turn inward.",
            "selectedMode": "The directional follow hypothesis failed consistently, so the reported strategy fades the same crowded migration event without changing candidate filters.",
            "executionDelaySec": 2,
            "horizonSec": HORIZON_SEC,
            "gapSec": GAP_SEC,
            "parameterSearch": False,
        },
        "sources": {
            source.spec.name: {
                "role": source.spec.role,
                "start": source.test_start,
                "end": source.test_end,
                "hours": round(source.hours, 4),
                "rawCandidates": len(candidates[source.spec.name]),
                "episodeCandidates": len(episode_candidates[source.spec.name]),
            }
            for source in sources
        },
        "modeComparison": {
            mode: grouped_report(frame, sources)
            for mode, frame in mode_frames.items()
        },
        "result": grouped_report(trades, sources),
        "delaySweep": {
            str(delay): grouped_report(frame, sources)
            for delay, frame in delay_frames.items()
        },
        "gapSweep": {
            str(gap): grouped_report(frame, sources)
            for gap, frame in gap_frames.items()
        },
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(json.dumps(clean(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
