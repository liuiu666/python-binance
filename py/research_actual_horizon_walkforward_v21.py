"""Actual engine semantics: entry delay shifts the 600-second settlement."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from research_long_history_walkforward_v20 import (
    FIRST_TEST_MONTH,
    INPUT,
    REVERSE_VALIDATION_END,
    TRAINING_MODES,
    month_folds,
    training_window,
)
from research_multiregime_strategy_v16 import (
    BREAKEVEN_WR,
    apply_shared_cooldown,
    clean,
    load_minutes,
    metrics,
)
from research_stationarity_router_v19 import (
    CELLS,
    PROFILES,
    _bootstrap_block_ev,
    fixed_metrics,
)


ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = ROOT / "tmp" / "v20_long_history_candidates_20260730.csv"
OUT_JSON = ROOT / "tmp" / "v21_actual_horizon_walkforward_20260730.json"
OUT_TRADES = ROOT / "tmp" / "v21_actual_horizon_walkforward_trades_20260730.csv"


def load_candidates(path: str | Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    required = {
        "profile",
        "family",
        "cell",
        "signal_time",
        "settle_time_h10_d0",
        "settle_time_h10_d1",
        "status_h10_d0",
        "status_h10_d1",
        "pnl_u_h10_d0",
        "pnl_u_h10_d1",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"V20 candidates missing columns: {missing}")
    for column in frame.columns:
        if column in {"signal_time", "signal_bar_time"} or column.startswith(
            ("entry_time_", "settle_time_")
        ):
            frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
    declared = {profile.name for profile in PROFILES}
    if not set(frame["profile"].astype(str)).issubset(declared):
        raise ValueError("candidate file contains profiles outside frozen V19")
    return frame.sort_values(["signal_time", "profile"], kind="stable").reset_index(drop=True)


def training_summary(
    frame: pd.DataFrame,
    delay: int,
    months: list[str],
    *,
    seed_key: str,
) -> dict[str, Any]:
    summary = metrics(frame, 10, delay)
    status_column = f"status_h10_d{delay}"
    pnl_column = f"pnl_u_h10_d{delay}"
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
    shifted: dict[str, Any],
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
        for row in (exact, shifted)
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
        candidates["settle_time_h10_d1"], utc=True, errors="coerce"
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
            0,
            months,
            seed_key=f"{mode_name}|{cell}|{profile}|{test_start}|d0",
        )
        shifted = training_summary(
            group,
            1,
            months,
            seed_key=f"{mode_name}|{cell}|{profile}|{test_start}|d1",
        )
        if not eligible(exact, shifted, group):
            continue
        meta = next(item for item in PROFILES if item.name == profile)
        score = (
            min(exact["bootstrap"]["lower90EvU"], shifted["bootstrap"]["lower90EvU"]),
            min(exact["expectedValueU"], shifted["expectedValueU"]),
            meta.lookback_min,
            -max(exact["trades"], shifted["trades"]),
        )
        ranked.append((score, str(profile), meta, exact, shifted))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    score, profile, meta, exact, shifted = ranked[0]
    return {
        "profile": profile,
        "family": meta.family,
        "lookbackMin": meta.lookback_min,
        "trainExact": exact,
        "trainShiftedOneMinute": shifted,
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


def walkforward(
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
                "trainStart": train_start,
                "trainingMonths": months,
                "selection": selection,
                "mapping": mapping,
                "test": {
                    "exact": metrics(trades, 10, 0),
                    "shiftedOneMinute": metrics(trades, 10, 1),
                    "fixedSettlementStress": fixed_metrics(trades),
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
    shifted = metrics(part, 10, 1)
    fixed = fixed_metrics(part)
    exact_bootstrap = _bootstrap_block_ev(
        part, "pnl_u_h10_d0", seed_key=f"{seed_key}|d0"
    )
    shifted_bootstrap = _bootstrap_block_ev(
        part, "pnl_u_h10_d1", seed_key=f"{seed_key}|d1"
    )
    fold_pnl = {}
    selected_folds = 0
    for name in fold_names:
        fold_part = part.loc[part["fold"].eq(name)] if not part.empty else part
        fold_pnl[name] = {
            "exact": metrics(fold_part, 10, 0)["pnlU"],
            "shifted": metrics(fold_part, 10, 1)["pnlU"],
            "fixedStress": fixed_metrics(fold_part)["pnlU"],
        }
        report = next(item for item in reports if item["name"] == name)
        selected_folds += int(any(value is not None for value in report["mapping"].values()))
    positive_exact = sum(value["exact"] > 0.0 for value in fold_pnl.values())
    positive_shifted = sum(value["shifted"] > 0.0 for value in fold_pnl.values())
    h5 = metrics(part, 5, 0)
    h20 = metrics(part, 20, 0)
    passed = bool(
        len(fold_names) >= 6
        and selected_folds >= 6
        and exact["trades"] >= 200
        and shifted["trades"] >= 200
        and exact["wilson95LowerPct"] is not None
        and exact["wilson95LowerPct"] > BREAKEVEN_WR
        and shifted["wilson95LowerPct"] is not None
        and shifted["wilson95LowerPct"] > BREAKEVEN_WR
        and exact_bootstrap["lower90EvU"] is not None
        and exact_bootstrap["lower90EvU"] > 0.0
        and shifted_bootstrap["lower90EvU"] is not None
        and shifted_bootstrap["lower90EvU"] > 0.0
        and positive_exact / len(fold_names) >= 0.60
        and positive_shifted / len(fold_names) >= 0.60
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
        "shiftedOneMinute": shifted,
        "fixedSettlementStress": fixed,
        "h5": h5,
        "h20": h20,
        "exactBootstrap": exact_bootstrap,
        "shiftedBootstrap": shifted_bootstrap,
        "positiveExactFoldPct": round(100.0 * positive_exact / len(fold_names), 4)
        if fold_names
        else 0.0,
        "positiveShiftedFoldPct": round(100.0 * positive_shifted / len(fold_names), 4)
        if fold_names
        else 0.0,
        "foldPnlU": fold_pnl,
        "passed": passed,
        "fixedStressAlsoPositive": fixed["pnlU"] > 0.0,
        "decision": "research_candidate_only" if passed else "no_trade",
    }


def run(input_path: str | Path, candidate_path: str | Path) -> dict[str, Any]:
    minutes = load_minutes(input_path)
    candidates = load_candidates(candidate_path)
    folds = month_folds(minutes.index)
    mode_reports: dict[str, Any] = {}
    trade_parts = []
    for mode_name in TRAINING_MODES:
        reports, trades = walkforward(
            candidates, folds, minutes.index[0], mode_name
        )
        if not trades.empty:
            trade_parts.append(trades)
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
                seed_key=f"{mode_name}|reverse|actual",
            ),
            "reused2026Diagnostic": period_summary(
                reports,
                trades,
                reused_names,
                seed_key=f"{mode_name}|2026|actual",
            ),
        }
    all_trades = (
        pd.concat(trade_parts, ignore_index=True)
        if trade_parts
        else pd.DataFrame(columns=[*candidates.columns, "fold", "training_mode"])
    )
    all_trades.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    passed_modes = [
        mode
        for mode, value in mode_reports.items()
        if value["reverseHistoricalValidation"]["passed"]
    ]
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "status": "V21_ACTUAL_ENTRY_PLUS_600S_HORIZON_WALKFORWARD",
        "safety": {
            "researchOnly": True,
            "tradeEnabled": False,
            "deploymentPerformed": False,
            "realTradingAllowed": False,
        },
        "data": {
            "minutes": str(Path(input_path).resolve()),
            "candidates": str(Path(candidate_path).resolve()),
            "rows": int(len(minutes)),
            "candidateRows": int(len(candidates)),
        },
        "design": {
            "profilesAndStates": "frozen V19/V20, unchanged",
            "primaryExecution": "entry delay shifts settlement; hold 600 seconds after actual entry",
            "fixedSettlement": "stress diagnostic only",
            "trainingModes": TRAINING_MODES,
            "reverseHistoricalValidationEndExclusive": REVERSE_VALIDATION_END,
        },
        "modes": mode_reports,
        "decision": {
            "reverseValidationPassedModes": passed_modes,
            "platformPassed": len(passed_modes) >= 2,
            "deployment": "none",
            "realTradingAllowed": False,
        },
        "outputs": {"json": str(OUT_JSON), "trades": str(OUT_TRADES)},
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
                    "modes": {
                        mode: {
                            "reverseHistoricalValidation": value[
                                "reverseHistoricalValidation"
                            ],
                            "reused2026Diagnostic": value[
                                "reused2026Diagnostic"
                            ],
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
