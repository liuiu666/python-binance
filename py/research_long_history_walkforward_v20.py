"""Long-history frozen V19 router across expanding, 3m and 6m training windows."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from research_multiregime_strategy_v16 import (
    BREAKEVEN_WR,
    apply_shared_cooldown,
    clean,
    load_minutes,
    metrics,
)
from research_stationarity_router_v19 import (
    BOOTSTRAP_SAMPLES,
    CELLS,
    PROFILES,
    _bootstrap_block_ev,
    fixed_metrics,
    generate_candidates,
)
from research_volatility_window_sensitivity_v17 import build_volatility_states
from stationarity_features_v19 import build_stationarity_features


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "data" / "btcusdt_futures_1m_20240101_20260730.csv"
OUT_JSON = ROOT / "tmp" / "v20_long_history_walkforward_20260730.json"
OUT_CANDIDATES = ROOT / "tmp" / "v20_long_history_candidates_20260730.csv"
OUT_TRADES = ROOT / "tmp" / "v20_long_history_trades_20260730.csv"
OUT_PROFILE_AUDIT = ROOT / "tmp" / "v20_long_history_profile_audit_20260730.csv"

TRAINING_MODES: dict[str, int | None] = {
    "rolling_3m": 3,
    "rolling_6m": 6,
    "expanding": None,
}
FIRST_TEST_MONTH = pd.Timestamp("2024-04-01T00:00:00Z")
REVERSE_VALIDATION_END = pd.Timestamp("2026-01-01T00:00:00Z")


def month_folds(index: pd.DatetimeIndex) -> list[tuple[str, pd.Timestamp, pd.Timestamp]]:
    data_end_exclusive = index[-1] + pd.Timedelta(minutes=1)
    starts = pd.date_range(
        FIRST_TEST_MONTH,
        data_end_exclusive.floor("D").replace(day=1),
        freq="MS",
        tz="UTC",
    )
    folds = []
    for start in starts:
        nominal_end = start + pd.DateOffset(months=1)
        end = min(nominal_end, data_end_exclusive)
        if end <= start:
            continue
        complete = end == nominal_end
        name = start.strftime("%Y-%m") if complete else f"{start.strftime('%Y-%m')}_partial"
        folds.append((name, pd.Timestamp(start), pd.Timestamp(end)))
    return folds


def training_window(
    mode: str,
    test_start: pd.Timestamp,
    data_start: pd.Timestamp,
) -> tuple[pd.Timestamp, list[str]]:
    months = TRAINING_MODES[mode]
    if months is None:
        start = data_start.floor("D").replace(day=1)
    else:
        start = test_start - pd.DateOffset(months=months)
    periods = pd.period_range(
        start.tz_localize(None).to_period("M"),
        (test_start - pd.Timedelta(seconds=1)).tz_localize(None).to_period("M"),
        freq="M",
    )
    return pd.Timestamp(start), [period.strftime("%Y-%m") for period in periods]


def training_summary(
    frame: pd.DataFrame,
    mode: str,
    months: list[str],
    *,
    seed_key: str,
) -> dict[str, Any]:
    if mode == "exact":
        summary = metrics(frame, 10, 0)
        status_column = "status_h10_d0"
        pnl_column = "pnl_u_h10_d0"
    elif mode == "fixed":
        summary = fixed_metrics(frame)
        status_column = "status_h10_fixed_d1"
        pnl_column = "pnl_u_h10_fixed_d1"
    else:
        raise ValueError(mode)
    settled = frame.loc[frame[status_column].isin(("won", "lost", "tie"))].copy()
    month = pd.to_datetime(settled["signal_time"], utc=True).dt.strftime("%Y-%m")
    pnl = pd.to_numeric(settled[pnl_column], errors="coerce").fillna(0.0)
    monthly_pnl = pnl.groupby(month).sum().reindex(months, fill_value=0.0)
    monthly_trades = settled.groupby(month).size().reindex(months, fill_value=0)
    summary.update(
        {
            "calendarMonths": months,
            "positiveMonthPctFixedDenominator": round(
                float(monthly_pnl.gt(0.0).mean()) * 100.0, 4
            )
            if months
            else None,
            "monthsWithAtLeast20Trades": int(monthly_trades.ge(20).sum()),
            "monthlyPnlU": {
                str(key): round(float(value), 4)
                for key, value in monthly_pnl.items()
            },
            "monthlyTrades": {
                str(key): int(value) for key, value in monthly_trades.items()
            },
            "bootstrap": _bootstrap_block_ev(
                frame, pnl_column, seed_key=seed_key
            ),
        }
    )
    return summary


def eligible(
    exact: dict[str, Any],
    fixed: dict[str, Any],
    frame: pd.DataFrame,
) -> bool:
    months = exact["calendarMonths"]
    if len(months) < 3:
        return False
    minimum_positive_pct = 100.0 * math.ceil(2.0 * len(months) / 3.0) / len(months)
    if not all(
        row["trades"] >= 90
        and row["monthsWithAtLeast20Trades"] >= 2
        and row["pnlU"] > 0.0
        and row["winRatePct"] is not None
        and row["winRatePct"] > BREAKEVEN_WR
        and row["positiveMonthPctFixedDenominator"] >= minimum_positive_pct
        and row["bootstrap"]["lower90EvU"] is not None
        and row["bootstrap"]["lower90EvU"] > 0.0
        for row in (exact, fixed)
    ):
        return False
    h5 = metrics(frame, 5, 0)
    h20 = metrics(frame, 20, 0)
    return any(
        row["trades"] >= 30
        and row["pnlU"] > 0.0
        and row["winRatePct"] is not None
        and row["winRatePct"] > BREAKEVEN_WR
        for row in (h5, h20)
    )


def select_profile(
    candidates: pd.DataFrame,
    cell: str,
    test_start: pd.Timestamp,
    train_start: pd.Timestamp,
    months: list[str],
    *,
    mode_name: str,
) -> dict[str, Any] | None:
    known = pd.to_datetime(
        candidates["settle_time_h10_d0"], utc=True, errors="coerce"
    ).lt(test_start)
    known &= pd.to_datetime(
        candidates["settle_time_h10_fixed_d1"], utc=True, errors="coerce"
    ).lt(test_start)
    pool = candidates.loc[
        candidates["cell"].eq(cell)
        & known
        & candidates["signal_time"].ge(train_start)
    ]
    ranked = []
    for profile, raw_group in pool.groupby("profile", sort=True):
        group = apply_shared_cooldown(raw_group)
        exact = training_summary(
            group,
            "exact",
            months,
            seed_key=f"{mode_name}|{cell}|{profile}|{test_start}|exact",
        )
        fixed = training_summary(
            group,
            "fixed",
            months,
            seed_key=f"{mode_name}|{cell}|{profile}|{test_start}|fixed",
        )
        if not eligible(exact, fixed, group):
            continue
        meta = next(item for item in PROFILES if item.name == profile)
        score = (
            min(exact["bootstrap"]["lower90EvU"], fixed["bootstrap"]["lower90EvU"]),
            min(exact["expectedValueU"], fixed["expectedValueU"]),
            meta.lookback_min,
            -max(exact["trades"], fixed["trades"]),
        )
        ranked.append((score, str(profile), meta, exact, fixed))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    score, profile, meta, exact, fixed = ranked[0]
    return {
        "profile": profile,
        "family": meta.family,
        "lookbackMin": meta.lookback_min,
        "trainExact": exact,
        "trainFixed": fixed,
        "score": list(score),
    }


def mapped_test(
    candidates: pd.DataFrame,
    mapping: dict[str, str | None],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    parts = []
    for cell, profile in mapping.items():
        if profile is None:
            continue
        parts.append(
            candidates.loc[
                candidates["cell"].eq(cell)
                & candidates["profile"].eq(profile)
                & candidates["signal_time"].ge(start)
                & candidates["signal_time"].lt(end)
            ]
        )
    frame = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=candidates.columns)
    return apply_shared_cooldown(frame)


def mode_walkforward(
    candidates: pd.DataFrame,
    folds: list[tuple[str, pd.Timestamp, pd.Timestamp]],
    data_start: pd.Timestamp,
    mode_name: str,
) -> tuple[list[dict[str, Any]], pd.DataFrame]:
    reports = []
    trade_parts = []
    for name, start, end in folds:
        train_start, months = training_window(mode_name, start, data_start)
        selection = {
            cell: select_profile(
                candidates,
                cell,
                start,
                train_start,
                months,
                mode_name=mode_name,
            )
            for cell in CELLS
        }
        mapping = {
            cell: item["profile"] if item is not None else None
            for cell, item in selection.items()
        }
        trades = mapped_test(candidates, mapping, start, end)
        if not trades.empty:
            tagged = trades.copy()
            tagged["fold"] = name
            tagged["training_mode"] = mode_name
            trade_parts.append(tagged)
        reports.append(
            {
                "name": name,
                "start": start,
                "end": end,
                "trainStart": train_start,
                "trainingMonths": months,
                "mapping": mapping,
                "selection": selection,
                "test": {
                    "exact": metrics(trades, 10, 0),
                    "fixed": fixed_metrics(trades),
                    "h5": metrics(trades, 5, 0),
                    "h20": metrics(trades, 20, 0),
                },
            }
        )
    all_trades = (
        pd.concat(trade_parts, ignore_index=True)
        if trade_parts
        else pd.DataFrame(columns=[*candidates.columns, "fold", "training_mode"])
    )
    return reports, all_trades


def period_summary(
    reports: list[dict[str, Any]],
    trades: pd.DataFrame,
    fold_names: list[str],
    *,
    seed_key: str,
) -> dict[str, Any]:
    part = trades.loc[trades["fold"].isin(fold_names)] if not trades.empty else trades
    exact = metrics(part, 10, 0)
    fixed = fixed_metrics(part)
    exact_bootstrap = _bootstrap_block_ev(
        part, "pnl_u_h10_d0", seed_key=f"{seed_key}|exact"
    )
    fixed_bootstrap = _bootstrap_block_ev(
        part, "pnl_u_h10_fixed_d1", seed_key=f"{seed_key}|fixed"
    )
    fold_pnl = {}
    selected_folds = 0
    for name in fold_names:
        fold_part = part.loc[part["fold"].eq(name)] if not part.empty else part
        fold_pnl[name] = {
            "exact": metrics(fold_part, 10, 0)["pnlU"],
            "fixed": fixed_metrics(fold_part)["pnlU"],
        }
        report = next(item for item in reports if item["name"] == name)
        selected_folds += int(any(value is not None for value in report["mapping"].values()))
    positive_exact = sum(value["exact"] > 0.0 for value in fold_pnl.values())
    positive_fixed = sum(value["fixed"] > 0.0 for value in fold_pnl.values())
    h5 = metrics(part, 5, 0)
    h20 = metrics(part, 20, 0)
    passed = bool(
        len(fold_names) >= 6
        and selected_folds >= 6
        and exact["trades"] >= 200
        and fixed["trades"] >= 200
        and exact["wilson95LowerPct"] is not None
        and exact["wilson95LowerPct"] > BREAKEVEN_WR
        and fixed["wilson95LowerPct"] is not None
        and fixed["wilson95LowerPct"] > BREAKEVEN_WR
        and exact_bootstrap["lower90EvU"] is not None
        and exact_bootstrap["lower90EvU"] > 0.0
        and fixed_bootstrap["lower90EvU"] is not None
        and fixed_bootstrap["lower90EvU"] > 0.0
        and positive_exact / len(fold_names) >= 0.60
        and positive_fixed / len(fold_names) >= 0.60
        and any(
            row["pnlU"] > 0.0
            and row["winRatePct"] is not None
            and row["winRatePct"] > BREAKEVEN_WR
            for row in (h5, h20)
        )
    )
    return {
        "folds": fold_names,
        "selectedFoldCount": selected_folds,
        "selectionCoveragePct": round(100.0 * selected_folds / len(fold_names), 4)
        if fold_names
        else 0.0,
        "exact": exact,
        "fixed": fixed,
        "h5": h5,
        "h20": h20,
        "exactBootstrap": exact_bootstrap,
        "fixedBootstrap": fixed_bootstrap,
        "positiveExactFoldPct": round(100.0 * positive_exact / len(fold_names), 4)
        if fold_names
        else 0.0,
        "positiveFixedFoldPct": round(100.0 * positive_fixed / len(fold_names), 4)
        if fold_names
        else 0.0,
        "foldPnlU": fold_pnl,
        "passed": passed,
        "decision": "research_candidate_only" if passed else "no_trade",
    }


def fixed_profile_audit(candidates: pd.DataFrame) -> pd.DataFrame:
    validation_months = [
        period.strftime("%Y-%m")
        for period in pd.period_range("2024-01", "2025-12", freq="M")
    ]
    rows: list[dict[str, Any]] = []
    for (cell, profile), raw_group in candidates.groupby(["cell", "profile"], sort=True):
        validation = apply_shared_cooldown(
            raw_group.loc[raw_group["signal_time"].lt(REVERSE_VALIDATION_END)]
        )
        reused = apply_shared_cooldown(
            raw_group.loc[raw_group["signal_time"].ge(REVERSE_VALIDATION_END)]
        )
        exact = metrics(validation, 10, 0)
        fixed = fixed_metrics(validation)
        shifted = metrics(validation, 10, 1)
        exact_bootstrap = _bootstrap_block_ev(
            validation,
            "pnl_u_h10_d0",
            seed_key=f"fixed|{cell}|{profile}|exact",
        )
        fixed_bootstrap = _bootstrap_block_ev(
            validation,
            "pnl_u_h10_fixed_d1",
            seed_key=f"fixed|{cell}|{profile}|fixed",
        )
        month = pd.to_datetime(validation["signal_time"], utc=True).dt.strftime("%Y-%m")
        exact_monthly = pd.to_numeric(
            validation["pnl_u_h10_d0"], errors="coerce"
        ).fillna(0.0).groupby(month).sum().reindex(validation_months, fill_value=0.0)
        fixed_monthly = pd.to_numeric(
            validation["pnl_u_h10_fixed_d1"], errors="coerce"
        ).fillna(0.0).groupby(month).sum().reindex(validation_months, fill_value=0.0)
        trade_monthly = validation.groupby(month).size().reindex(
            validation_months, fill_value=0
        )
        reused_exact = metrics(reused, 10, 0)
        reused_fixed = fixed_metrics(reused)
        row: dict[str, Any] = {
            "cell": str(cell),
            "profile": str(profile),
            "family": str(raw_group["family"].iloc[0]),
            "lookback_min": int(raw_group["lookback_min"].iloc[0]),
            "validation_trades": exact["trades"],
            "validation_exact_win_rate_pct": exact["winRatePct"],
            "validation_exact_wilson95_lower_pct": exact["wilson95LowerPct"],
            "validation_exact_pnl_u": exact["pnlU"],
            "validation_shifted_win_rate_pct": shifted["winRatePct"],
            "validation_shifted_pnl_u": shifted["pnlU"],
            "validation_fixed_win_rate_pct": fixed["winRatePct"],
            "validation_fixed_wilson95_lower_pct": fixed["wilson95LowerPct"],
            "validation_fixed_pnl_u": fixed["pnlU"],
            "validation_exact_bootstrap_lower90_ev_u": exact_bootstrap["lower90EvU"],
            "validation_fixed_bootstrap_lower90_ev_u": fixed_bootstrap["lower90EvU"],
            "validation_positive_exact_months": int(exact_monthly.gt(0.0).sum()),
            "validation_positive_fixed_months": int(fixed_monthly.gt(0.0).sum()),
            "validation_months_with_20_trades": int(trade_monthly.ge(20).sum()),
            "reused_2026_trades": reused_exact["trades"],
            "reused_2026_exact_win_rate_pct": reused_exact["winRatePct"],
            "reused_2026_exact_pnl_u": reused_exact["pnlU"],
            "reused_2026_fixed_win_rate_pct": reused_fixed["winRatePct"],
            "reused_2026_fixed_pnl_u": reused_fixed["pnlU"],
        }
        for month_name in validation_months:
            row[f"{month_name}_trades"] = int(trade_monthly.loc[month_name])
            row[f"{month_name}_exact_pnl_u"] = round(
                float(exact_monthly.loc[month_name]), 4
            )
            row[f"{month_name}_fixed_pnl_u"] = round(
                float(fixed_monthly.loc[month_name]), 4
            )
        row["retrospective_candidate"] = bool(
            exact["trades"] >= 200
            and fixed["trades"] >= 200
            and exact["winRatePct"] is not None
            and exact["winRatePct"] > BREAKEVEN_WR
            and fixed["winRatePct"] is not None
            and fixed["winRatePct"] > BREAKEVEN_WR
            and exact_bootstrap["lower90EvU"] is not None
            and exact_bootstrap["lower90EvU"] > 0.0
            and fixed_bootstrap["lower90EvU"] is not None
            and fixed_bootstrap["lower90EvU"] > 0.0
            and exact_monthly.gt(0.0).mean() >= 0.60
            and fixed_monthly.gt(0.0).mean() >= 0.60
        )
        row["strict_pass"] = bool(
            row["retrospective_candidate"]
            and exact["wilson95LowerPct"] is not None
            and exact["wilson95LowerPct"] > BREAKEVEN_WR
            and fixed["wilson95LowerPct"] is not None
            and fixed["wilson95LowerPct"] > BREAKEVEN_WR
        )
        rows.append(row)
    audit = pd.DataFrame(rows)
    if audit.empty:
        return audit
    support = audit.groupby(["cell", "family"])["retrospective_candidate"].transform("sum")
    audit["family_window_platform_support"] = support.astype(int)
    audit["platform_pass"] = audit["retrospective_candidate"] & support.ge(2)
    return audit


def run(input_path: str | Path) -> dict[str, Any]:
    minutes = load_minutes(input_path)
    volatility = build_volatility_states(minutes, 120)
    stationarity = build_stationarity_features(minutes)
    candidates = pd.concat(
        [generate_candidates(minutes, volatility, stationarity, profile) for profile in PROFILES],
        ignore_index=True,
    )
    if not candidates.empty:
        candidates = candidates.sort_values(["signal_time", "profile"], kind="stable").reset_index(drop=True)
    folds = month_folds(minutes.index)
    mode_reports: dict[str, Any] = {}
    all_trade_parts = []
    for mode_name in TRAINING_MODES:
        reports, trades = mode_walkforward(
            candidates, folds, minutes.index[0], mode_name
        )
        if not trades.empty:
            all_trade_parts.append(trades)
        reverse_names = [
            name for name, start, end in folds if end <= REVERSE_VALIDATION_END
        ]
        reused_names = [
            name for name, start, end in folds if start >= REVERSE_VALIDATION_END
        ]
        mode_reports[mode_name] = {
            "folds": reports,
            "reverseHistoricalValidation": period_summary(
                reports,
                trades,
                reverse_names,
                seed_key=f"{mode_name}|reverse",
            ),
            "reused2026Diagnostic": period_summary(
                reports,
                trades,
                reused_names,
                seed_key=f"{mode_name}|2026",
            ),
        }
    all_trades = (
        pd.concat(all_trade_parts, ignore_index=True)
        if all_trade_parts
        else pd.DataFrame(columns=[*candidates.columns, "fold", "training_mode"])
    )
    candidates.to_csv(OUT_CANDIDATES, index=False, encoding="utf-8-sig")
    all_trades.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    profile_audit = fixed_profile_audit(candidates)
    profile_audit.to_csv(OUT_PROFILE_AUDIT, index=False, encoding="utf-8-sig")
    passed_modes = [
        mode
        for mode, result in mode_reports.items()
        if result["reverseHistoricalValidation"]["passed"]
    ]
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "status": "V20_LONG_HISTORY_FROZEN_RULE_WALKFORWARD",
        "safety": {
            "researchOnly": True,
            "tradeEnabled": False,
            "deploymentPerformed": False,
            "realTradingAllowed": False,
        },
        "data": {
            "input": str(Path(input_path).resolve()),
            "rows": int(len(minutes)),
            "start": minutes.index[0],
            "end": minutes.index[-1],
            "candidateRows": int(len(candidates)),
        },
        "design": {
            "ruleFreeze": "V19 definitions were written and tested before opening the extended history",
            "trainingModes": TRAINING_MODES,
            "firstTestMonth": FIRST_TEST_MONTH,
            "reverseHistoricalValidationEndExclusive": REVERSE_VALIDATION_END,
            "profiles": [profile.__dict__ for profile in PROFILES],
            "bootstrapSamples": BOOTSTRAP_SAMPLES,
            "note": "2024-2025 is newly opened reverse-time historical validation, not future chronological holdout.",
        },
        "modes": mode_reports,
        "fixedProfileAudit": {
            "profileCount": int(len(profile_audit)),
            "retrospectiveCandidates": profile_audit.loc[
                profile_audit["retrospective_candidate"]
            ]["profile"].astype(str).tolist()
            if not profile_audit.empty
            else [],
            "strictPassProfiles": profile_audit.loc[profile_audit["strict_pass"]][
                "profile"
            ].astype(str).tolist()
            if not profile_audit.empty
            else [],
            "platformPassProfiles": profile_audit.loc[profile_audit["platform_pass"]][
                "profile"
            ].astype(str).tolist()
            if not profile_audit.empty
            else [],
        },
        "decision": {
            "reverseValidationPassedModes": passed_modes,
            "platformPassed": len(passed_modes) >= 2,
            "deployment": "none",
            "realTradingAllowed": False,
        },
        "outputs": {
            "json": str(OUT_JSON),
            "candidates": str(OUT_CANDIDATES),
            "trades": str(OUT_TRADES),
            "profileAudit": str(OUT_PROFILE_AUDIT),
        },
    }
    OUT_JSON.write_text(
        json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(INPUT))
    args = parser.parse_args()
    report = run(args.input)
    print(
        json.dumps(
            clean(
                {
                    "modes": {
                        mode: {
                            "reverseHistoricalValidation": value[
                                "reverseHistoricalValidation"
                            ],
                            "reused2026Diagnostic": value["reused2026Diagnostic"],
                        }
                        for mode, value in report["modes"].items()
                    },
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
