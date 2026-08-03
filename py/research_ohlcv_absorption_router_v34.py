"""V34 causal OHLCV absorption confirmation router.

Research only.  V34 keeps the frozen V19 action profiles and V32 walk-forward
gates, adding only causal minute candle, volume and range confirmation variants.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

from research_full_history_regime_walkforward_v31 import (
    EARLY_INPUT,
    EARLY_MANIFEST,
    LATE_INPUT,
    LATE_MANIFEST,
    calendar_folds,
    load_frozen_history,
)
from research_full_history_stationarity_router_v32 import (
    EXECUTION_SPECS,
    MAIN_EXECUTIONS,
    NEIGHBOR_EXECUTIONS,
    TRAINING_WINDOWS_MONTHS,
    fixed_profile_audit,
    run_walkforward_mode,
)
from research_minute_volatility_normal_v15 import clean
from research_multiregime_strategy_v16 import (
    AMOUNT_U,
    HORIZONS_MIN,
    PAYOUT_RATE,
    _boundary_mask,
)
from research_stationarity_router_v19 import (
    PROFILES as BASE_PROFILES,
    VOLATILITY_WINDOW_MIN,
    _signal_masks,
)
from research_volatility_window_sensitivity_v17 import build_volatility_states


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "tmp" / "v34_ohlcv_absorption_router_20260730.json"
OUT_CANDIDATES = ROOT / "tmp" / "v34_ohlcv_absorption_router_candidates_20260730.csv"
OUT_PROFILE_AUDIT = ROOT / "tmp" / "v34_ohlcv_absorption_router_profile_audit_20260730.csv"
OUT_SELECTIONS = ROOT / "tmp" / "v34_ohlcv_absorption_router_selections_20260730.csv"
OUT_TRADES = ROOT / "tmp" / "v34_ohlcv_absorption_router_trades_20260730.csv"

VOL_CELLS = ("low", "mid", "high")
GATES = (
    "ungated_control",
    "directional_candle",
    "volume_confirmed",
    "climactic_confirmed",
)
FEATURE_HISTORY_MIN = 7 * 24 * 60
FEATURE_MIN_PERIODS = 3 * 24 * 60
FEATURE_QUANTILE = 0.80


@dataclass(frozen=True)
class V34Profile:
    name: str
    family: str
    base_name: str
    base_family: str
    gate: str
    lookback_min: int
    threshold: float


PROFILES = tuple(
    V34Profile(
        name=f"v34_{base.name.removeprefix('v19_')}__{gate}",
        family=f"{base.family}__{gate}",
        base_name=base.name,
        base_family=base.family,
        gate=gate,
        lookback_min=base.lookback_min,
        threshold=base.threshold,
    )
    for base in BASE_PROFILES
    for gate in GATES
)


def build_ohlcv_features(minutes: pd.DataFrame) -> pd.DataFrame:
    open_price = minutes["open"].astype(float)
    high = minutes["high"].astype(float)
    low = minutes["low"].astype(float)
    close = minutes["close"].astype(float)
    volume = minutes["volume"].astype(float)
    candle_range = (high - low).clip(lower=0.0)
    safe_range = candle_range.replace(0.0, np.nan)
    lower_wick = (np.minimum(open_price, close) - low).clip(lower=0.0)
    upper_wick = (high - np.maximum(open_price, close)).clip(lower=0.0)
    range_bps = candle_range / open_price.replace(0.0, np.nan) * 10_000.0
    volume_threshold = (
        volume.shift(1)
        .rolling(FEATURE_HISTORY_MIN, min_periods=FEATURE_MIN_PERIODS)
        .quantile(FEATURE_QUANTILE)
    )
    range_threshold = (
        range_bps.shift(1)
        .rolling(FEATURE_HISTORY_MIN, min_periods=FEATURE_MIN_PERIODS)
        .quantile(FEATURE_QUANTILE)
    )
    return pd.DataFrame(
        {
            "is_up_candle": close.gt(open_price),
            "is_down_candle": close.lt(open_price),
            "body_fraction": (close - open_price).abs() / safe_range,
            "lower_wick_fraction": lower_wick / safe_range,
            "upper_wick_fraction": upper_wick / safe_range,
            "close_location": (close - low) / safe_range,
            "volume": volume,
            "range_bps": range_bps,
            "volume_threshold": volume_threshold,
            "range_threshold": range_threshold,
            "high_volume": volume.ge(volume_threshold),
            "high_range": range_bps.ge(range_threshold),
        },
        index=minutes.index,
    )


def directional_confirmation(
    up: pd.Series,
    down: pd.Series,
    features: pd.DataFrame,
    *,
    trend_continuation: bool,
) -> pd.Series:
    if trend_continuation:
        up_confirm = (
            features["is_up_candle"]
            & features["body_fraction"].ge(0.50)
            & features["close_location"].ge(0.70)
        )
        down_confirm = (
            features["is_down_candle"]
            & features["body_fraction"].ge(0.50)
            & features["close_location"].le(0.30)
        )
    else:
        up_confirm = (
            features["is_up_candle"]
            & features["lower_wick_fraction"].ge(0.35)
            & features["close_location"].ge(0.60)
        )
        down_confirm = (
            features["is_down_candle"]
            & features["upper_wick_fraction"].ge(0.35)
            & features["close_location"].le(0.40)
        )
    return (up & up_confirm) | (down & down_confirm)


def gate_mask(
    gate: str,
    up: pd.Series,
    down: pd.Series,
    features: pd.DataFrame,
    *,
    base_family: str,
) -> pd.Series:
    base = up | down
    if gate == "ungated_control":
        return base
    directional = directional_confirmation(
        up,
        down,
        features,
        trend_continuation=base_family == "trend_continuation",
    )
    if gate == "directional_candle":
        return base & directional
    if gate == "volume_confirmed":
        return base & directional & features["high_volume"]
    if gate == "climactic_confirmed":
        return (
            base
            & directional
            & features["high_volume"]
            & features["high_range"]
        )
    raise ValueError(gate)


def _outcome_arrays(
    opens: np.ndarray,
    positions: np.ndarray,
    direction: np.ndarray,
    horizon: int,
    delay: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    entry_position = positions + 1 + delay
    settle_position = entry_position + horizon
    signed = (
        (opens[settle_position] / opens[entry_position] - 1.0)
        * 10_000.0
        * direction
    )
    status = np.where(signed > 0.0, "won", np.where(signed < 0.0, "lost", "tie"))
    pnl = np.where(
        signed > 0.0,
        AMOUNT_U * PAYOUT_RATE,
        np.where(signed < 0.0, -AMOUNT_U, 0.0),
    )
    return entry_position, settle_position, status, pnl


def generate_candidates(
    minutes: pd.DataFrame,
    volatility: pd.DataFrame,
    features: pd.DataFrame,
) -> pd.DataFrame:
    boundary = pd.Series(_boundary_mask(minutes.index), index=minutes.index)
    known_volatility = volatility["vol_state"].isin(VOL_CELLS)
    opens = minutes["open"].to_numpy(float)
    parts: list[pd.DataFrame] = []
    base_by_name = {profile.name: profile for profile in BASE_PROFILES}
    base_signals: dict[str, tuple[pd.Series, pd.Series]] = {}
    for base_profile in BASE_PROFILES:
        up, down, _ = _signal_masks(minutes, base_profile)
        base_signals[base_profile.name] = (up, down)
    for profile in PROFILES:
        base_profile = base_by_name[profile.base_name]
        up, down = base_signals[base_profile.name]
        selected = (
            boundary
            & known_volatility
            & gate_mask(
                profile.gate,
                up,
                down,
                features,
                base_family=profile.base_family,
            )
        )
        positions = np.flatnonzero(selected.to_numpy(bool))
        positions = positions[positions + 22 < len(minutes)]
        if not len(positions):
            continue
        up_at = up.iloc[positions].to_numpy(bool)
        direction = np.where(up_at, 1.0, -1.0)
        part = pd.DataFrame(
            {
                "profile": profile.name,
                "family": profile.family,
                "base_family": profile.base_family,
                "gate": profile.gate,
                "cell": volatility["vol_state"].iloc[positions].astype(str).to_numpy(),
                "vol_state": volatility["vol_state"].iloc[positions].astype(str).to_numpy(),
                "structure_state": "ohlcv",
                "signal_bar_time": minutes.index[positions],
                "signal_time": minutes.index[positions] + pd.Timedelta(minutes=1),
                "signal": np.where(up_at, "UP", "DOWN"),
                "volume": features["volume"].iloc[positions].to_numpy(float),
                "volume_threshold": features["volume_threshold"].iloc[positions].to_numpy(float),
                "range_bps": features["range_bps"].iloc[positions].to_numpy(float),
                "range_threshold": features["range_threshold"].iloc[positions].to_numpy(float),
                "body_fraction": features["body_fraction"].iloc[positions].to_numpy(float),
                "lower_wick_fraction": features["lower_wick_fraction"].iloc[positions].to_numpy(float),
                "upper_wick_fraction": features["upper_wick_fraction"].iloc[positions].to_numpy(float),
                "close_location": features["close_location"].iloc[positions].to_numpy(float),
            }
        )
        for horizon in HORIZONS_MIN:
            for delay in (0, 1):
                entry, settle, status, pnl = _outcome_arrays(
                    opens, positions, direction, horizon, delay
                )
                suffix = f"h{horizon}_d{delay}"
                part[f"status_{suffix}"] = status
                part[f"pnl_u_{suffix}"] = pnl
                if suffix == "h20_d1":
                    part["entry_time_h20_d1"] = minutes.index[entry]
                    part["settle_time_h20_d1"] = minutes.index[settle]
        fixed_entry = positions + 2
        fixed_settle = positions + 11
        fixed_signed = (
            (opens[fixed_settle] / opens[fixed_entry] - 1.0)
            * 10_000.0
            * direction
        )
        part["status_h10_fixed_d1"] = np.where(
            fixed_signed > 0.0,
            "won",
            np.where(fixed_signed < 0.0, "lost", "tie"),
        )
        part["pnl_u_h10_fixed_d1"] = np.where(
            fixed_signed > 0.0,
            AMOUNT_U * PAYOUT_RATE,
            np.where(fixed_signed < 0.0, -AMOUNT_U, 0.0),
        )
        parts.append(part)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def run(
    inputs: Sequence[str | Path] = (EARLY_INPUT, LATE_INPUT),
    manifests: Sequence[str | Path] = (EARLY_MANIFEST, LATE_MANIFEST),
) -> dict[str, Any]:
    minutes, frozen_inputs = load_frozen_history(inputs, manifests)
    volatility = build_volatility_states(minutes, VOLATILITY_WINDOW_MIN)
    features = build_ohlcv_features(minutes)
    candidates = generate_candidates(minutes, volatility, features)
    candidates.to_csv(OUT_CANDIDATES, index=False, encoding="utf-8-sig")
    folds = calendar_folds(minutes.index)
    audit, audit_report = fixed_profile_audit(
        candidates, folds, cells=VOL_CELLS, profiles=PROFILES
    )
    audit.to_csv(OUT_PROFILE_AUDIT, index=False, encoding="utf-8-sig")

    mode_reports: dict[str, Any] = {}
    selection_parts: list[pd.DataFrame] = []
    trade_parts: list[pd.DataFrame] = []
    for training_months in TRAINING_WINDOWS_MONTHS:
        mode = f"rolling_{training_months}m"
        report, selections, trades = run_walkforward_mode(
            candidates, folds, training_months, cells=VOL_CELLS
        )
        mode_reports[mode] = report
        selection_parts.append(selections)
        trade_parts.append(trades)
    selections_frame = pd.concat(selection_parts, ignore_index=True)
    trades_frame = pd.concat(trade_parts, ignore_index=True)
    selections_frame.to_csv(OUT_SELECTIONS, index=False, encoding="utf-8-sig")
    trades_frame.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")

    passed_modes = [mode for mode, value in mode_reports.items() if value["passed"]]
    platform_passed = len(passed_modes) == len(TRAINING_WINDOWS_MONTHS)
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "status": "V34_FULL_HISTORY_CAUSAL_OHLCV_ABSORPTION_ROUTER",
        "safety": {
            "researchOnly": True,
            "tradeEnabled": False,
            "realTradingAllowed": False,
            "deploymentPerformed": False,
            "onlineConfigurationChanged": False,
            "frontendChanged": False,
            "secondDataDownloaded": False,
        },
        "evidenceBoundary": (
            "OHLCV confirmations were designed after earlier full-history studies; "
            "a pass would remain a non-pristine historical candidate"
        ),
        "data": {
            "frozenInputs": frozen_inputs,
            "combinedRows": int(len(minutes)),
            "start": minutes.index[0],
            "end": minutes.index[-1],
            "candidateRows": int(len(candidates)),
        },
        "design": {
            "volatilityWindowMin": VOLATILITY_WINDOW_MIN,
            "cells": list(VOL_CELLS),
            "featureHistoryMin": FEATURE_HISTORY_MIN,
            "featureMinPeriods": FEATURE_MIN_PERIODS,
            "featureQuantile": FEATURE_QUANTILE,
            "gates": list(GATES),
            "profileCount": len(PROFILES),
            "profiles": [
                {
                    "name": item.name,
                    "family": item.family,
                    "baseName": item.base_name,
                    "baseFamily": item.base_family,
                    "gate": item.gate,
                    "lookbackMin": item.lookback_min,
                    "threshold": item.threshold,
                }
                for item in PROFILES
            ],
            "trainingWindowsMonths": list(TRAINING_WINDOWS_MONTHS),
            "executionSpecs": list(EXECUTION_SPECS),
            "mainExecutions": list(MAIN_EXECUTIONS),
            "neighborExecutions": list(NEIGHBOR_EXECUTIONS),
            "selectionAndFinalGates": "identical to V32/V33",
            "monthBoundaryPolicy": "purge all cross-month outcomes and reset cooldown",
        },
        "candidateCounts": {
            "byGate": {
                str(key): int(value)
                for key, value in candidates["gate"].value_counts().items()
            },
            "byBaseFamily": {
                str(key): int(value)
                for key, value in candidates["base_family"].value_counts().items()
            },
            "byVolatility": {
                str(key): int(value)
                for key, value in candidates["cell"].value_counts().items()
            },
        },
        "descriptiveFixedProfileAudit": audit_report,
        "causalWalkForward": mode_reports,
        "decision": {
            "passedModes": passed_modes,
            "requiredModes": [
                f"rolling_{value}m" for value in TRAINING_WINDOWS_MONTHS
            ],
            "platformPassed": platform_passed,
            "action": "research_candidate_only" if platform_passed else "no_trade",
            "volatilityWindowSensitivityEligible": platform_passed,
            "secondExecutionEligible": platform_passed,
            "deployment": "none",
            "realTradingAllowed": False,
        },
        "outputs": {
            "json": str(OUT_JSON.resolve()),
            "candidates": str(OUT_CANDIDATES.resolve()),
            "profileAudit": str(OUT_PROFILE_AUDIT.resolve()),
            "selections": str(OUT_SELECTIONS.resolve()),
            "trades": str(OUT_TRADES.resolve()),
        },
    }
    OUT_JSON.write_text(
        json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def _compact_console(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": report["status"],
        "data": report["data"],
        "candidateCounts": report["candidateCounts"],
        "fixedAudit": report["descriptiveFixedProfileAudit"],
        "modes": {
            mode: {
                "selectionCounts": value["selectionCounts"],
                "overall": value["overall"],
                "passed": value["passed"],
                "failureReasons": value["failureReasons"],
            }
            for mode, value in report["causalWalkForward"].items()
        },
        "decision": report["decision"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--early-input", default=str(EARLY_INPUT))
    parser.add_argument("--early-manifest", default=str(EARLY_MANIFEST))
    parser.add_argument("--late-input", default=str(LATE_INPUT))
    parser.add_argument("--late-manifest", default=str(LATE_MANIFEST))
    args = parser.parse_args()
    report = run(
        (args.early_input, args.late_input),
        (args.early_manifest, args.late_manifest),
    )
    print(json.dumps(clean(_compact_console(report)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
