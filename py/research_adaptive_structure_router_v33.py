"""V33 causal adaptive structure router on the frozen full minute history.

The trading profiles and all walk-forward gates are inherited unchanged from
V32.  Only the structure labels change: absolute V19 thresholds are replaced by
causal trailing standardisation and trailing score quantiles.
"""

from __future__ import annotations

import argparse
import json
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
    CELLS,
    EXECUTION_SPECS,
    MAIN_EXECUTIONS,
    NEIGHBOR_EXECUTIONS,
    PROFILES,
    TRAINING_WINDOWS_MONTHS,
    fixed_profile_audit,
    run_walkforward_mode,
)
from research_minute_volatility_normal_v15 import clean
from research_stationarity_router_v19 import (
    VOLATILITY_WINDOW_MIN,
    generate_candidates,
)
from research_volatility_window_sensitivity_v17 import build_volatility_states
from stationarity_features_v19 import (
    ESTIMATION_WINDOW_MIN,
    build_stationarity_features,
)


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "tmp" / "v33_adaptive_structure_router_20260730.json"
OUT_CANDIDATES = ROOT / "tmp" / "v33_adaptive_structure_router_candidates_20260730.csv"
OUT_PROFILE_AUDIT = ROOT / "tmp" / "v33_adaptive_structure_router_profile_audit_20260730.csv"
OUT_SELECTIONS = ROOT / "tmp" / "v33_adaptive_structure_router_selections_20260730.csv"
OUT_TRADES = ROOT / "tmp" / "v33_adaptive_structure_router_trades_20260730.csv"

NORMALIZATION_HISTORY_MIN = 30 * 24 * 60
NORMALIZATION_MIN_PERIODS = 7 * 24 * 60
STRUCTURE_QUANTILE = 0.80
SHOCK_QUANTILE = 0.95
ZSCORE_CLIP = 5.0


def causal_trailing_zscore(
    series: pd.Series,
    *,
    history_min: int = NORMALIZATION_HISTORY_MIN,
    min_periods: int = NORMALIZATION_MIN_PERIODS,
) -> pd.Series:
    history = series.shift(1)
    center = history.rolling(history_min, min_periods=min_periods).mean()
    scale = history.rolling(history_min, min_periods=min_periods).std(ddof=0)
    return ((series - center) / scale.replace(0.0, np.nan)).clip(
        -ZSCORE_CLIP, ZSCORE_CLIP
    )


def causal_trailing_quantile(
    series: pd.Series,
    quantile: float,
    *,
    history_min: int = NORMALIZATION_HISTORY_MIN,
    min_periods: int = NORMALIZATION_MIN_PERIODS,
) -> pd.Series:
    return (
        series.shift(1)
        .rolling(history_min, min_periods=min_periods)
        .quantile(quantile)
    )


def build_adaptive_structure_features(
    minutes: pd.DataFrame,
    *,
    history_min: int = NORMALIZATION_HISTORY_MIN,
    min_periods: int = NORMALIZATION_MIN_PERIODS,
) -> pd.DataFrame:
    base = build_stationarity_features(
        minutes, estimation_window_min=ESTIMATION_WINDOW_MIN
    )
    source = {
        "efficiency": base["efficiency60"],
        "abs_momentum": base["momentum60_score"].abs(),
        "variance_ratio": base["variance_ratio10"],
        "adf": base["adf_t_beta"],
        "shock": base["shock_max10"],
    }
    z = pd.DataFrame(
        {
            key: causal_trailing_zscore(
                value, history_min=history_min, min_periods=min_periods
            )
            for key, value in source.items()
        },
        index=minutes.index,
    )
    mr_score = (
        -z["efficiency"]
        - z["abs_momentum"]
        - z["variance_ratio"]
        - z["adf"]
        - z["shock"]
    ) / 5.0
    trend_score = (
        z["efficiency"]
        + z["abs_momentum"]
        + z["variance_ratio"]
        + z["adf"]
        - z["shock"]
    ) / 5.0
    mr_threshold = causal_trailing_quantile(
        mr_score,
        STRUCTURE_QUANTILE,
        history_min=history_min,
        min_periods=min_periods,
    )
    trend_threshold = causal_trailing_quantile(
        trend_score,
        STRUCTURE_QUANTILE,
        history_min=history_min,
        min_periods=min_periods,
    )
    shock_threshold = causal_trailing_quantile(
        base["shock_max10"],
        SHOCK_QUANTILE,
        history_min=history_min,
        min_periods=min_periods,
    )
    ready = pd.concat(
        [z, mr_score, trend_score, mr_threshold, trend_threshold, shock_threshold],
        axis=1,
    ).notna().all(axis=1)
    extreme_shock = ready & base["shock_max10"].ge(shock_threshold)
    mr_high = ready & mr_score.ge(mr_threshold)
    trend_high = ready & trend_score.ge(trend_threshold)
    structure = pd.Series("mixed", index=minutes.index, dtype="object")
    structure.loc[~ready] = "unknown"
    structure.loc[extreme_shock] = "shock"
    structure.loc[ready & ~extreme_shock & mr_high & ~trend_high] = "revertible"
    structure.loc[ready & ~extreme_shock & trend_high & ~mr_high] = "trend"

    result = base.copy()
    result["adaptive_mr_score"] = mr_score
    result["adaptive_trend_score"] = trend_score
    result["adaptive_mr_threshold"] = mr_threshold
    result["adaptive_trend_threshold"] = trend_threshold
    result["adaptive_shock_threshold"] = shock_threshold
    result["structure_state"] = structure
    return result


def generate_all_candidates(
    minutes: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    volatility = build_volatility_states(minutes, VOLATILITY_WINDOW_MIN)
    adaptive = build_adaptive_structure_features(minutes)
    parts = [
        generate_candidates(minutes, volatility, adaptive, profile)
        for profile in PROFILES
    ]
    candidates = pd.concat(
        [part for part in parts if not part.empty], ignore_index=True
    )
    return candidates, volatility, adaptive


def run(
    inputs: Sequence[str | Path] = (EARLY_INPUT, LATE_INPUT),
    manifests: Sequence[str | Path] = (EARLY_MANIFEST, LATE_MANIFEST),
) -> dict[str, Any]:
    minutes, frozen_inputs = load_frozen_history(inputs, manifests)
    candidates, volatility, adaptive = generate_all_candidates(minutes)
    candidates.to_csv(OUT_CANDIDATES, index=False, encoding="utf-8-sig")
    folds = calendar_folds(minutes.index)
    audit, audit_report = fixed_profile_audit(candidates, folds)
    audit.to_csv(OUT_PROFILE_AUDIT, index=False, encoding="utf-8-sig")

    mode_reports: dict[str, Any] = {}
    selections_all: list[pd.DataFrame] = []
    trades_all: list[pd.DataFrame] = []
    for training_months in TRAINING_WINDOWS_MONTHS:
        mode = f"rolling_{training_months}m"
        report, selections, trades = run_walkforward_mode(
            candidates, folds, training_months
        )
        mode_reports[mode] = report
        selections_all.append(selections)
        trades_all.append(trades)
    selections_frame = pd.concat(selections_all, ignore_index=True)
    trades_frame = pd.concat(trades_all, ignore_index=True)
    selections_frame.to_csv(OUT_SELECTIONS, index=False, encoding="utf-8-sig")
    trades_frame.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")

    passed_modes = [mode for mode, value in mode_reports.items() if value["passed"]]
    platform_passed = len(passed_modes) == len(TRAINING_WINDOWS_MONTHS)
    structure_counts = adaptive["structure_state"].value_counts()
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "status": "V33_FULL_HISTORY_CAUSAL_ADAPTIVE_STRUCTURE_ROUTER",
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
            "adaptive structure design followed inspection of V32 full-history results; "
            "a pass is not pristine holdout evidence"
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
            "stationarityEstimationWindowMin": ESTIMATION_WINDOW_MIN,
            "normalizationHistoryMin": NORMALIZATION_HISTORY_MIN,
            "normalizationMinPeriods": NORMALIZATION_MIN_PERIODS,
            "structureQuantile": STRUCTURE_QUANTILE,
            "shockQuantile": SHOCK_QUANTILE,
            "zscoreClip": ZSCORE_CLIP,
            "scoreWeights": "five equal-weight causal z-scores",
            "profiles": [
                {
                    "name": item.name,
                    "family": item.family,
                    "lookbackMin": item.lookback_min,
                    "threshold": item.threshold,
                }
                for item in PROFILES
            ],
            "cells": list(CELLS),
            "trainingWindowsMonths": list(TRAINING_WINDOWS_MONTHS),
            "executionSpecs": list(EXECUTION_SPECS),
            "mainExecutions": list(MAIN_EXECUTIONS),
            "neighborExecutions": list(NEIGHBOR_EXECUTIONS),
            "selectionAndFinalGates": "identical to V32",
            "monthBoundaryPolicy": "purge all cross-month outcomes and reset cooldown",
        },
        "stateOccupancyPct": {
            str(key): round(100.0 * int(value) / len(adaptive), 4)
            for key, value in structure_counts.items()
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
        "stateOccupancyPct": report["stateOccupancyPct"],
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
