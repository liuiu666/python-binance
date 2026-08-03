"""V32 full-history causal volatility x stationarity action router.

Research only.  V32 reuses the profiles and structural thresholds frozen in V19,
joins the two verified BTCUSDT futures minute histories, and performs rolling
3/6/12-month selection with an explicit no-trade fallback.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Iterable, Sequence

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
from research_minute_volatility_normal_v15 import (
    BREAKEVEN_WR,
    apply_shared_cooldown,
    clean,
    wilson_lower,
)
from research_stationarity_router_v19 import (
    CELLS,
    PROFILES,
    VOLATILITY_WINDOW_MIN,
    _bootstrap_block_ev,
    generate_candidates,
)
from research_volatility_window_sensitivity_v17 import build_volatility_states
from stationarity_features_v19 import (
    ESTIMATION_WINDOW_MIN,
    build_stationarity_features,
)


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "tmp" / "v32_full_history_stationarity_router_20260730.json"
OUT_CANDIDATES = (
    ROOT / "tmp" / "v32_full_history_stationarity_router_candidates_20260730.csv"
)
OUT_SELECTIONS = (
    ROOT / "tmp" / "v32_full_history_stationarity_router_selections_20260730.csv"
)
OUT_TRADES = (
    ROOT / "tmp" / "v32_full_history_stationarity_router_trades_20260730.csv"
)
OUT_PROFILE_AUDIT = (
    ROOT / "tmp" / "v32_full_history_stationarity_router_profile_audit_20260730.csv"
)

TRAINING_WINDOWS_MONTHS = (3, 6, 12)
COOLDOWN_MIN = 10
MIN_TRAIN_TRADES = 90
MIN_POSITIVE_MONTH_PCT = 60.0
MIN_WORST_MONTH_PNL_U = -25.0
MIN_MONTHS_WITH_20_TRADES = 2

EXECUTION_SPECS: dict[str, tuple[str, str]] = {
    "h5_d0": ("status_h5_d0", "pnl_u_h5_d0"),
    "h5_d1": ("status_h5_d1", "pnl_u_h5_d1"),
    "h10_d0": ("status_h10_d0", "pnl_u_h10_d0"),
    "h10_d1": ("status_h10_d1", "pnl_u_h10_d1"),
    "h10_fixed_d1": ("status_h10_fixed_d1", "pnl_u_h10_fixed_d1"),
    "h20_d0": ("status_h20_d0", "pnl_u_h20_d0"),
    "h20_d1": ("status_h20_d1", "pnl_u_h20_d1"),
}
MAIN_EXECUTIONS = ("h10_d0", "h10_d1", "h10_fixed_d1")
NEIGHBOR_EXECUTIONS = ("h5_d0", "h5_d1", "h20_d0", "h20_d1")


def common_period_frame(
    frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp
) -> pd.DataFrame:
    """Return signals for which every registered execution settles in-period."""
    if frame.empty:
        return frame.copy()
    entry = pd.to_datetime(frame["entry_time_h20_d1"], utc=True, errors="coerce")
    settlement = pd.to_datetime(
        frame["settle_time_h20_d1"], utc=True, errors="coerce"
    )
    return frame.loc[entry.ge(start) & settlement.lt(end)].copy()


def _empty_metrics(calendar_months: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "ties": 0,
        "winRatePct": None,
        "wilson95LowerPct": None,
        "pnlU": 0.0,
        "expectedValueU": None,
        "maxDrawdownU": 0.0,
        "maxLossStreak": 0,
        "calendarMonths": len(calendar_months),
        "activeMonths": 0,
        "positiveMonthPct": 0.0 if calendar_months else None,
        "positiveActiveMonthPct": None,
        "worstMonthPnlU": 0.0 if calendar_months else None,
        "monthsWithAtLeast20Trades": 0,
    }


def outcome_metrics(
    frame: pd.DataFrame,
    execution: str,
    *,
    calendar_months: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Metrics for one frozen execution using zero-trade calendar months."""
    months = list(calendar_months or [])
    if frame.empty:
        return _empty_metrics(months)
    status_column, pnl_column = EXECUTION_SPECS[execution]
    status = frame[status_column].astype(str)
    settled = frame.loc[status.isin(("won", "lost", "tie"))].copy()
    if settled.empty:
        return _empty_metrics(months)
    settled = settled.sort_values("signal_time", kind="stable")
    status = settled[status_column].astype(str)
    wins = int(status.eq("won").sum())
    losses = int(status.eq("lost").sum())
    ties = int(status.eq("tie").sum())
    decided = wins + losses
    pnl = pd.to_numeric(settled[pnl_column], errors="coerce").fillna(0.0)
    values = pnl.to_numpy(float)
    equity = np.r_[0.0, np.cumsum(values)]
    drawdown = np.maximum.accumulate(equity) - equity
    streak = maximum = 0
    for item in status:
        streak = streak + 1 if item == "lost" else 0
        maximum = max(maximum, streak)
    observed_month = pd.to_datetime(
        settled["signal_time"], utc=True
    ).dt.strftime("%Y-%m")
    month_keys = months or sorted(observed_month.unique())
    monthly_pnl = pnl.groupby(observed_month).sum().reindex(month_keys, fill_value=0.0)
    monthly_trades = observed_month.value_counts().reindex(month_keys, fill_value=0)
    active = monthly_trades.gt(0)
    lower = wilson_lower(wins, decided)
    return {
        "trades": int(len(settled)),
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "winRatePct": round(100.0 * wins / decided, 4) if decided else None,
        "wilson95LowerPct": round(100.0 * lower, 4) if lower is not None else None,
        "pnlU": round(float(values.sum()), 4),
        "expectedValueU": round(float(values.mean()), 6),
        "maxDrawdownU": round(float(drawdown.max()), 4),
        "maxLossStreak": maximum,
        "calendarMonths": int(len(month_keys)),
        "activeMonths": int(active.sum()),
        "positiveMonthPct": round(100.0 * float(monthly_pnl.gt(0.0).mean()), 4)
        if len(month_keys)
        else None,
        "positiveActiveMonthPct": round(
            100.0 * float(monthly_pnl.loc[active].gt(0.0).mean()), 4
        )
        if active.any()
        else None,
        "worstMonthPnlU": round(float(monthly_pnl.min()), 4)
        if len(month_keys)
        else None,
        "monthsWithAtLeast20Trades": int(monthly_trades.ge(20).sum()),
    }


def execution_summary(
    frame: pd.DataFrame, *, calendar_months: Iterable[str] | None = None
) -> dict[str, dict[str, Any]]:
    months = list(calendar_months or [])
    return {
        execution: outcome_metrics(frame, execution, calendar_months=months)
        for execution in EXECUTION_SPECS
    }


def _base_profile_eligible(summary: dict[str, dict[str, Any]]) -> bool:
    main_ok = all(
        row["trades"] >= MIN_TRAIN_TRADES
        and row["pnlU"] > 0.0
        and row["winRatePct"] is not None
        and row["winRatePct"] > BREAKEVEN_WR
        and row["wilson95LowerPct"] is not None
        and row["wilson95LowerPct"] > BREAKEVEN_WR
        and row["positiveMonthPct"] is not None
        and row["positiveMonthPct"] >= MIN_POSITIVE_MONTH_PCT
        and row["worstMonthPnlU"] is not None
        and row["worstMonthPnlU"] >= MIN_WORST_MONTH_PNL_U
        and row["monthsWithAtLeast20Trades"] >= MIN_MONTHS_WITH_20_TRADES
        for row in (summary[key] for key in MAIN_EXECUTIONS)
    )
    neighbor_ok = all(
        row["trades"] >= MIN_TRAIN_TRADES
        and row["pnlU"] > 0.0
        and row["winRatePct"] is not None
        and row["winRatePct"] > BREAKEVEN_WR
        for row in (summary[key] for key in NEIGHBOR_EXECUTIONS)
    )
    return bool(main_ok and neighbor_ok)


def _main_positive(summary: dict[str, dict[str, Any]]) -> bool:
    return all(
        summary[key]["trades"] > 0 and summary[key]["pnlU"] > 0.0
        for key in MAIN_EXECUTIONS
    )


def select_cell_profile(
    candidates: pd.DataFrame,
    cell: str,
    train_start: pd.Timestamp,
    train_end: pd.Timestamp,
    training_months: Sequence[str],
) -> dict[str, Any] | None:
    pool = candidates.loc[candidates["cell"].eq(cell)]
    evaluated: list[dict[str, Any]] = []
    for profile, raw in pool.groupby("profile", sort=True):
        common = common_period_frame(raw, train_start, train_end)
        executed = apply_shared_cooldown(common, cooldown_min=COOLDOWN_MIN)
        summary = execution_summary(executed, calendar_months=training_months)
        family = str(raw["family"].iloc[0])
        evaluated.append(
            {
                "profile": str(profile),
                "family": family,
                "frame": executed,
                "summary": summary,
                "baseEligible": _base_profile_eligible(summary),
            }
        )
    ranked: list[tuple[tuple[float, ...], dict[str, Any]]] = []
    for item in evaluated:
        if not item["baseEligible"]:
            continue
        support = sum(
            peer["family"] == item["family"] and _main_positive(peer["summary"])
            for peer in evaluated
        )
        if support < 2:
            continue
        bootstrap: dict[str, Any] = {}
        for execution in MAIN_EXECUTIONS:
            _, pnl_column = EXECUTION_SPECS[execution]
            bootstrap[execution] = _bootstrap_block_ev(
                item["frame"],
                pnl_column,
                seed_key=(
                    f"v32|{cell}|{item['profile']}|{train_start}|{train_end}|"
                    f"{execution}"
                ),
            )
        if not all(
            row["lower90EvU"] is not None and row["lower90EvU"] > 0.0
            for row in bootstrap.values()
        ):
            continue
        main = [item["summary"][key] for key in MAIN_EXECUTIONS]
        score = (
            min(float(row["wilson95LowerPct"]) for row in main),
            min(float(bootstrap[key]["lower90EvU"]) for key in MAIN_EXECUTIONS),
            min(float(row["expectedValueU"]) for row in main),
            min(float(row["positiveMonthPct"]) for row in main),
            -max(float(row["maxDrawdownU"]) for row in main),
        )
        selected = {
            "profile": item["profile"],
            "family": item["family"],
            "summary": item["summary"],
            "bootstrap": bootstrap,
            "parameterSupportCount": int(support),
            "score": [round(float(value), 6) for value in score],
        }
        ranked.append((score, selected))
    if not ranked:
        return None
    ranked.sort(key=lambda value: value[0], reverse=True)
    return ranked[0][1]


def validate_fold_training(
    test_month: str, observed: Sequence[str], training_window_months: int
) -> None:
    test = pd.Period(test_month, freq="M")
    expected = list(
        pd.period_range(
            test - training_window_months, test - 1, freq="M"
        ).astype(str)
    )
    if list(observed) != expected:
        raise ValueError(
            f"non-causal training months for {test_month}: "
            f"observed={list(observed)}, expected={expected}"
        )


def _selected_test_frame(
    candidates: pd.DataFrame,
    selections: dict[str, dict[str, Any] | None],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for cell, selection in selections.items():
        if selection is None:
            continue
        raw = candidates.loc[
            candidates["cell"].eq(cell)
            & candidates["profile"].eq(selection["profile"])
        ]
        part = common_period_frame(raw, start, end)
        if part.empty:
            continue
        part["selected_cell"] = cell
        part["selected_profile"] = selection["profile"]
        part["selected_family"] = selection["family"]
        parts.append(part)
    combined = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    return apply_shared_cooldown(combined, cooldown_min=COOLDOWN_MIN)


def _yearly_summary(
    trades: pd.DataFrame, years: Sequence[str]
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    observed_year = (
        pd.to_datetime(trades["signal_time"], utc=True).dt.strftime("%Y")
        if not trades.empty
        else pd.Series([], dtype="object")
    )
    for year in years:
        part = trades.loc[observed_year.eq(year)] if not trades.empty else trades
        output[year] = execution_summary(part)
    return output


def _breakdowns(
    trades: pd.DataFrame,
    selections: pd.DataFrame,
    calendar_months: Sequence[str],
    *,
    cells: Sequence[str] = CELLS,
) -> dict[str, Any]:
    by_cell: dict[str, Any] = {}
    for cell in cells:
        part = (
            trades.loc[trades["selected_cell"].eq(cell)]
            if not trades.empty
            else trades
        )
        by_cell[cell] = {
            "selectedMonths": int(
                selections.loc[
                    selections["cell"].eq(cell)
                    & selections["family"].ne("no_trade")
                ].shape[0]
            ),
            "metrics": execution_summary(part, calendar_months=calendar_months),
        }
    by_family: dict[str, Any] = {}
    families = sorted({profile.family for profile in PROFILES})
    for family in families:
        part = (
            trades.loc[trades["selected_family"].eq(family)]
            if not trades.empty
            else trades
        )
        by_family[family] = {
            "selectedCells": int(selections["family"].eq(family).sum()),
            "metrics": execution_summary(part, calendar_months=calendar_months),
        }
    return {"byCell": by_cell, "byFamily": by_family}


def _mode_passed(
    overall: dict[str, dict[str, Any]], yearly: dict[str, Any]
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    for execution in MAIN_EXECUTIONS:
        row = overall[execution]
        if row["trades"] < 100:
            reasons.append(f"{execution}:trades")
        if row["wilson95LowerPct"] is None or row["wilson95LowerPct"] <= BREAKEVEN_WR:
            reasons.append(f"{execution}:wilson")
        if row["positiveMonthPct"] is None or row["positiveMonthPct"] < 60.0:
            reasons.append(f"{execution}:months")
    for execution in NEIGHBOR_EXECUTIONS:
        row = overall[execution]
        if row["trades"] == 0 or row["pnlU"] < 0.0:
            reasons.append(f"{execution}:pnl")
    active_years = [
        year for year, value in yearly.items() if value["h10_d0"]["trades"] > 0
    ]
    if len(active_years) < 3:
        reasons.append("active_years")
    else:
        for year in active_years:
            for execution in MAIN_EXECUTIONS:
                if yearly[year][execution]["pnlU"] <= 0.0:
                    reasons.append(f"{year}:{execution}")
    return not reasons, sorted(set(reasons))


def fixed_profile_audit(
    candidates: pd.DataFrame,
    folds: Sequence[tuple[str, str, pd.Timestamp, pd.Timestamp, bool]],
    *,
    cells: Sequence[str] = CELLS,
    profiles: Sequence[Any] = PROFILES,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Descriptive month-reset audit; never used for causal selection."""
    calendar_months = [item[0] for item in folds]
    years = sorted({month[:4] for month in calendar_months})
    rows: list[dict[str, Any]] = []
    champions: dict[str, Any] = {}
    for cell in cells:
        cell_rows: list[dict[str, Any]] = []
        for profile, raw in candidates.loc[candidates["cell"].eq(cell)].groupby(
            "profile", sort=True
        ):
            executed_parts: list[pd.DataFrame] = []
            for _, _, start, end, _ in folds:
                common = common_period_frame(raw, start, end)
                executed = apply_shared_cooldown(
                    common, cooldown_min=COOLDOWN_MIN
                )
                if not executed.empty:
                    executed_parts.append(executed)
            executed = (
                pd.concat(executed_parts, ignore_index=True)
                if executed_parts
                else pd.DataFrame(columns=raw.columns)
            )
            summary = execution_summary(
                executed, calendar_months=calendar_months
            )
            observed_year = (
                pd.to_datetime(executed["signal_time"], utc=True).dt.strftime("%Y")
                if not executed.empty
                else pd.Series([], dtype="object")
            )
            yearly: dict[str, dict[str, float]] = {}
            active_years: list[str] = []
            for year in years:
                part = (
                    executed.loc[observed_year.eq(year)]
                    if not executed.empty
                    else executed
                )
                yearly[year] = {
                    key: outcome_metrics(part, key)["pnlU"]
                    for key in MAIN_EXECUTIONS
                }
                if outcome_metrics(part, "h10_d0")["trades"] > 0:
                    active_years.append(year)
            all_active_years_positive = bool(
                len(active_years) >= 3
                and all(
                    yearly[year][execution] > 0.0
                    for year in active_years
                    for execution in MAIN_EXECUTIONS
                )
            )
            strict_pass = bool(
                all(
                    summary[key]["trades"] >= 100
                    and summary[key]["wilson95LowerPct"] is not None
                    and summary[key]["wilson95LowerPct"] > BREAKEVEN_WR
                    and summary[key]["positiveMonthPct"] is not None
                    and summary[key]["positiveMonthPct"] >= 60.0
                    for key in MAIN_EXECUTIONS
                )
                and all(summary[key]["pnlU"] >= 0.0 for key in NEIGHBOR_EXECUTIONS)
                and all_active_years_positive
            )
            meta = next(item for item in profiles if item.name == profile)
            row: dict[str, Any] = {
                "cell": cell,
                "vol_state": cell.split("|", 1)[0],
                "structure_state": (
                    cell.split("|", 1)[1] if "|" in cell else "all"
                ),
                "profile": str(profile),
                "family": meta.family,
                "lookback_min": meta.lookback_min,
                "threshold": meta.threshold,
                "active_years": ",".join(active_years),
                "all_active_years_positive": all_active_years_positive,
                "strict_pass": strict_pass,
            }
            for execution, metrics_row in summary.items():
                for key in (
                    "trades",
                    "winRatePct",
                    "wilson95LowerPct",
                    "pnlU",
                    "expectedValueU",
                    "positiveMonthPct",
                    "worstMonthPnlU",
                ):
                    row[f"{execution}_{key}"] = metrics_row[key]
            row["yearly_main_pnl_json"] = json.dumps(yearly, sort_keys=True)
            rows.append(row)
            cell_rows.append(row)
        ranked = sorted(
            cell_rows,
            key=lambda row: (
                min(
                    float(row[f"{key}_wilson95LowerPct"] or 0.0)
                    for key in MAIN_EXECUTIONS
                ),
                min(float(row[f"{key}_pnlU"]) for key in MAIN_EXECUTIONS),
                min(
                    float(row[f"{key}_positiveMonthPct"] or 0.0)
                    for key in MAIN_EXECUTIONS
                ),
            ),
            reverse=True,
        )
        champions[cell] = (
            {
                "profile": ranked[0]["profile"],
                "family": ranked[0]["family"],
                "lookbackMin": ranked[0]["lookback_min"],
                "threshold": ranked[0]["threshold"],
                "strictPass": ranked[0]["strict_pass"],
                "main": {
                    key: {
                        "trades": ranked[0][f"{key}_trades"],
                        "winRatePct": ranked[0][f"{key}_winRatePct"],
                        "wilson95LowerPct": ranked[0][f"{key}_wilson95LowerPct"],
                        "pnlU": ranked[0][f"{key}_pnlU"],
                        "positiveMonthPct": ranked[0][f"{key}_positiveMonthPct"],
                    }
                    for key in MAIN_EXECUTIONS
                },
            }
            if ranked
            else None
        )
    audit = pd.DataFrame(rows)
    report = {
        "profileCellRows": int(len(audit)),
        "strictPassRows": int(audit["strict_pass"].sum()) if not audit.empty else 0,
        "champions": champions,
        "warning": "post-hoc full-history diagnostics only; never causal evidence",
    }
    return audit, report


def run_walkforward_mode(
    candidates: pd.DataFrame,
    folds: Sequence[tuple[str, str, pd.Timestamp, pd.Timestamp, bool]],
    training_window_months: int,
    *,
    cells: Sequence[str] = CELLS,
) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame]:
    selection_rows: list[dict[str, Any]] = []
    trade_parts: list[pd.DataFrame] = []
    fold_reports: list[dict[str, Any]] = []
    test_months: list[str] = []
    for fold_index in range(training_window_months, len(folds)):
        month, fold_name, start, end, complete = folds[fold_index]
        training_slice = folds[fold_index - training_window_months : fold_index]
        training_months = [item[0] for item in training_slice]
        validate_fold_training(month, training_months, training_window_months)
        train_start = training_slice[0][2]
        selections = {
            cell: select_cell_profile(
                candidates, cell, train_start, start, training_months
            )
            for cell in cells
        }
        for cell, selection in selections.items():
            row: dict[str, Any] = {
                "mode": f"rolling_{training_window_months}m",
                "fold": fold_name,
                "month": month,
                "complete_month": complete,
                "train_months": ",".join(training_months),
                "cell": cell,
                "vol_state": cell.split("|", 1)[0],
                "structure_state": (
                    cell.split("|", 1)[1] if "|" in cell else "all"
                ),
            }
            if selection is None:
                row.update(
                    {
                        "profile": "no_trade",
                        "family": "no_trade",
                        "parameter_support_count": 0,
                    }
                )
            else:
                row.update(
                    {
                        "profile": selection["profile"],
                        "family": selection["family"],
                        "parameter_support_count": selection[
                            "parameterSupportCount"
                        ],
                        "train_score": json.dumps(selection["score"]),
                        "train_h10_d0_trades": selection["summary"]["h10_d0"][
                            "trades"
                        ],
                        "train_h10_d0_pnl_u": selection["summary"]["h10_d0"][
                            "pnlU"
                        ],
                        "train_h10_d1_pnl_u": selection["summary"]["h10_d1"][
                            "pnlU"
                        ],
                        "train_h10_fixed_d1_pnl_u": selection["summary"][
                            "h10_fixed_d1"
                        ]["pnlU"],
                    }
                )
            selection_rows.append(row)
        trades = _selected_test_frame(candidates, selections, start, end)
        if not trades.empty:
            trades = trades.copy()
            trades["fold"] = fold_name
            trades["month"] = month
            trades["training_mode"] = f"rolling_{training_window_months}m"
            trade_parts.append(trades)
        test_months.append(month)
        fold_reports.append(
            {
                "fold": fold_name,
                "month": month,
                "completeMonth": complete,
                "trainingMonths": training_months,
                "mapping": {
                    cell: (
                        {
                            "profile": selection["profile"],
                            "family": selection["family"],
                        }
                        if selection is not None
                        else {"family": "no_trade"}
                    )
                    for cell, selection in selections.items()
                },
                "test": execution_summary(trades, calendar_months=[month]),
            }
        )
    selections = pd.DataFrame(selection_rows)
    trades = (
        pd.concat(trade_parts, ignore_index=True)
        if trade_parts
        else pd.DataFrame(
            columns=[
                "signal_time",
                "selected_cell",
                "selected_profile",
                "selected_family",
                "fold",
                "month",
                "training_mode",
            ]
        )
    )
    overall = execution_summary(trades, calendar_months=test_months)
    years = sorted({month[:4] for month in test_months})
    yearly = _yearly_summary(trades, years)
    passed, failure_reasons = _mode_passed(overall, yearly)
    report = {
        "trainingWindowMonths": training_window_months,
        "folds": fold_reports,
        "overall": overall,
        "yearly": yearly,
        **_breakdowns(trades, selections, test_months, cells=cells),
        "selectionCounts": {
            str(key): int(value)
            for key, value in selections["family"].value_counts().items()
        }
        if not selections.empty
        else {"no_trade": 0},
        "passed": passed,
        "failureReasons": failure_reasons,
        "decision": "research_candidate_only" if passed else "no_trade",
    }
    return report, selections, trades


def generate_all_candidates(
    minutes: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    volatility = build_volatility_states(minutes, VOLATILITY_WINDOW_MIN)
    stationarity = build_stationarity_features(
        minutes, estimation_window_min=ESTIMATION_WINDOW_MIN
    )
    parts = [
        generate_candidates(minutes, volatility, stationarity, profile)
        for profile in PROFILES
    ]
    candidates = pd.concat(
        [part for part in parts if not part.empty], ignore_index=True
    )
    return candidates, volatility, stationarity


def run(
    inputs: Sequence[str | Path] = (EARLY_INPUT, LATE_INPUT),
    manifests: Sequence[str | Path] = (EARLY_MANIFEST, LATE_MANIFEST),
) -> dict[str, Any]:
    minutes, frozen_inputs = load_frozen_history(inputs, manifests)
    candidates, volatility, stationarity = generate_all_candidates(minutes)
    candidates.to_csv(OUT_CANDIDATES, index=False, encoding="utf-8-sig")
    folds = calendar_folds(minutes.index)
    profile_audit, profile_audit_report = fixed_profile_audit(candidates, folds)
    profile_audit.to_csv(OUT_PROFILE_AUDIT, index=False, encoding="utf-8-sig")
    mode_reports: dict[str, Any] = {}
    selections_all: list[pd.DataFrame] = []
    trades_all: list[pd.DataFrame] = []
    for training_months in TRAINING_WINDOWS_MONTHS:
        report, selections, trades = run_walkforward_mode(
            candidates, folds, training_months
        )
        mode = f"rolling_{training_months}m"
        mode_reports[mode] = report
        selections_all.append(selections)
        trades_all.append(trades)
    selections_frame = pd.concat(selections_all, ignore_index=True)
    trades_frame = pd.concat(trades_all, ignore_index=True)
    selections_frame.to_csv(OUT_SELECTIONS, index=False, encoding="utf-8-sig")
    trades_frame.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    passed_modes = [mode for mode, value in mode_reports.items() if value["passed"]]
    platform_passed = len(passed_modes) == len(TRAINING_WINDOWS_MONTHS)
    known_volatility = volatility["vol_state"].isin(("low", "mid", "high"))
    structure_counts = stationarity["structure_state"].value_counts()
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "status": "V32_FULL_HISTORY_CAUSAL_STATIONARITY_ROUTER",
        "safety": {
            "researchOnly": True,
            "tradeEnabled": False,
            "realTradingAllowed": False,
            "deploymentPerformed": False,
            "onlineConfigurationChanged": False,
            "frontendChanged": False,
            "secondDataDownloaded": False,
        },
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
            "selectionGate": {
                "minTrades": MIN_TRAIN_TRADES,
                "breakEvenWinRatePct": BREAKEVEN_WR,
                "wilsonMustExceedBreakEven": True,
                "positiveCalendarMonthPct": MIN_POSITIVE_MONTH_PCT,
                "worstMonthPnlU": MIN_WORST_MONTH_PNL_U,
                "minMonthsWith20Trades": MIN_MONTHS_WITH_20_TRADES,
                "blockBootstrapLower90EvMustBePositive": True,
                "adjacentHorizons": list(NEIGHBOR_EXECUTIONS),
                "adjacentProfileRequired": True,
            },
            "monthBoundaryPolicy": "purge all cross-month outcomes and reset cooldown",
        },
        "stateOccupancy": {
            "knownVolatilityPct": round(100.0 * float(known_volatility.mean()), 4),
            "structurePct": {
                str(key): round(100.0 * int(value) / len(stationarity), 4)
                for key, value in structure_counts.items()
            },
        },
        "descriptiveFixedProfileAudit": profile_audit_report,
        "causalWalkForward": mode_reports,
        "decision": {
            "passedModes": passed_modes,
            "requiredModes": [
                f"rolling_{value}m" for value in TRAINING_WINDOWS_MONTHS
            ],
            "platformPassed": platform_passed,
            "action": "research_candidate_only" if platform_passed else "no_trade",
            "secondExecutionEligible": platform_passed,
            "deployment": "none",
            "realTradingAllowed": False,
        },
        "outputs": {
            "json": str(OUT_JSON.resolve()),
            "candidates": str(OUT_CANDIDATES.resolve()),
            "selections": str(OUT_SELECTIONS.resolve()),
            "trades": str(OUT_TRADES.resolve()),
            "profileAudit": str(OUT_PROFILE_AUDIT.resolve()),
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
        "modes": {
            mode: {
                "overall": value["overall"],
                "selectionCounts": value["selectionCounts"],
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
