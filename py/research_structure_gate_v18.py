"""Causal two-axis volatility plus market-structure gate research.

V18 keeps the V17 normal/reversal actions deliberately small and adds a
second, causal state axis: trend efficiency, variance ratio, centre crossings,
and short/long volatility shock.  Gate thresholds are trailing quantiles.  The
script reports both a strict walk-forward router and a looser exploratory
router, but never enables deployment.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research_multiregime_strategy_v16 import (
    BREAKEVEN_WR,
    FOLDS,
    PRIMARY_FOLD_NAMES,
    REUSED_DIAGNOSTIC_FOLD,
    STATES,
    apply_shared_cooldown,
    clean,
    load_minutes,
    mapped_test,
    metrics,
    summarize,
)
from research_volatility_window_sensitivity_v17 import (
    CANDIDATES,
    INPUT,
    build_volatility_states,
    fixed_settlement_delay_metrics,
    load_candidates,
    remap_states,
)


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "tmp" / "v18_structure_gate_20260730.json"
OUT_VARIANTS = ROOT / "tmp" / "v18_structure_gate_variants_20260730.csv"
OUT_STRICT_TRADES = ROOT / "tmp" / "v18_structure_gate_strict_trades_20260730.csv"
OUT_EXPLORATORY_TRADES = ROOT / "tmp" / "v18_structure_gate_exploratory_trades_20260730.csv"

VOLATILITY_WINDOW_MIN = 120
QUANTILE_HISTORY_MIN = 7 * 24 * 60
QUANTILE_MIN_PERIODS = 3 * 24 * 60
GATES = ("none", "not_trending", "choppy", "mr_signature", "balanced_mr", "stable_mr")
RETROSPECTIVE_PERIODS = (
    ("2026-02_retrospective", pd.Timestamp("2026-02-01T00:00:00Z"), pd.Timestamp("2026-03-01T00:00:00Z")),
    ("2026-03_retrospective", pd.Timestamp("2026-03-01T00:00:00Z"), pd.Timestamp("2026-04-01T00:00:00Z")),
    ("2026-07_reused", pd.Timestamp("2026-07-01T00:00:00Z"), pd.Timestamp("2026-07-30T00:00:00Z")),
)


@dataclass(frozen=True)
class StateAction:
    state: str
    profile: str
    research_role: str


STATE_ACTIONS = (
    StateAction("low", "normal_edge_w60_z2p5", "direct_reversion"),
    StateAction("low", "normal_reclaim_w120_z2p0", "confirmed_reversal"),
    StateAction("mid", "normal_edge_w10_z2p0", "direct_reversion"),
    StateAction("mid", "normal_reclaim_w120_z2p5", "confirmed_reversal"),
    StateAction("high", "normal_reclaim_w30_z2p5", "confirmed_reversal"),
    StateAction("high", "exhaustion_w120_s1p5", "exhaustion_reversal"),
)


def _causal_quantile(series: pd.Series, quantile: float) -> pd.Series:
    return series.shift(1).rolling(
        QUANTILE_HISTORY_MIN,
        min_periods=QUANTILE_MIN_PERIODS,
    ).quantile(quantile)


def build_structure_features(minutes: pd.DataFrame) -> pd.DataFrame:
    close = minutes["close"].astype(float)
    log_close = np.log(close)
    ret1 = log_close.diff()

    move60 = (log_close - log_close.shift(60)).abs()
    path60 = ret1.abs().rolling(60, min_periods=60).sum()
    efficiency60 = move60 / path60.replace(0.0, np.nan)

    sigma120 = ret1.rolling(120, min_periods=120).std(ddof=0)
    trend_score120 = (log_close - log_close.shift(120)).abs() / (
        sigma120 * math.sqrt(120)
    ).replace(0.0, np.nan)

    ret5 = log_close - log_close.shift(5)
    variance_ratio5 = ret5.rolling(120, min_periods=120).var(ddof=0) / (
        5.0 * ret1.rolling(120, min_periods=120).var(ddof=0)
    ).replace(0.0, np.nan)

    prior_center60 = close.shift(1).rolling(60, min_periods=60).mean()
    residual = close - prior_center60
    residual_sign = np.sign(residual)
    centre_cross = residual_sign.mul(residual_sign.shift(1)).lt(0.0).astype(float)
    crossing_rate120 = centre_cross.rolling(120, min_periods=120).mean()

    sigma15 = ret1.rolling(15, min_periods=15).std(ddof=0)
    volatility_shock = sigma15 / sigma120.replace(0.0, np.nan)

    frame = pd.DataFrame(
        {
            "efficiency60": efficiency60,
            "trend_score120": trend_score120,
            "variance_ratio5": variance_ratio5,
            "crossing_rate120": crossing_rate120,
            "volatility_shock": volatility_shock,
        },
        index=minutes.index,
    )
    quantile_requests = {
        "efficiency60": (1.0 / 3.0, 0.5, 2.0 / 3.0),
        "trend_score120": (0.5, 2.0 / 3.0),
        "variance_ratio5": (1.0 / 3.0, 0.5),
        "crossing_rate120": (0.5, 2.0 / 3.0),
        "volatility_shock": (2.0 / 3.0,),
    }
    labels = {1.0 / 3.0: "q33", 0.5: "q50", 2.0 / 3.0: "q67"}
    for column, quantiles in quantile_requests.items():
        for quantile in quantiles:
            frame[f"{column}_{labels[quantile]}"] = _causal_quantile(
                frame[column], quantile
            )
    return frame


def structure_gate_masks(features: pd.DataFrame) -> dict[str, pd.Series]:
    ready = features[
        [
            "efficiency60_q67",
            "trend_score120_q67",
            "variance_ratio5_q50",
            "crossing_rate120_q50",
            "volatility_shock_q67",
        ]
    ].notna().all(axis=1)
    not_trending = (
        features["efficiency60"].le(features["efficiency60_q67"])
        & features["trend_score120"].le(features["trend_score120_q67"])
        & features["volatility_shock"].le(features["volatility_shock_q67"])
    )
    choppy = (
        features["efficiency60"].le(features["efficiency60_q33"])
        & features["trend_score120"].le(features["trend_score120_q50"])
    )
    mr_signature = (
        features["variance_ratio5"].le(features["variance_ratio5_q33"])
        & features["crossing_rate120"].ge(features["crossing_rate120_q67"])
    )
    balanced_mr = (
        features["efficiency60"].le(features["efficiency60_q50"])
        & features["variance_ratio5"].le(features["variance_ratio5_q50"])
        & features["crossing_rate120"].ge(features["crossing_rate120_q50"])
        & features["volatility_shock"].le(features["volatility_shock_q67"])
    )
    return {
        "none": ready,
        "not_trending": ready & not_trending,
        "choppy": ready & choppy,
        "mr_signature": ready & mr_signature,
        "balanced_mr": ready & balanced_mr,
        "stable_mr": ready & choppy & mr_signature
        & features["volatility_shock"].le(features["volatility_shock_q67"]),
    }


def attach_structure(
    candidates: pd.DataFrame,
    features: pd.DataFrame,
) -> pd.DataFrame:
    positions = pd.DatetimeIndex(candidates["signal_bar_time"])
    mapped = features.reindex(positions)
    frame = candidates.copy()
    for column in mapped.columns:
        frame[column] = mapped[column].to_numpy()
    masks = structure_gate_masks(mapped.reset_index(drop=True))
    for gate, mask in masks.items():
        frame[f"gate_{gate}"] = mask.to_numpy(bool)
    return frame


def generate_variants(candidates: pd.DataFrame) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for action in STATE_ACTIONS:
        base = candidates.loc[
            candidates["vol_state"].eq(action.state)
            & candidates["profile"].eq(action.profile)
        ]
        for gate in GATES:
            part = base.loc[base[f"gate_{gate}"]].copy()
            if part.empty:
                continue
            part["state_action"] = action.research_role
            part["structure_gate"] = gate
            part["variant"] = f"{action.state}|{action.profile}|{gate}"
            parts.append(part)
    if not parts:
        return pd.DataFrame(columns=[*candidates.columns, "state_action", "structure_gate", "variant"])
    return pd.concat(parts, ignore_index=True).sort_values(
        ["signal_time", "variant"], kind="stable"
    ).reset_index(drop=True)


def metric_triplet(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
    return {
        "exactBoundary": metrics(frame, 10, 0),
        "oneMinuteShiftedHorizon": metrics(frame, 10, 1),
        "oneMinuteLateFixedSettlement": fixed_settlement_delay_metrics(frame),
    }


def _minimum_training_months(train_end: pd.Timestamp) -> int:
    if train_end <= pd.Timestamp("2026-04-01T00:00:00Z"):
        return 2
    if train_end <= pd.Timestamp("2026-05-01T00:00:00Z"):
        return 3
    if train_end <= pd.Timestamp("2026-06-01T00:00:00Z"):
        return 3
    return 4


def _eligible(
    summaries: dict[str, dict[str, Any]],
    *,
    tier: str,
    minimum_months: int,
) -> bool:
    rows = list(summaries.values())
    if tier == "strict":
        return all(
            row["trades"] >= 60
            and row["months"] >= minimum_months
            and row["pnlU"] > 0.0
            and row["winRatePct"] is not None
            and row["winRatePct"] > BREAKEVEN_WR
            and row["positiveMonthPct"] is not None
            and row["positiveMonthPct"] >= (200.0 / 3.0)
            and row["worstMonthPnlU"] is not None
            and row["worstMonthPnlU"] >= -25.0
            for row in rows
        )
    if tier == "exploratory":
        return all(
            row["trades"] >= 40
            and row["months"] >= minimum_months
            and row["pnlU"] > 0.0
            and row["winRatePct"] is not None
            and row["winRatePct"] > BREAKEVEN_WR
            and row["positiveMonthPct"] is not None
            and row["positiveMonthPct"] >= 50.0
            and row["worstMonthPnlU"] is not None
            and row["worstMonthPnlU"] >= -50.0
            for row in rows
        )
    raise ValueError(tier)


def select_variant(
    variants: pd.DataFrame,
    state: str,
    train_end: pd.Timestamp,
    *,
    tier: str,
) -> dict[str, Any] | None:
    if variants.empty:
        return None
    settlement_columns = ("settle_time_h10_d0", "settle_time_h10_d1")
    known = pd.Series(True, index=variants.index)
    for column in settlement_columns:
        known &= pd.to_datetime(variants[column], utc=True, errors="coerce").lt(train_end)
    pool = variants.loc[variants["vol_state"].eq(state) & known]
    minimum_months = _minimum_training_months(train_end)
    ranked: list[tuple[tuple[float, ...], str, dict[str, Any], str, str]] = []
    for variant, raw_group in pool.groupby("variant", sort=True):
        group = apply_shared_cooldown(raw_group)
        summaries = metric_triplet(group)
        if not _eligible(
            summaries, tier=tier, minimum_months=minimum_months
        ):
            continue
        rows = list(summaries.values())
        score = (
            min(float(row["positiveMonthPct"]) for row in rows),
            min(float(row["medianMonthPnlU"]) for row in rows),
            min(float(row["wilson95LowerPct"] or 0.0) for row in rows),
            -max(float(row["maxDrawdownU"]) for row in rows),
            -max(float(row["trades"]) for row in rows),
        )
        ranked.append(
            (
                score,
                str(variant),
                summaries,
                str(group["state_action"].iloc[0]),
                str(group["structure_gate"].iloc[0]),
            )
        )
    if not ranked:
        return None
    ranked.sort(reverse=True)
    score, variant, summaries, action, gate = ranked[0]
    same_action_support = sum(
        item[3] == action for item in ranked
    )
    if tier == "strict" and same_action_support < 2:
        return None
    return {
        "variant": variant,
        "action": action,
        "gate": gate,
        "supportingEligibleVariantsSameAction": int(same_action_support),
        "train": summaries,
        "score": list(score),
    }


def mapped_variant_test(
    variants: pd.DataFrame,
    mapping: dict[str, str | None],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    parts = []
    for state, variant in mapping.items():
        if variant is None:
            continue
        parts.append(
            variants.loc[
                variants["vol_state"].eq(state)
                & variants["variant"].eq(variant)
                & variants["signal_time"].ge(start)
                & variants["signal_time"].lt(end)
            ]
        )
    frame = (
        pd.concat(parts, ignore_index=True)
        if parts
        else pd.DataFrame(columns=variants.columns)
    )
    return apply_shared_cooldown(frame)


def summarize_with_execution(
    frame: pd.DataFrame,
    *,
    period_start: pd.Timestamp | None = None,
    period_end: pd.Timestamp | None = None,
) -> dict[str, Any]:
    base = summarize(frame, period_start=period_start, period_end=period_end)
    base["oneMinuteLateFixedSettlement"] = fixed_settlement_delay_metrics(
        frame, period_start=period_start, period_end=period_end
    )
    return base


def walk_forward(
    variants: pd.DataFrame,
    *,
    tier: str,
) -> tuple[list[dict[str, Any]], pd.DataFrame]:
    reports: list[dict[str, Any]] = []
    trade_parts: list[pd.DataFrame] = []
    for name, start, end in FOLDS:
        selection = {
            state: select_variant(variants, state, start, tier=tier)
            for state in STATES
        }
        mapping = {
            state: item["variant"] if item is not None else None
            for state, item in selection.items()
        }
        trades = mapped_variant_test(variants, mapping, start, end)
        if not trades.empty:
            tagged = trades.copy()
            tagged["fold"] = name
            trade_parts.append(tagged)
        reports.append(
            {
                "name": name,
                "selection": selection,
                "mapping": mapping,
                "test": summarize_with_execution(
                    trades, period_start=start, period_end=end
                ),
            }
        )
    all_trades = (
        pd.concat(trade_parts, ignore_index=True)
        if trade_parts
        else pd.DataFrame(columns=[*variants.columns, "fold"])
    )
    return reports, all_trades


def route_decisions(
    reports: list[dict[str, Any]],
    trades: pd.DataFrame,
) -> dict[str, Any]:
    primary = (
        trades.loc[trades["fold"].isin(PRIMARY_FOLD_NAMES)]
        if not trades.empty
        else trades
    )
    output: dict[str, Any] = {}
    for state in STATES:
        part = (
            primary.loc[primary["vol_state"].eq(state)]
            if not primary.empty
            else primary
        )
        triplet = metric_triplet(part)
        mappings = {
            report["name"]: report["mapping"].get(state)
            for report in reports
            if report["name"] in PRIMARY_FOLD_NAMES
        }
        fold_pnl: dict[str, dict[str, float]] = {}
        for fold in PRIMARY_FOLD_NAMES:
            fold_part = (
                part.loc[part["fold"].eq(fold)] if not part.empty else part
            )
            fold_metrics = metric_triplet(fold_part)
            fold_pnl[fold] = {
                key: float(value["pnlU"])
                for key, value in fold_metrics.items()
            }
        coverage = 100.0 * sum(
            value is not None for value in mappings.values()
        ) / len(PRIMARY_FOLD_NAMES)
        positive_folds = {
            key: sum(row[key] > 0.0 for row in fold_pnl.values())
            for key in triplet
        }
        rows = list(triplet.values())
        passed = bool(
            coverage == 100.0
            and all(row["trades"] >= 90 for row in rows)
            and all(row["winRatePct"] is not None and row["winRatePct"] > BREAKEVEN_WR for row in rows)
            and all(
                row["wilson95LowerPct"] is not None
                and row["wilson95LowerPct"] > BREAKEVEN_WR
                for row in rows
            )
            and all(value >= 2 for value in positive_folds.values())
        )
        output[state] = {
            "mappings": mappings,
            "selectionCoveragePct": round(coverage, 4),
            "metrics": triplet,
            "foldPnlU": fold_pnl,
            "positiveFolds": positive_folds,
            "passed": passed,
            "decision": "research_candidate_only" if passed else "no_trade",
        }
    return output


def fixed_variant_rows(variants: pd.DataFrame) -> list[dict[str, Any]]:
    start = pd.Timestamp("2026-04-01T00:00:00Z")
    end = pd.Timestamp("2026-07-01T00:00:00Z")
    rows: list[dict[str, Any]] = []
    for variant, raw_group in variants.groupby("variant", sort=True):
        group = apply_shared_cooldown(
            raw_group.loc[
                raw_group["signal_time"].ge(start)
                & raw_group["signal_time"].lt(end)
            ]
        )
        triplet = metric_triplet(group)
        row: dict[str, Any] = {
            "variant": variant,
            "vol_state": str(raw_group["vol_state"].iloc[0]),
            "profile": str(raw_group["profile"].iloc[0]),
            "action": str(raw_group["state_action"].iloc[0]),
            "gate": str(raw_group["structure_gate"].iloc[0]),
        }
        for execution, summary in triplet.items():
            prefix = {
                "exactBoundary": "exact",
                "oneMinuteShiftedHorizon": "shifted",
                "oneMinuteLateFixedSettlement": "fixed",
            }[execution]
            row[f"{prefix}_trades"] = summary["trades"]
            row[f"{prefix}_win_rate_pct"] = summary["winRatePct"]
            row[f"{prefix}_wilson95_lower_pct"] = summary["wilson95LowerPct"]
            row[f"{prefix}_pnl_u"] = summary["pnlU"]
        positive = {key: 0 for key in triplet}
        for fold_name, fold_start, fold_end in FOLDS:
            if fold_name not in PRIMARY_FOLD_NAMES:
                continue
            fold_group = group.loc[
                group["signal_time"].ge(fold_start)
                & group["signal_time"].lt(fold_end)
            ]
            fold_triplet = metric_triplet(fold_group)
            for execution, summary in fold_triplet.items():
                prefix = {
                    "exactBoundary": "exact",
                    "oneMinuteShiftedHorizon": "shifted",
                    "oneMinuteLateFixedSettlement": "fixed",
                }[execution]
                row[f"{fold_name}_{prefix}_pnl_u"] = summary["pnlU"]
                positive[execution] += int(summary["pnlU"] > 0.0)
        row["minimum_positive_folds"] = min(positive.values())
        row["retrospective_candidate"] = bool(
            all(summary["trades"] >= 90 for summary in triplet.values())
            and all(
                summary["winRatePct"] is not None
                and summary["winRatePct"] > BREAKEVEN_WR
                for summary in triplet.values()
            )
            and min(positive.values()) == len(PRIMARY_FOLD_NAMES)
        )
        row["strict_pass"] = bool(
            row["retrospective_candidate"]
            and all(
                summary["wilson95LowerPct"] is not None
                and summary["wilson95LowerPct"] > BREAKEVEN_WR
                for summary in triplet.values()
            )
        )
        row["near_candidate"] = bool(
            all(summary["trades"] >= 15 for summary in triplet.values())
            and all(summary["pnlU"] > 0.0 for summary in triplet.values())
            and min(positive.values()) >= 2
        )
        for period_name, period_start, period_end in RETROSPECTIVE_PERIODS:
            period_group = apply_shared_cooldown(
                raw_group.loc[
                    raw_group["signal_time"].ge(period_start)
                    & raw_group["signal_time"].lt(period_end)
                ]
            )
            period_triplet = metric_triplet(period_group)
            for execution, summary in period_triplet.items():
                prefix = {
                    "exactBoundary": "exact",
                    "oneMinuteShiftedHorizon": "shifted",
                    "oneMinuteLateFixedSettlement": "fixed",
                }[execution]
                row[f"{period_name}_{prefix}_trades"] = summary["trades"]
                row[f"{period_name}_{prefix}_pnl_u"] = summary["pnlU"]
        rows.append(row)
    return rows


def run(input_path: str | Path, candidate_path: str | Path) -> dict[str, Any]:
    minutes = load_minutes(input_path)
    volatility = build_volatility_states(minutes, VOLATILITY_WINDOW_MIN)
    base_candidates = remap_states(load_candidates(candidate_path), volatility)
    structure = build_structure_features(minutes)
    attached = attach_structure(base_candidates, structure)
    variants = generate_variants(attached)

    strict_reports, strict_trades = walk_forward(variants, tier="strict")
    exploratory_reports, exploratory_trades = walk_forward(
        variants, tier="exploratory"
    )
    stability_rows = fixed_variant_rows(variants)
    stability = pd.DataFrame(stability_rows)
    stability.to_csv(OUT_VARIANTS, index=False, encoding="utf-8-sig")
    strict_trades.to_csv(OUT_STRICT_TRADES, index=False, encoding="utf-8-sig")
    exploratory_trades.to_csv(
        OUT_EXPLORATORY_TRADES, index=False, encoding="utf-8-sig"
    )

    strict_decisions = route_decisions(strict_reports, strict_trades)
    exploratory_decisions = route_decisions(
        exploratory_reports, exploratory_trades
    )
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "status": "V18_CAUSAL_STRUCTURE_GATE_WALKFORWARD",
        "safety": {
            "researchOnly": True,
            "tradeEnabled": False,
            "deploymentPerformed": False,
            "realTradingAllowed": False,
        },
        "data": {
            "minutes": str(Path(input_path).resolve()),
            "candidates": str(Path(candidate_path).resolve()),
            "minuteRows": int(len(minutes)),
            "baseCandidateRows": int(len(base_candidates)),
            "variantRows": int(len(variants)),
            "warning": "All calendar periods have already been inspected; V18 is exploratory, not sealed holdout evidence.",
        },
        "design": {
            "volatilityWindowMin": VOLATILITY_WINDOW_MIN,
            "states": list(STATES),
            "actions": [action.__dict__ for action in STATE_ACTIONS],
            "structureGates": list(GATES),
            "features": [
                "60m trend efficiency",
                "120m drift-to-noise",
                "5m/1m variance ratio over 120m",
                "120m rolling-centre crossing rate",
                "15m/120m volatility shock",
            ],
            "thresholds": "prior 7-day causal q33/q50/q67",
            "primaryHorizonMin": 10,
            "diagnosticHorizonsMin": [5, 20],
            "executionViews": [
                "exact next-minute boundary then 10m",
                "one-minute shifted entry then 10m",
                "one-minute late entry with original fixed settlement",
            ],
            "primaryFolds": list(PRIMARY_FOLD_NAMES),
            "july": "reused diagnostic only",
        },
        "strict": {
            "folds": strict_reports,
            "decisions": strict_decisions,
            "allStatesPassed": all(
                row["passed"] for row in strict_decisions.values()
            ),
        },
        "exploratory": {
            "folds": exploratory_reports,
            "decisions": exploratory_decisions,
            "allStatesPassed": all(
                row["passed"] for row in exploratory_decisions.values()
            ),
        },
        "fixedVariantAudit": {
            "variantCount": int(len(stability)),
            "nearCandidates": stability.loc[stability["near_candidate"]][
                "variant"
            ].astype(str).tolist()
            if not stability.empty
            else [],
            "retrospectiveCandidates": stability.loc[
                stability["retrospective_candidate"]
            ]["variant"].astype(str).tolist()
            if not stability.empty
            else [],
            "strictPassVariants": stability.loc[stability["strict_pass"]][
                "variant"
            ].astype(str).tolist()
            if not stability.empty
            else [],
        },
        "decision": {
            "deployment": "none",
            "realTradingAllowed": False,
            "passedStates": [
                state
                for state, row in strict_decisions.items()
                if row["passed"]
            ],
        },
        "outputs": {
            "json": str(OUT_JSON),
            "variants": str(OUT_VARIANTS),
            "strictTrades": str(OUT_STRICT_TRADES),
            "exploratoryTrades": str(OUT_EXPLORATORY_TRADES),
        },
    }
    OUT_JSON.write_text(
        json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(INPUT))
    parser.add_argument("--candidates", default=str(CANDIDATES))
    args = parser.parse_args()
    report = run(args.input, args.candidates)
    print(
        json.dumps(
            clean(
                {
                    "strict": report["strict"],
                    "exploratory": report["exploratory"],
                    "fixedVariantAudit": report["fixedVariantAudit"],
                    "decision": report["decision"],
                    "outputs": report["outputs"],
                }
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
