"""Test causal 2-minute recovery for guarded low-volatility tail signals.

The live shared core remains unchanged. This research asks whether a signal
blocked because the last 30 seconds are still moving outward can be restored
when the last fully completed 2-minute bar shows a genuine turn or deceleration.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from multi_normal_hf_stable_core import MultiNormalHFStableConfig, evaluate_snapshot  # noqa: E402
from research_two_min_hf_overlay import build_features as build_basic_two_min_features  # noqa: E402
from run_multi_normal_hf_stable_backtest import (  # noqa: E402
    DEFAULT_SOURCES,
    LoadedSource,
    SourceSpec,
    load_sources,
    metrics,
    price_at_or_after,
    utc,
)


OUT_JSON = ROOT / "tmp" / "two_min_guard_recovery_latest.json"
OUT_CSV = ROOT / "tmp" / "two_min_guard_recovery_trades.csv"


@dataclass(frozen=True)
class RecoveryMode:
    key: str
    label: str


MODES = (
    RecoveryMode("guard_only", "当前线上保护版"),
    RecoveryMode("strict_turn", "2分钟已经转向"),
    RecoveryMode("balanced_deceleration", "2分钟平衡减速"),
    RecoveryMode("broad_deceleration", "2分钟宽松减速"),
)


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return value


def build_two_min_features(data: pd.DataFrame) -> pd.DataFrame:
    close = data["close"].astype(float)
    volume = data["volume"].astype(float) if "volume" in data else pd.Series(0.0, index=data.index)
    two = pd.DataFrame(
        {
            "open": close.resample("2min").first(),
            "high": close.resample("2min").max(),
            "low": close.resample("2min").min(),
            "close": close.resample("2min").last(),
            "volume": volume.resample("2min").sum(),
        }
    ).dropna()
    out = build_basic_two_min_features(two)
    out["prev_ret2_bps"] = out["ret2_bps"].shift(1)
    out["ret4_bps"] = (out["close"] / out["close"].shift(2) - 1.0) * 10000.0
    out["ret6_bps"] = (out["close"] / out["close"].shift(3) - 1.0) * 10000.0
    path = out["ret2_bps"].abs().rolling(5, min_periods=5).sum().replace(0.0, np.nan)
    out["efficiency10"] = out["ret10_bps"].abs() / path
    signs = np.sign(out["ret2_bps"])
    out["direction_persistence10"] = signs.rolling(5, min_periods=5).sum().abs() / 5.0
    return out


def aligned_two_row(features: pd.DataFrame, detected_time: pd.Timestamp) -> dict[str, Any] | None:
    # Detection happens at xx:xx:59. Only the 2-minute bar that ended before
    # the next second is available. Adding one second includes a bar that ends
    # exactly at the detection second without looking into the future.
    bar_time = (utc(detected_time) + pd.Timedelta(seconds=1)).floor("2min") - pd.Timedelta(minutes=2)
    if bar_time not in features.index:
        return None
    row = features.loc[bar_time]
    return {str(key): value for key, value in row.items()}


def recovery_decision(
    row: dict[str, Any],
    decision: dict[str, Any],
    two: dict[str, Any] | None,
    mode: str,
) -> tuple[str | None, str | None, dict[str, Any]]:
    if mode == "guard_only" or decision.get("reason") != "lowvol_short_move_still_outward" or not two:
        return None, None, {}
    z = float(row.get("z", float("nan")))
    if not math.isfinite(z) or z == 0.0:
        return None, None, {}
    signal = "DOWN" if z > 0.0 else "UP"
    sign = 1.0 if signal == "UP" else -1.0
    required = ("ret2_bps", "prev_ret2_bps", "sigma10_bps", "regime", "efficiency10")
    if any(key not in two or (key != "regime" and not math.isfinite(float(two[key]))) for key in required):
        return None, None, {}

    signed_ret2 = sign * float(two["ret2_bps"])
    signed_previous = sign * float(two["prev_ret2_bps"])
    signed_acceleration = signed_ret2 - signed_previous
    sigma10 = float(two["sigma10_bps"])
    regime = str(two["regime"])
    opposing_trend = (signal == "UP" and regime == "trend_down") or (signal == "DOWN" and regime == "trend_up")
    strict_turn = signed_ret2 > 0.0 and signed_acceleration > 0.0 and not opposing_trend
    decelerating = signed_ret2 >= -0.5 * sigma10 and signed_acceleration > 0.0 and not opposing_trend
    balanced = regime in {"flat", "transition"} and float(two["efficiency10"]) <= 0.55

    allow = False
    reason = None
    if mode == "strict_turn":
        allow, reason = strict_turn, "two_min_strict_turn_recovery"
    elif mode == "balanced_deceleration":
        allow, reason = decelerating and balanced, "two_min_balanced_deceleration_recovery"
    elif mode == "broad_deceleration":
        allow, reason = decelerating, "two_min_broad_deceleration_recovery"
    payload = {
        "two_regime": regime,
        "two_ret2_bps": float(two["ret2_bps"]),
        "two_prev_ret2_bps": float(two["prev_ret2_bps"]),
        "two_signed_ret2_bps": signed_ret2,
        "two_signed_acceleration_bps": signed_acceleration,
        "two_sigma10_bps": sigma10,
        "two_efficiency10": float(two["efficiency10"]),
        "two_direction_persistence10": float(two["direction_persistence10"]),
    }
    return (signal, reason, payload) if allow else (None, None, payload)


def two_context_payload(two: dict[str, Any] | None, signal: str) -> dict[str, Any]:
    if not two:
        return {}
    required = ("ret2_bps", "prev_ret2_bps", "sigma10_bps", "regime", "efficiency10")
    if any(key not in two or (key != "regime" and not math.isfinite(float(two[key]))) for key in required):
        return {}
    sign = 1.0 if signal == "UP" else -1.0
    signed_ret2 = sign * float(two["ret2_bps"])
    signed_previous = sign * float(two["prev_ret2_bps"])
    acceleration = signed_ret2 - signed_previous
    if signed_ret2 > 0.0 and acceleration > 0.0:
        phase = "turn_accelerating"
    elif signed_ret2 > 0.0:
        phase = "turn_fading"
    elif acceleration > 0.0:
        phase = "outward_slowing"
    else:
        phase = "outward_accelerating"
    return {
        "two_regime": str(two["regime"]),
        "two_phase": phase,
        "two_ret2_bps": float(two["ret2_bps"]),
        "two_prev_ret2_bps": float(two["prev_ret2_bps"]),
        "two_signed_ret2_bps": signed_ret2,
        "two_signed_acceleration_bps": acceleration,
        "two_sigma10_bps": float(two["sigma10_bps"]),
        "two_efficiency10": float(two["efficiency10"]),
        "two_direction_persistence10": float(two["direction_persistence10"]),
    }


def replay(source: LoadedSource, cfg: MultiNormalHFStableConfig, mode: str, delay_sec: int) -> pd.DataFrame:
    close = source.data["close"].astype(float)
    two_features = build_two_min_features(source.data)
    rows: list[dict[str, Any]] = []
    last_emit: pd.Timestamp | None = None
    for snapshot in source.snapshots.sort_values("time").to_dict("records"):
        decision = evaluate_snapshot(snapshot, cfg)
        detected_time = utc(snapshot["time"]) + pd.Timedelta(seconds=59)
        two = aligned_two_row(two_features, detected_time)
        signal = decision.get("signal")
        module = decision.get("module")
        reason = decision.get("reason")
        recovery_payload: dict[str, Any] = {}
        recovered = False
        if not signal:
            signal, recovery_reason, recovery_payload = recovery_decision(snapshot, decision, two, mode)
            if signal:
                module = "two_min_lowvol_recovery"
                reason = recovery_reason
                recovered = True
        if not signal:
            continue
        recovery_payload = {**two_context_payload(two, str(signal)), **recovery_payload}
        if last_emit is not None and (detected_time - last_emit).total_seconds() < cfg.min_gap_sec:
            continue
        entry_target = detected_time + pd.Timedelta(seconds=delay_sec)
        entry = price_at_or_after(close, entry_target)
        settle = price_at_or_after(close, entry_target + pd.Timedelta(seconds=cfg.horizon_sec))
        if entry is None or settle is None:
            continue
        sign = 1.0 if signal == "UP" else -1.0
        outcome = (settle[1] / entry[1] - 1.0) * 10000.0 * sign
        rows.append(
            {
                "source": source.spec.name,
                "role": source.spec.role,
                "mode": mode,
                "entry_time": entry[0],
                "settle_time": settle[0],
                "signal": signal,
                "module": module,
                "reason": reason,
                "recovered": recovered,
                "entry": entry[1],
                "settle": settle[1],
                "signed_outcome_bps": outcome,
                "won": bool(outcome > 0.0),
                **recovery_payload,
            }
        )
        last_emit = detected_time
    return pd.DataFrame(rows)


def load_research_sources(cfg: MultiNormalHFStableConfig) -> list[LoadedSource]:
    independent = SourceSpec(
        "independent_before_today",
        DEFAULT_SOURCES[3].seconds,
        DEFAULT_SOURCES[3].orderbook,
        start=DEFAULT_SOURCES[3].start,
        end="2026-07-11T16:00:00Z",
        role="independent",
    )
    fresh_root = ROOT / "tmp" / "latest_pull_20260712_migration_fix" / "extracted" / "data"
    today = SourceSpec(
        "today",
        fresh_root / "btcusdt_1s_trades.csv",
        fresh_root / "btcusdt_orderbook_1s.csv",
        start="2026-07-11T16:00:00Z",
        role="today",
    )
    return load_sources((*DEFAULT_SOURCES[:3], independent, today), cfg)


def group_report(frame: pd.DataFrame, sources: list[LoadedSource]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for role in ("history", "independent", "today"):
        hours = sum(source.hours for source in sources if source.spec.role == role)
        subset = frame[frame["role"] == role]
        recovered = subset[subset["recovered"]]
        result[role] = {
            "all": metrics(subset, hours),
            "recovered": metrics(recovered, hours),
            "byTwoPhase": {
                str(name): metrics(group, hours)
                for name, group in subset.groupby("two_phase", dropna=False)
            },
            "byModuleAndTwoPhase": {
                f"{module}|{phase}": metrics(group, hours)
                for (module, phase), group in subset.groupby(["module", "two_phase"], dropna=False)
            },
        }
    total_hours = sum(source.hours for source in sources)
    result["combined"] = {
        "all": metrics(frame, total_hours),
        "recovered": metrics(frame[frame["recovered"]], total_hours),
    }
    return result


def main() -> None:
    cfg = MultiNormalHFStableConfig()
    sources = load_research_sources(cfg)
    reports: dict[str, Any] = {}
    all_rows: list[pd.DataFrame] = []
    for mode in MODES:
        mode_frames = [replay(source, cfg, mode.key, 2) for source in sources]
        frame = pd.concat(mode_frames, ignore_index=True) if mode_frames else pd.DataFrame()
        all_rows.append(frame)
        reports[mode.key] = {"label": mode.label, **group_report(frame, sources)}

    delay_report: dict[str, Any] = {}
    for delay in (0, 2, 5, 10):
        frames = [replay(source, cfg, "strict_turn", delay) for source in sources]
        frame = pd.concat(frames, ignore_index=True)
        delay_report[str(delay)] = group_report(frame, sources)

    report = {
        "method": {
            "causalAlignment": "Every decision uses the last fully completed 2-minute bar before the signal minute ends.",
            "base": "The currently deployed 30-second outward-move guard remains the primary strategy.",
            "strictTurn": "Recover only when the completed 2-minute return points toward reversion and improved versus the previous 2-minute bar.",
            "balancedDeceleration": "Recover in flat/transition context when outward movement has slowed to at most half of 2-minute sigma and 10-minute path efficiency is at most 0.55.",
            "broadDeceleration": "Same deceleration rule without requiring a balanced 2-minute regime.",
            "parameterSearch": False,
        },
        "sources": {
            source.spec.name: {
                "role": source.spec.role,
                "start": source.test_start,
                "end": source.test_end,
                "hours": round(source.hours, 4),
            }
            for source in sources
        },
        "modes": reports,
        "strictTurnDelaySweep": delay_report,
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.concat(all_rows, ignore_index=True).to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(json.dumps(clean(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
