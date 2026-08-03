"""V31 full-history causal volatility-regime/action-family walk-forward.

Research only.  This module joins the two frozen BTCUSDT futures 1m histories,
reuses the locked V25 action definitions, and performs strictly prior-month
rolling selection.  Descriptive full-history champions are deliberately kept
separate from causal walk-forward results.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

from research_full_regime_action_matrix_v25 import (
    BREAKEVEN_WR,
    CONFIRMED_REVERSAL,
    DELAYS_MIN,
    DIRECT_REVERSION,
    EXHAUSTION_REVERSAL,
    FAMILIES,
    HORIZONS_MIN,
    LOOKBACKS_MIN,
    NO_TRADE,
    PROFILES,
    STATES,
    THRESHOLDS,
    TREND_CONTINUATION,
    VOLATILITY_WINDOW_MIN,
    build_fixed_audit,
    calendar_folds,
    generate_candidate_matrix,
    metrics_from_signed,
    run_walkforward_mode,
    verify_frozen_input,
)
from research_minute_volatility_normal_v15 import clean, load_minutes, wilson_lower
from research_volatility_window_sensitivity_v17 import (
    VOL_HISTORY_MIN,
    VOL_HISTORY_MIN_PERIODS,
    build_volatility_states,
    volatility_summary,
)


ROOT = Path(__file__).resolve().parents[1]
EARLY_INPUT = ROOT / "data" / "btcusdt_futures_1m_20200101_20240101.csv"
EARLY_MANIFEST = (
    ROOT / "data" / "btcusdt_futures_1m_20200101_20240101.manifest.json"
)
LATE_INPUT = ROOT / "data" / "btcusdt_futures_1m_20240101_20260730.csv"
LATE_MANIFEST = (
    ROOT / "data" / "btcusdt_futures_1m_20240101_20260730.manifest.json"
)

OUT_JSON = ROOT / "tmp" / "v31_full_history_regime_walkforward_20260730.json"
OUT_AUDIT = ROOT / "tmp" / "v31_full_history_fixed_audit_20260730.csv"
OUT_MONTHLY = ROOT / "tmp" / "v31_full_history_monthly_matrix_20260730.csv"
OUT_SELECTIONS = (
    ROOT / "tmp" / "v31_full_history_walkforward_selections_20260730.csv"
)
OUT_TRADES = ROOT / "tmp" / "v31_full_history_walkforward_trades_20260730.csv"

TRAINING_WINDOWS_MONTHS = (3, 6, 12)
ALL_ACTIONS = (*FAMILIES, NO_TRADE)


def combine_minute_frames(frames: Sequence[pd.DataFrame]) -> pd.DataFrame:
    """Join already-cleaned frozen frames and reject gaps, overlap or disorder."""
    if not frames:
        raise ValueError("at least one frozen minute frame is required")
    ordered = sorted(frames, key=lambda frame: frame.index[0])
    for frame in ordered:
        if frame.empty or frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
            raise ValueError("each frozen minute frame must be non-empty, unique and sorted")
    for left, right in zip(ordered, ordered[1:]):
        expected = left.index[-1] + pd.Timedelta(minutes=1)
        if right.index[0] != expected:
            raise ValueError(
                "frozen histories must have an exact one-minute seam: "
                f"expected={expected.isoformat()}, got={right.index[0].isoformat()}"
            )
    combined = pd.concat(ordered, axis=0)
    if combined.index.has_duplicates:
        raise ValueError("combined frozen minute history contains duplicate minutes")
    steps = combined.index.to_series().diff().dt.total_seconds().dropna()
    if len(steps) and not steps.eq(60.0).all():
        raise ValueError("combined frozen minute history is not contiguous")
    return combined


def load_frozen_history(
    inputs: Sequence[str | Path], manifests: Sequence[str | Path]
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    if len(inputs) != len(manifests):
        raise ValueError("each frozen input must have exactly one manifest")
    frames: list[pd.DataFrame] = []
    audits: list[dict[str, Any]] = []
    for input_path, manifest_path in zip(inputs, manifests):
        frozen = verify_frozen_input(input_path, manifest_path)
        minutes = load_minutes(input_path)[
            ["open", "high", "low", "close", "volume"]
        ].copy()
        manifest_rows = frozen.get("manifestAudit", {}).get("rows")
        if manifest_rows is not None and int(manifest_rows) != len(minutes):
            raise ValueError("frozen manifest row count does not match loaded minute rows")
        frames.append(minutes)
        audits.append(
            {
                "input": str(Path(input_path).resolve()),
                "rows": int(len(minutes)),
                "start": minutes.index[0],
                "end": minutes.index[-1],
                **frozen,
            }
        )
    return combine_minute_frames(frames), audits


def validate_causal_selections(
    selections: pd.DataFrame, training_window_months: int
) -> dict[str, Any]:
    """Prove each test cell uses exactly the contiguous prior calendar months."""
    if selections.empty:
        return {
            "validatedCells": 0,
            "trainingWindowMonths": training_window_months,
            "futureMonthsObserved": 0,
            "contiguousPriorMonths": True,
        }
    violations: list[str] = []
    for row in selections.itertuples(index=False):
        test = pd.Period(str(row.month), freq="M")
        keys = [value for value in str(row.train_months).split(",") if value]
        observed = [pd.Period(value, freq="M") for value in keys]
        expected = list(
            pd.period_range(
                test - training_window_months,
                test - 1,
                freq="M",
            )
        )
        if observed != expected:
            violations.append(
                f"{row.vol_state}|{test}: observed={keys}, "
                f"expected={[str(value) for value in expected]}"
            )
    if violations:
        raise ValueError("non-causal walk-forward selection: " + violations[0])
    return {
        "validatedCells": int(len(selections)),
        "trainingWindowMonths": int(training_window_months),
        "futureMonthsObserved": 0,
        "contiguousPriorMonths": True,
    }


def _aggregate_monthly_metrics(
    frame: pd.DataFrame, calendar_months: Iterable[str]
) -> dict[str, Any]:
    months = list(calendar_months)
    if frame.empty:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "ties": 0,
            "winRatePct": None,
            "wilson95LowerPct": None,
            "pnlU": 0.0,
            "expectedValueU": None,
            "calendarMonths": len(months),
            "activeMonths": 0,
            "positiveMonthPct": 0.0 if months else None,
            "positiveActiveMonthPct": None,
            "worstMonthPnlU": 0.0 if months else None,
        }
    indexed = frame.set_index("month").reindex(months)
    numeric = {
        column: pd.to_numeric(indexed[column], errors="coerce").fillna(0.0)
        for column in ("trades", "wins", "losses", "ties", "pnlU")
    }
    trades = int(numeric["trades"].sum())
    wins = int(numeric["wins"].sum())
    losses = int(numeric["losses"].sum())
    ties = int(numeric["ties"].sum())
    decided = wins + losses
    pnl = float(numeric["pnlU"].sum())
    active = numeric["trades"].gt(0)
    lower = wilson_lower(wins, decided)
    return {
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "winRatePct": round(100.0 * wins / decided, 4) if decided else None,
        "wilson95LowerPct": round(100.0 * lower, 4) if lower is not None else None,
        "pnlU": round(pnl, 4),
        "expectedValueU": round(pnl / trades, 6) if trades else None,
        "calendarMonths": len(months),
        "activeMonths": int(active.sum()),
        "positiveMonthPct": round(float(numeric["pnlU"].gt(0.0).mean()) * 100.0, 4)
        if months
        else None,
        "positiveActiveMonthPct": round(
            float(numeric["pnlU"].loc[active].gt(0.0).mean()) * 100.0, 4
        )
        if active.any()
        else None,
        "worstMonthPnlU": round(float(numeric["pnlU"].min()), 4)
        if months
        else None,
    }


def _year_keys(folds: Sequence[tuple[str, str, pd.Timestamp, pd.Timestamp, bool]]) -> list[str]:
    return sorted({key[:4] for key, *_ in folds})


def add_full_history_stability(
    audit: pd.DataFrame,
    monthly: pd.DataFrame,
    years: Sequence[str],
) -> pd.DataFrame:
    """Add 2020-2026 annual PnL and a full-range fixed-profile gate."""
    enriched = audit.copy()
    active_monthly = monthly.loc[monthly["family"].ne(NO_TRADE)].copy()
    active_monthly["year"] = active_monthly["month"].astype(str).str[:4]
    annual = (
        active_monthly.groupby(
            ["vol_state", "profile", "horizon_min", "execution", "year"],
            observed=True,
            sort=False,
        )["pnlU"]
        .sum()
        .rename("pnlU")
        .reset_index()
    )
    for year in years:
        for execution in ("exact", "delayed"):
            name = f"y{year}_{execution}_pnlU_v31"
            part = annual.loc[
                annual["year"].eq(year) & annual["execution"].eq(execution),
                ["vol_state", "profile", "horizon_min", "pnlU"],
            ].rename(columns={"pnlU": name})
            enriched = enriched.merge(
                part,
                on=["vol_state", "profile", "horizon_min"],
                how="left",
                validate="one_to_one",
            )
            enriched[name] = enriched[name].fillna(0.0)
    annual_columns = [
        f"y{year}_{execution}_pnlU_v31"
        for year in years
        for execution in ("exact", "delayed")
    ]
    enriched["all_years_positive_v31"] = enriched[annual_columns].gt(0.0).all(axis=1)
    active = enriched["family"].ne(NO_TRADE)
    enriched["strict_fixed_pass_v31"] = active & (
        enriched["full_exact_trades"].ge(60)
        & enriched["full_delayed_trades"].ge(60)
        & enriched["full_exact_wilson95LowerPct"].gt(BREAKEVEN_WR)
        & enriched["full_delayed_wilson95LowerPct"].gt(BREAKEVEN_WR)
        & enriched["full_exact_positiveMonthPct"].ge(60.0)
        & enriched["full_delayed_positiveMonthPct"].ge(60.0)
        & enriched["all_years_positive_v31"]
        & enriched["parameter_support_count"].ge(2)
    )
    enriched.loc[~active, "strict_fixed_pass_v31"] = True
    return enriched


def _yearly_profile_metrics(
    monthly: pd.DataFrame,
    *,
    state: str,
    profile: str,
    horizon_min: int,
    years: Sequence[str],
) -> dict[str, Any]:
    selected = monthly.loc[
        monthly["vol_state"].eq(state)
        & monthly["profile"].astype(str).eq(profile)
        & monthly["horizon_min"].eq(horizon_min)
    ]
    output: dict[str, Any] = {}
    for year in years:
        keys = sorted(selected.loc[selected["month"].astype(str).str[:4].eq(year), "month"].unique())
        output[year] = {
            execution: _aggregate_monthly_metrics(
                selected.loc[
                    selected["execution"].eq(execution)
                    & selected["month"].isin(keys)
                ],
                keys,
            )
            for execution in ("exact", "delayed")
        }
    return output


def descriptive_full_history_champions(
    audit: pd.DataFrame,
    monthly: pd.DataFrame,
    years: Sequence[str],
) -> dict[str, Any]:
    """Select post-hoc champions; these are labels, never causal candidates."""
    active = audit.loc[audit["family"].ne(NO_TRADE)].copy()
    active["robust_wilson"] = active[
        ["full_exact_wilson95LowerPct", "full_delayed_wilson95LowerPct"]
    ].min(axis=1).fillna(0.0)
    active["robust_ev"] = active[
        ["full_exact_expectedValueU", "full_delayed_expectedValueU"]
    ].min(axis=1).fillna(-99.0)
    active["robust_month_pct"] = active[
        ["full_exact_positiveMonthPct", "full_delayed_positiveMonthPct"]
    ].min(axis=1).fillna(0.0)
    output: dict[str, Any] = {}
    for state in STATES:
        state_output: dict[str, Any] = {}
        for family in FAMILIES:
            ranked = active.loc[
                active["vol_state"].eq(state) & active["family"].eq(family)
            ].sort_values(
                [
                    "strict_fixed_pass_v31",
                    "all_years_positive_v31",
                    "robust_wilson",
                    "robust_ev",
                    "robust_month_pct",
                    "parameter_support_count",
                    "profile",
                ],
                ascending=[False, False, False, False, False, False, True],
                kind="stable",
            )
            if ranked.empty:
                state_output[family] = {"bestObserved": None, "causalAction": NO_TRADE}
                continue
            row = ranked.iloc[0]
            profile = str(row["profile"])
            horizon = int(row["horizon_min"])
            state_output[family] = {
                "selectionSemantics": "post_hoc_full_history_descriptive_only",
                "bestObserved": {
                    "profile": profile,
                    "lookbackMin": int(row["lookback_min"]),
                    "threshold": float(row["threshold"]),
                    "horizonMin": horizon,
                    "exact": {
                        "trades": int(row["full_exact_trades"]),
                        "winRatePct": row["full_exact_winRatePct"],
                        "wilson95LowerPct": row["full_exact_wilson95LowerPct"],
                        "pnlU": row["full_exact_pnlU"],
                    },
                    "delayed": {
                        "trades": int(row["full_delayed_trades"]),
                        "winRatePct": row["full_delayed_winRatePct"],
                        "wilson95LowerPct": row["full_delayed_wilson95LowerPct"],
                        "pnlU": row["full_delayed_pnlU"],
                    },
                    "yearly": _yearly_profile_metrics(
                        monthly,
                        state=state,
                        profile=profile,
                        horizon_min=horizon,
                        years=years,
                    ),
                    "allYearsPositive": bool(row["all_years_positive_v31"]),
                    "parameterSupportCount": int(row["parameter_support_count"]),
                    "strictFixedPass": bool(row["strict_fixed_pass_v31"]),
                },
                "causalAction": NO_TRADE,
            }
        state_output[NO_TRADE] = {
            "selectionSemantics": "risk_free_baseline",
            "trades": 0,
            "pnlU": 0.0,
        }
        output[state] = state_output
    return output


def _trade_pair(frame: pd.DataFrame) -> dict[str, Any]:
    return {
        execution: metrics_from_signed(frame, f"signed_bps_{execution}")
        for execution in ("exact", "delayed")
    }


def _selection_count(
    selections: pd.DataFrame,
    *,
    state: str | None = None,
    family: str | None = None,
    year: str | None = None,
) -> int:
    part = selections
    if state is not None:
        part = part.loc[part["vol_state"].eq(state)]
    if family is not None:
        part = part.loc[part["family"].eq(family)]
    if year is not None:
        part = part.loc[part["month"].astype(str).str[:4].eq(year)]
    return int(len(part))


def causal_breakdowns(
    selections: pd.DataFrame,
    trades: pd.DataFrame,
    years: Sequence[str],
) -> dict[str, Any]:
    signal_time = (
        pd.to_datetime(trades["signal_time"], utc=True)
        if not trades.empty
        else pd.Series([], dtype="datetime64[ns, UTC]")
    )
    by_state: dict[str, Any] = {}
    by_family: dict[str, Any] = {}
    by_state_family: dict[str, Any] = {}
    for state in STATES:
        state_trades = trades.loc[trades["vol_state"].astype(str).eq(state)] if not trades.empty else trades
        by_state[state] = {
            "selectionCells": _selection_count(selections, state=state),
            **_trade_pair(state_trades),
        }
        by_state_family[state] = {}
        for family in ALL_ACTIONS:
            part = (
                state_trades.loc[state_trades["selected_family"].eq(family)]
                if not state_trades.empty and family != NO_TRADE
                else state_trades.iloc[0:0]
            )
            by_state_family[state][family] = {
                "selectionCells": _selection_count(
                    selections, state=state, family=family
                ),
                **_trade_pair(part),
            }
    for family in ALL_ACTIONS:
        part = (
            trades.loc[trades["selected_family"].eq(family)]
            if not trades.empty and family != NO_TRADE
            else trades.iloc[0:0]
        )
        by_family[family] = {
            "selectionCells": _selection_count(selections, family=family),
            **_trade_pair(part),
        }

    by_year_state_family: dict[str, Any] = {}
    for year in years:
        year_trades = trades.loc[signal_time.dt.strftime("%Y").eq(year)] if not trades.empty else trades
        by_year_state_family[year] = {}
        for state in STATES:
            state_trades = year_trades.loc[
                year_trades["vol_state"].astype(str).eq(state)
            ] if not year_trades.empty else year_trades
            by_year_state_family[year][state] = {}
            for family in ALL_ACTIONS:
                part = (
                    state_trades.loc[state_trades["selected_family"].eq(family)]
                    if not state_trades.empty and family != NO_TRADE
                    else state_trades.iloc[0:0]
                )
                by_year_state_family[year][state][family] = {
                    "selectionCells": _selection_count(
                        selections, state=state, family=family, year=year
                    ),
                    **_trade_pair(part),
                }
    return {
        "byState": by_state,
        "byFamily": by_family,
        "byStateFamily": by_state_family,
        "byYearStateFamily": by_year_state_family,
    }


def augment_walkforward_report(
    base: dict[str, Any],
    selections: pd.DataFrame,
    trades: pd.DataFrame,
    years: Sequence[str],
    training_window_months: int,
) -> dict[str, Any]:
    causality = validate_causal_selections(selections, training_window_months)
    breakdowns = causal_breakdowns(selections, trades, years)
    yearly: dict[str, Any] = {}
    signal_year = (
        pd.to_datetime(trades["signal_time"], utc=True).dt.strftime("%Y")
        if not trades.empty
        else pd.Series([], dtype="object")
    )
    for year in years:
        part = trades.loc[signal_year.eq(year)] if not trades.empty else trades
        yearly[year] = _trade_pair(part)

    exact = base["overall"]["exact"]
    delayed = base["overall"]["delayed"]
    active_years = [
        year for year in years if yearly[year]["exact"]["trades"] > 0
    ]
    all_active_years_positive = bool(
        len(active_years) >= 3
        and all(
            yearly[year][execution]["pnlU"] > 0.0
            for year in active_years
            for execution in ("exact", "delayed")
        )
    )
    passed = bool(
        exact["trades"] >= 100
        and delayed["trades"] >= 100
        and exact["wilson95LowerPct"] is not None
        and exact["wilson95LowerPct"] > BREAKEVEN_WR
        and delayed["wilson95LowerPct"] is not None
        and delayed["wilson95LowerPct"] > BREAKEVEN_WR
        and base["activeFoldCount"] >= 6
        and base["positiveExactActiveFoldPct"] >= 60.0
        and base["positiveDelayedActiveFoldPct"] >= 60.0
        and all_active_years_positive
    )
    return {
        **base,
        "yearly": yearly,
        "causalityAudit": causality,
        **breakdowns,
        "allActiveYearsPositive": all_active_years_positive,
        "passed": passed,
        "decision": "research_candidate_only" if passed else NO_TRADE,
        "interpretation": (
            "causal monthly walk-forward; each monthly state/action choice uses "
            "only the immediately preceding training window"
        ),
    }


def run(
    inputs: Sequence[str | Path] = (EARLY_INPUT, LATE_INPUT),
    manifests: Sequence[str | Path] = (EARLY_MANIFEST, LATE_MANIFEST),
) -> dict[str, Any]:
    minutes, frozen_inputs = load_frozen_history(inputs, manifests)
    volatility = build_volatility_states(minutes, VOLATILITY_WINDOW_MIN)
    candidates = generate_candidate_matrix(minutes, volatility)
    folds = calendar_folds(minutes.index)
    years = _year_keys(folds)
    data_end = minutes.index[-1] + pd.Timedelta(minutes=1)
    audit, monthly, group_indices = build_fixed_audit(
        candidates, folds, data_end
    )
    audit = add_full_history_stability(audit, monthly, years)
    audit.to_csv(OUT_AUDIT, index=False, encoding="utf-8-sig")
    monthly.to_csv(OUT_MONTHLY, index=False, encoding="utf-8-sig")

    walkforward: dict[str, Any] = {}
    selection_parts: list[pd.DataFrame] = []
    trade_parts: list[pd.DataFrame] = []
    for training_months in TRAINING_WINDOWS_MONTHS:
        base, selections, trades = run_walkforward_mode(
            candidates,
            group_indices,
            monthly,
            folds,
            training_months,
        )
        mode = f"rolling_{training_months}m"
        walkforward[mode] = augment_walkforward_report(
            base,
            selections,
            trades,
            years,
            training_months,
        )
        selection_parts.append(selections)
        trade_parts.append(trades)

    selections_all = pd.concat(selection_parts, ignore_index=True)
    trades_all = pd.concat(trade_parts, ignore_index=True)
    selections_all.to_csv(OUT_SELECTIONS, index=False, encoding="utf-8-sig")
    trades_all.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")

    descriptive = descriptive_full_history_champions(audit, monthly, years)
    passed_modes = [mode for mode, value in walkforward.items() if value["passed"]]
    platform_passed = len(passed_modes) == len(TRAINING_WINDOWS_MONTHS)
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "status": "V31_FULL_HISTORY_CAUSAL_REGIME_ACTION_WALKFORWARD",
        "safety": {
            "researchOnly": True,
            "tradeEnabled": False,
            "realTradingAllowed": False,
            "deploymentPerformed": False,
            "onlineConfigurationChanged": False,
            "frontendChanged": False,
        },
        "data": {
            "frozenInputs": frozen_inputs,
            "combinedRows": int(len(minutes)),
            "start": minutes.index[0],
            "end": minutes.index[-1],
            "contiguous": True,
            "duplicateMinutes": 0,
            "seam": (
                f"{frozen_inputs[0]['end'].isoformat()} -> "
                f"{frozen_inputs[1]['start'].isoformat()}"
            ),
        },
        "design": {
            "volatilityState": {
                "windowMin": VOLATILITY_WINDOW_MIN,
                "thresholdHistoryMin": VOL_HISTORY_MIN,
                "thresholdMinPeriods": VOL_HISTORY_MIN_PERIODS,
                "thresholds": "causal trailing q33/q67, shifted before classification",
                "states": list(STATES),
                "summary": volatility_summary(volatility),
            },
            "normalLookbacksMin": list(LOOKBACKS_MIN),
            "thresholds": list(THRESHOLDS),
            "horizonsMin": list(HORIZONS_MIN),
            "executionDelaysMin": list(DELAYS_MIN),
            "families": list(FAMILIES),
            "noTradeComparedExplicitly": True,
            "profileCount": int(len(PROFILES)),
            "stateVariantCount": int(len(PROFILES) * len(HORIZONS_MIN) * len(STATES)),
            "walkForwardTrainingWindowsMonths": list(TRAINING_WINDOWS_MONTHS),
            "testFrequency": "one calendar month",
            "selectionInformation": "only immediately preceding calendar months",
            "tenMinuteClarification": (
                "10m is one of five normal lookbacks and one of three holding horizons; "
                "volatility classification uses 120m"
            ),
        },
        "candidateRows": int(len(candidates)),
        "descriptiveFullHistory": {
            "selectionUsesFutureRelativeToEarlierMonths": True,
            "causalEvidence": False,
            "stateFamilyChampions": descriptive,
            "strictPassedRows": int(
                audit.loc[audit["family"].ne(NO_TRADE), "strict_fixed_pass_v31"].sum()
            ),
            "interpretation": (
                "post-hoc full-history champions describe the sample only and cannot be "
                "used as deployment evidence"
            ),
        },
        "causalWalkForward": walkforward,
        "decision": {
            "passedModes": passed_modes,
            "requiredModes": [f"rolling_{value}m" for value in TRAINING_WINDOWS_MONTHS],
            "platformPassed": platform_passed,
            "action": "research_candidate_only" if platform_passed else NO_TRADE,
            "deployment": "none",
            "realTradingAllowed": False,
        },
        "outputs": {
            "json": str(OUT_JSON.resolve()),
            "fixedAudit": str(OUT_AUDIT.resolve()),
            "monthlyMatrix": str(OUT_MONTHLY.resolve()),
            "walkForwardSelections": str(OUT_SELECTIONS.resolve()),
            "walkForwardTrades": str(OUT_TRADES.resolve()),
        },
    }
    OUT_JSON.write_text(
        json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def _compact_console(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "data": {
            key: report["data"][key]
            for key in ("combinedRows", "start", "end", "contiguous")
        },
        "candidateRows": report["candidateRows"],
        "strictPassedRows": report["descriptiveFullHistory"]["strictPassedRows"],
        "causalWalkForward": {
            mode: {
                key: value[key]
                for key in (
                    "overall",
                    "yearly",
                    "familySelectionCounts",
                    "activeFoldCount",
                    "positiveExactActiveFoldPct",
                    "positiveDelayedActiveFoldPct",
                    "allActiveYearsPositive",
                    "passed",
                    "decision",
                )
            }
            for mode, value in report["causalWalkForward"].items()
        },
        "decision": report["decision"],
        "outputs": report["outputs"],
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
