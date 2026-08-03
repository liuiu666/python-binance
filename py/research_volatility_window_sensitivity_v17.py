"""Test strategy families across several causal volatility windows.

V17 is a sensitivity audit, not a deployment selector.  It reuses the frozen
V16 candidate labels, remaps every signal into volatility states computed from
15/30/60/120 minute realized-volatility windows, and asks whether any *fixed*
profile survives the April/May/June calendar blocks under both exact-boundary
and one-minute-late/fixed-settlement execution.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research_multiregime_strategy_v16 import (
    AMOUNT_U,
    BREAKEVEN_WR,
    FOLDS,
    PAYOUT_RATE,
    PRIMARY_FOLD_NAMES,
    PROFILES,
    REUSED_DIAGNOSTIC_FOLD,
    STATES,
    apply_shared_cooldown,
    clean,
    load_minutes,
    mapped_test,
    metrics,
    select_for_state,
    summarize,
)


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "data" / "btcusdt_futures_1m_20260131_20260730.csv"
CANDIDATES = ROOT / "tmp" / "v16_multiregime_strategy_20260730_candidates.csv"
OUT_JSON = ROOT / "tmp" / "v17_volatility_window_sensitivity_20260730.json"
OUT_TABLE = ROOT / "tmp" / "v17_volatility_window_profile_stability_20260730.csv"

VOL_WINDOWS_MIN = (15, 30, 60, 120)
VOL_HISTORY_MIN = 7 * 24 * 60
VOL_HISTORY_MIN_PERIODS = 3 * 24 * 60
LOW_QUANTILE = 1.0 / 3.0
HIGH_QUANTILE = 2.0 / 3.0
PRIMARY_HORIZON_MIN = 10
RETROSPECTIVE_PERIODS = (
    ("2026-02_retrospective", pd.Timestamp("2026-02-01T00:00:00Z"), pd.Timestamp("2026-03-01T00:00:00Z")),
    ("2026-03_retrospective", pd.Timestamp("2026-03-01T00:00:00Z"), pd.Timestamp("2026-04-01T00:00:00Z")),
    ("2026-07_reused", pd.Timestamp("2026-07-01T00:00:00Z"), pd.Timestamp("2026-07-30T00:00:00Z")),
)


def load_candidates(path: str | Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    required = {
        "profile",
        "family",
        "signal",
        "signal_bar_time",
        "signal_time",
        "status_h10_d0",
        "pnl_u_h10_d0",
        "entry_time_h10_d1",
        "entry_h10_d1",
        "settle_time_h10_d0",
        "settle_h10_d0",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"V16 candidates missing columns: {missing}")
    for column in frame.columns:
        if column == "signal_time" or column == "signal_bar_time" or column.startswith(
            ("entry_time_", "settle_time_")
        ):
            frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
    declared = {profile.name for profile in PROFILES}
    observed = set(frame["profile"].astype(str).unique())
    if not observed.issubset(declared):
        raise ValueError("candidate file contains profiles outside the locked V16 grid")
    return frame.sort_values(["signal_time", "profile"], kind="stable").reset_index(drop=True)


def build_volatility_states(minutes: pd.DataFrame, window_min: int) -> pd.DataFrame:
    close = minutes["close"].astype(float)
    log_return = np.log(close / close.shift(1))
    rv = log_return.rolling(window_min, min_periods=window_min).std(ddof=0)
    rv10 = rv * math.sqrt(PRIMARY_HORIZON_MIN) * 10_000.0
    prior = rv10.shift(1)
    low = prior.rolling(
        VOL_HISTORY_MIN, min_periods=VOL_HISTORY_MIN_PERIODS
    ).quantile(LOW_QUANTILE)
    high = prior.rolling(
        VOL_HISTORY_MIN, min_periods=VOL_HISTORY_MIN_PERIODS
    ).quantile(HIGH_QUANTILE)
    state = pd.Series("unknown", index=minutes.index, dtype="object")
    ready = rv10.notna() & low.notna() & high.notna()
    state.loc[ready & rv10.le(low)] = "low"
    state.loc[ready & rv10.gt(low) & rv10.lt(high)] = "mid"
    state.loc[ready & rv10.ge(high)] = "high"
    return pd.DataFrame(
        {
            "rv10m_bps": rv10,
            "prior_q33_bps": low,
            "prior_q67_bps": high,
            "vol_state": state,
        },
        index=minutes.index,
    )


def volatility_summary(volatility: pd.DataFrame) -> dict[str, Any]:
    known = volatility.loc[volatility["vol_state"].isin(STATES), "vol_state"]
    transition = known.ne(known.shift(1))
    return {
        "knownMinutes": int(len(known)),
        "statePct": {
            state: round(float(known.eq(state).mean()) * 100.0, 4) for state in STATES
        },
        "oneMinutePersistencePct": round(float((~transition.iloc[1:]).mean()) * 100.0, 4)
        if len(transition) > 1
        else None,
    }


def remap_states(candidates: pd.DataFrame, volatility: pd.DataFrame) -> pd.DataFrame:
    positions = pd.DatetimeIndex(candidates["signal_bar_time"])
    mapped_volatility = volatility.reindex(positions)
    frame = candidates.copy()
    frame["vol_state"] = mapped_volatility["vol_state"].to_numpy()
    frame["rv10m_bps"] = mapped_volatility["rv10m_bps"].to_numpy()
    return frame.loc[frame["vol_state"].isin(STATES)].reset_index(drop=True)


def fixed_settlement_delay_metrics(
    frame: pd.DataFrame,
    *,
    period_start: pd.Timestamp | None = None,
    period_end: pd.Timestamp | None = None,
) -> dict[str, Any]:
    """One-minute late entry, but settlement remains the original contract end."""
    if frame.empty:
        return metrics(pd.DataFrame(), 10, 0)
    direction = np.where(frame["signal"].eq("UP"), 1.0, -1.0)
    entry = pd.to_numeric(frame["entry_h10_d1"], errors="coerce")
    settle = pd.to_numeric(frame["settle_h10_d0"], errors="coerce")
    signed = (settle / entry - 1.0) * 10_000.0 * direction
    usable = entry.notna() & settle.notna()
    status = np.where(signed > 0.0, "won", np.where(signed < 0.0, "lost", "tie"))
    pnl = np.where(signed > 0.0, AMOUNT_U * PAYOUT_RATE, np.where(signed < 0.0, -AMOUNT_U, 0.0))
    alias = pd.DataFrame(
        {
            "signal_time": frame["signal_time"].to_numpy(),
            "entry_time_h10_d0": frame["entry_time_h10_d1"].to_numpy(),
            "settle_time_h10_d0": frame["settle_time_h10_d0"].to_numpy(),
            "status_h10_d0": np.where(usable, status, "missing"),
            "pnl_u_h10_d0": np.where(usable, pnl, np.nan),
        }
    )
    return metrics(
        alias,
        10,
        0,
        period_start=period_start,
        period_end=period_end,
    )


def profile_stability_rows(candidates: pd.DataFrame, vol_window_min: int) -> list[dict[str, Any]]:
    primary_folds = [fold for fold in FOLDS if fold[0] in PRIMARY_FOLD_NAMES]
    start = primary_folds[0][1]
    end = primary_folds[-1][2]
    rows: list[dict[str, Any]] = []
    for (state, profile), raw_group in candidates.groupby(["vol_state", "profile"], sort=True):
        primary_group = raw_group.loc[
            raw_group["signal_time"].ge(start) & raw_group["signal_time"].lt(end)
        ]
        group = apply_shared_cooldown(primary_group)
        exact = metrics(group, 10, 0, period_start=start, period_end=end)
        fixed_delay = fixed_settlement_delay_metrics(
            group, period_start=start, period_end=end
        )
        profile_meta = next(item for item in PROFILES if item.name == profile)
        row: dict[str, Any] = {
            "vol_window_min": vol_window_min,
            "vol_state": str(state),
            "profile": str(profile),
            "family": profile_meta.family,
            "lookback_min": profile_meta.lookback_min,
            "threshold": profile_meta.threshold,
            "trades": exact["trades"],
            "win_rate_pct": exact["winRatePct"],
            "pnl_u": exact["pnlU"],
            "wilson95_lower_pct": exact["wilson95LowerPct"],
            "fixed_delay_win_rate_pct": fixed_delay["winRatePct"],
            "fixed_delay_pnl_u": fixed_delay["pnlU"],
        }
        exact_positive = fixed_positive = 0
        exact_pnls: list[float] = []
        fixed_pnls: list[float] = []
        for fold_name, fold_start, fold_end in primary_folds:
            fold_group = group.loc[
                group["signal_time"].ge(fold_start)
                & group["signal_time"].lt(fold_end)
            ]
            fold_exact = metrics(
                fold_group, 10, 0, period_start=fold_start, period_end=fold_end
            )
            fold_fixed = fixed_settlement_delay_metrics(
                fold_group, period_start=fold_start, period_end=fold_end
            )
            exact_pnls.append(float(fold_exact["pnlU"]))
            fixed_pnls.append(float(fold_fixed["pnlU"]))
            exact_positive += int(fold_exact["pnlU"] > 0.0)
            fixed_positive += int(fold_fixed["pnlU"] > 0.0)
            row[f"{fold_name}_trades"] = fold_exact["trades"]
            row[f"{fold_name}_pnl_u"] = fold_exact["pnlU"]
            row[f"{fold_name}_fixed_delay_pnl_u"] = fold_fixed["pnlU"]
        row["positive_folds"] = exact_positive
        row["fixed_delay_positive_folds"] = fixed_positive
        row["worst_fold_pnl_u"] = min(exact_pnls)
        row["fixed_delay_worst_fold_pnl_u"] = min(fixed_pnls)
        row["retrospective_candidate"] = bool(
            exact["trades"] >= 90
            and exact["winRatePct"] is not None
            and exact["winRatePct"] > BREAKEVEN_WR
            and fixed_delay["winRatePct"] is not None
            and fixed_delay["winRatePct"] > BREAKEVEN_WR
            and exact_positive == len(primary_folds)
            and fixed_positive == len(primary_folds)
        )
        row["strict_pass"] = bool(
            row["retrospective_candidate"]
            and exact["wilson95LowerPct"] is not None
            and exact["wilson95LowerPct"] > BREAKEVEN_WR
            and fixed_delay["wilson95LowerPct"] is not None
            and fixed_delay["wilson95LowerPct"] > BREAKEVEN_WR
        )
        for period_name, period_start, period_end in RETROSPECTIVE_PERIODS:
            period_group = raw_group.loc[
                raw_group["signal_time"].ge(period_start)
                & raw_group["signal_time"].lt(period_end)
            ]
            period_group = apply_shared_cooldown(period_group)
            period_exact = metrics(
                period_group,
                10,
                0,
                period_start=period_start,
                period_end=period_end,
            )
            period_fixed = fixed_settlement_delay_metrics(
                period_group, period_start=period_start, period_end=period_end
            )
            row[f"{period_name}_trades"] = period_exact["trades"]
            row[f"{period_name}_pnl_u"] = period_exact["pnlU"]
            row[f"{period_name}_fixed_delay_pnl_u"] = period_fixed["pnlU"]
        rows.append(row)
    return rows


def family_diagnostics(stability: pd.DataFrame) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for state in STATES:
        output[state] = {}
        for family in sorted(stability["family"].unique()):
            part = stability.loc[
                stability["vol_state"].eq(state) & stability["family"].eq(family)
            ].copy()
            if part.empty:
                output[state][family] = {
                    "profiles": 0,
                    "retrospectiveCandidateProfiles": 0,
                    "strictPassProfiles": 0,
                    "bestDiagnostic": None,
                }
                continue
            part["rank_min_positive_folds"] = part[
                ["positive_folds", "fixed_delay_positive_folds"]
            ].min(axis=1)
            part["rank_min_pnl"] = part[["pnl_u", "fixed_delay_pnl_u"]].min(axis=1)
            part["rank_min_win_rate"] = part[
                ["win_rate_pct", "fixed_delay_win_rate_pct"]
            ].min(axis=1)
            ranked = part.sort_values(
                ["strict_pass", "rank_min_positive_folds", "rank_min_win_rate", "rank_min_pnl", "profile"],
                ascending=[False, False, False, False, True],
                kind="stable",
            )
            best = ranked.iloc[0]
            output[state][family] = {
                "profiles": int(len(part)),
                "retrospectiveCandidateProfiles": int(
                    part["retrospective_candidate"].sum()
                ),
                "strictPassProfiles": int(part["strict_pass"].sum()),
                "bestDiagnostic": clean({
                    key: best[key]
                    for key in (
                        "profile",
                        "trades",
                        "win_rate_pct",
                        "pnl_u",
                        "fixed_delay_win_rate_pct",
                        "fixed_delay_pnl_u",
                        "positive_folds",
                        "fixed_delay_positive_folds",
                        "worst_fold_pnl_u",
                        "fixed_delay_worst_fold_pnl_u",
                        "retrospective_candidate",
                        "strict_pass",
                    )
                }),
            }
    return output


def walk_forward(candidates: pd.DataFrame) -> tuple[list[dict[str, Any]], pd.DataFrame]:
    reports: list[dict[str, Any]] = []
    trade_parts: list[pd.DataFrame] = []
    for name, start, end in FOLDS:
        selection = {state: select_for_state(candidates, state, start) for state in STATES}
        mapping = {
            state: item["profile"] if item is not None else None
            for state, item in selection.items()
        }
        trades = mapped_test(candidates, mapping, start, end)
        if not trades.empty:
            tagged = trades.copy()
            tagged["fold"] = name
            trade_parts.append(tagged)
        reports.append(
            {
                "name": name,
                "selection": selection,
                "mapping": mapping,
                "test": summarize(trades, period_start=start, period_end=end),
            }
        )
    all_trades = (
        pd.concat(trade_parts, ignore_index=True)
        if trade_parts
        else pd.DataFrame(columns=[*candidates.columns, "fold"])
    )
    return reports, all_trades


def route_summary(fold_reports: list[dict[str, Any]], trades: pd.DataFrame) -> dict[str, Any]:
    primary = trades.loc[trades["fold"].isin(PRIMARY_FOLD_NAMES)] if not trades.empty else trades
    output: dict[str, Any] = {}
    for state in STATES:
        part = primary.loc[primary["vol_state"].eq(state)] if not primary.empty else primary
        exact = metrics(part, 10, 0)
        fixed_delay = fixed_settlement_delay_metrics(part)
        fold_pnl = {
            name: metrics(part.loc[part["fold"].eq(name)], 10, 0)["pnlU"]
            if not part.empty
            else 0.0
            for name in PRIMARY_FOLD_NAMES
        }
        fixed_fold_pnl = {
            name: fixed_settlement_delay_metrics(part.loc[part["fold"].eq(name)])["pnlU"]
            if not part.empty
            else 0.0
            for name in PRIMARY_FOLD_NAMES
        }
        mappings = {
            report["name"]: report["mapping"].get(state)
            for report in fold_reports
            if report["name"] in PRIMARY_FOLD_NAMES
        }
        coverage = 100.0 * sum(value is not None for value in mappings.values()) / len(
            PRIMARY_FOLD_NAMES
        )
        passed = bool(
            exact["trades"] >= 90
            and exact["winRatePct"] is not None
            and exact["winRatePct"] > BREAKEVEN_WR
            and fixed_delay["winRatePct"] is not None
            and fixed_delay["winRatePct"] > BREAKEVEN_WR
            and exact["wilson95LowerPct"] is not None
            and exact["wilson95LowerPct"] > BREAKEVEN_WR
            and fixed_delay["wilson95LowerPct"] is not None
            and fixed_delay["wilson95LowerPct"] > BREAKEVEN_WR
            and sum(value > 0.0 for value in fold_pnl.values()) >= 2
            and sum(value > 0.0 for value in fixed_fold_pnl.values()) >= 2
            and coverage == 100.0
        )
        output[state] = {
            "mappings": mappings,
            "selectionCoveragePct": round(coverage, 4),
            "exactBoundary": exact,
            "oneMinuteLateFixedSettlement": fixed_delay,
            "foldPnlU": fold_pnl,
            "fixedDelayFoldPnlU": fixed_fold_pnl,
            "passed": passed,
            "decision": "research_candidate_only" if passed else "no_trade",
        }
    return output


def run(input_path: str | Path, candidate_path: str | Path) -> dict[str, Any]:
    minutes = load_minutes(input_path)
    base_candidates = load_candidates(candidate_path)
    all_stability_rows: list[dict[str, Any]] = []
    window_reports: dict[str, Any] = {}
    for window in VOL_WINDOWS_MIN:
        volatility = build_volatility_states(minutes, window)
        candidates = remap_states(base_candidates, volatility)
        stability_rows = profile_stability_rows(candidates, window)
        all_stability_rows.extend(stability_rows)
        stability = pd.DataFrame(stability_rows)
        fold_reports, trades = walk_forward(candidates)
        window_reports[str(window)] = {
            "volatility": volatility_summary(volatility),
            "familyDiagnostics": family_diagnostics(stability),
            "folds": fold_reports,
            "routeSummary": route_summary(fold_reports, trades),
            "reusedJulyDiagnostic": summarize(
                trades.loc[trades["fold"].eq(REUSED_DIAGNOSTIC_FOLD)]
                if not trades.empty
                else trades
            ),
        }
    stability_table = pd.DataFrame(all_stability_rows)
    stability_table.to_csv(OUT_TABLE, index=False, encoding="utf-8-sig")
    consensus: dict[str, Any] = {}
    for state in STATES:
        strict_by_family: dict[str, int] = {}
        retrospective_by_family: dict[str, int] = {}
        state_rows = stability_table.loc[stability_table["vol_state"].eq(state)]
        for family, group in state_rows.groupby("family", sort=True):
            strict_by_family[str(family)] = int(group["strict_pass"].sum())
            retrospective_by_family[str(family)] = int(
                group["retrospective_candidate"].sum()
            )
        passed_windows = [
            int(window)
            for window, report in window_reports.items()
            if report["routeSummary"][state]["passed"]
        ]
        consensus[state] = {
            "retrospectiveCandidatesByFamilyAcrossAllWindows": retrospective_by_family,
            "strictFixedProfilesByFamilyAcrossAllWindows": strict_by_family,
            "walkForwardPassedVolatilityWindows": passed_windows,
            "decision": "no_trade" if not passed_windows else "research_candidate_only",
        }
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "status": "V17_VOLATILITY_WINDOW_SENSITIVITY",
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
            "candidateRows": int(len(base_candidates)),
            "warning": "All periods have been inspected; July is reused diagnostic, not sealed holdout.",
        },
        "design": {
            "volatilityWindowsMin": list(VOL_WINDOWS_MIN),
            "volatilityHistoryMin": VOL_HISTORY_MIN,
            "volatilityQuantiles": [LOW_QUANTILE, HIGH_QUANTILE],
            "normalAndTrendProfileCount": len(PROFILES),
            "primaryCalendarBlocks": list(PRIMARY_FOLD_NAMES),
            "primaryHorizonMin": PRIMARY_HORIZON_MIN,
            "executionSensitivity": "one-minute late entry with original fixed settlement",
            "strictFixedProfileRule": "both exact and delayed win rate above break-even; all 3 primary folds profitable; at least 90 trades",
            "statisticalConfirmation": "Wilson 95% lower bound must also exceed the 55.56% payout break-even rate",
        },
        "windows": window_reports,
        "consensus": consensus,
        "decision": {
            "deployment": "none",
            "realTradingAllowed": False,
            "note": "Sensitivity evidence only; no new sealed out-of-sample period remains.",
        },
        "outputs": {"json": str(OUT_JSON), "profileStabilityCsv": str(OUT_TABLE)},
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
            clean({
                "consensus": report["consensus"],
                "routeSummary": {
                    window: value["routeSummary"]
                    for window, value in report["windows"].items()
                },
                "outputs": report["outputs"],
            }),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
