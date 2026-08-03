"""Compare reversion, confirmed reversal and continuation by volatility state."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research_minute_volatility_normal_v15 import (
    AMOUNT_U,
    BREAKEVEN_WR,
    PAYOUT_RATE,
    STATES,
    _boundary_mask,
    apply_shared_cooldown,
    build_normal_features,
    build_volatility_states,
    clean,
    load_minutes,
    wilson_lower,
)


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "data" / "btcusdt_futures_1m_20260131_20260730.csv"
OUT_JSON = ROOT / "tmp" / "v16_multiregime_strategy_20260730.json"
OUT_TRADES = ROOT / "tmp" / "v16_multiregime_strategy_20260730_trades.csv"
OUT_CANDIDATES = ROOT / "tmp" / "v16_multiregime_strategy_20260730_candidates.csv"
HORIZONS_MIN = (5, 10, 20)
DELAYS_MIN = (0, 1)


@dataclass(frozen=True)
class StrategyProfile:
    name: str
    family: str
    lookback_min: int
    threshold: float
    reclaim_z: float = 0.75


PROFILES = tuple(
    [
        StrategyProfile(
            f"normal_edge_w{window}_z{str(level).replace('.', 'p')}",
            "normal_edge_reversion",
            window,
            level,
        )
        for window in (10, 20, 30, 60, 120)
        for level in (1.5, 2.0, 2.5)
    ]
    + [
        StrategyProfile(
            f"normal_reclaim_w{window}_z{str(level).replace('.', 'p')}",
            "normal_confirmed_reversal",
            window,
            level,
        )
        for window in (10, 20, 30, 60, 120)
        for level in (1.5, 2.0, 2.5)
    ]
    + [
        StrategyProfile(
            f"momentum_w{window}_s{str(level).replace('.', 'p')}",
            "trend_continuation",
            window,
            level,
        )
        for window in (5, 10, 20, 30)
        for level in (1.0, 1.5, 2.0)
    ]
    + [
        StrategyProfile(
            f"exhaustion_w{window}_s{str(level).replace('.', 'p')}",
            "trend_exhaustion_reversal",
            window,
            level,
        )
        for window in (30, 60, 120)
        for level in (1.5, 2.0, 2.5)
    ]
)


FOLDS = (
    ("2026-04", pd.Timestamp("2026-04-01T00:00:00Z"), pd.Timestamp("2026-05-01T00:00:00Z")),
    ("2026-05", pd.Timestamp("2026-05-01T00:00:00Z"), pd.Timestamp("2026-06-01T00:00:00Z")),
    ("2026-06", pd.Timestamp("2026-06-01T00:00:00Z"), pd.Timestamp("2026-07-01T00:00:00Z")),
    ("2026-07_reused_diagnostic", pd.Timestamp("2026-07-01T00:00:00Z"), pd.Timestamp("2026-07-30T00:00:00Z")),
)
PRIMARY_FOLD_NAMES = ("2026-04", "2026-05", "2026-06")
REUSED_DIAGNOSTIC_FOLD = "2026-07_reused_diagnostic"


def _score_return(minutes: pd.DataFrame, lookback: int) -> pd.DataFrame:
    close = minutes["close"].astype(float)
    log_return = np.log(close / close.shift(1))
    move = np.log(close / close.shift(lookback)) * 10_000.0
    scale = log_return.rolling(60, min_periods=60).std(ddof=0) * math.sqrt(lookback) * 10_000.0
    return pd.DataFrame(
        {
            "move_bps": move,
            "move_score": move / scale.replace(0.0, np.nan),
            "ret_1m_bps": log_return * 10_000.0,
            "ret_3m_bps": np.log(close / close.shift(3)) * 10_000.0,
        },
        index=minutes.index,
    )


def _signal_masks(
    minutes: pd.DataFrame,
    profile: StrategyProfile,
) -> tuple[pd.Series, pd.Series, pd.DataFrame]:
    if profile.family in {"normal_edge_reversion", "normal_confirmed_reversal"}:
        normal = build_normal_features(
            minutes,
            # Importing the V15 dataclass would couple the grids; this simple
            # compatible object keeps the V16 profile declaration authoritative.
            type("P", (), {
                "window_min": profile.lookback_min,
                "retest_min": max(5, profile.lookback_min // 3),
            })(),
        )
        # V16 deliberately compares structure without the V15 normal-shape
        # gate; the z centre/sigma remain strictly prior-minute estimates.
        if profile.family == "normal_edge_reversion":
            up = normal["z"].le(-profile.threshold)
            down = normal["z"].ge(profile.threshold)
        else:
            up = (
                normal["past_z_min"].le(-profile.threshold)
                & normal["z"].between(-profile.reclaim_z, 0.0)
                & normal["ret_1m_bps"].gt(0.0)
            )
            down = (
                normal["past_z_max"].ge(profile.threshold)
                & normal["z"].between(0.0, profile.reclaim_z)
                & normal["ret_1m_bps"].lt(0.0)
            )
        diagnostics = normal[["z", "ret_1m_bps", "ret_30m_bps", "ret_120m_bps"]].copy()
        diagnostics["structure_score"] = normal["z"]
        return up, down, diagnostics

    returns = _score_return(minutes, profile.lookback_min)
    if profile.family == "trend_continuation":
        up = returns["move_score"].ge(profile.threshold)
        down = returns["move_score"].le(-profile.threshold)
    elif profile.family == "trend_exhaustion_reversal":
        long_move = returns["move_bps"]
        short_move = returns["ret_3m_bps"]
        retraced = long_move.mul(short_move).lt(0.0) & short_move.abs().ge(long_move.abs() * 0.15)
        up = returns["move_score"].le(-profile.threshold) & retraced
        down = returns["move_score"].ge(profile.threshold) & retraced
    else:
        raise ValueError(profile.family)
    diagnostics = returns.rename(columns={"move_score": "structure_score"})
    diagnostics["z"] = np.nan
    diagnostics["ret_30m_bps"] = np.log(
        minutes["close"].astype(float) / minutes["close"].astype(float).shift(30)
    ) * 10_000.0
    diagnostics["ret_120m_bps"] = np.log(
        minutes["close"].astype(float) / minutes["close"].astype(float).shift(120)
    ) * 10_000.0
    return up, down, diagnostics


def generate_candidates(
    minutes: pd.DataFrame,
    volatility: pd.DataFrame,
    profile: StrategyProfile,
) -> pd.DataFrame:
    up, down, diagnostics = _signal_masks(minutes, profile)
    selected = (up | down) & _boundary_mask(minutes.index)
    selected &= volatility["vol_state"].isin(STATES)
    positions = np.flatnonzero(selected.to_numpy(bool))
    opens = minutes["open"].to_numpy(float)
    rows: list[dict[str, Any]] = []
    for position in positions:
        signal = "UP" if bool(up.iloc[position]) else "DOWN"
        direction = 1.0 if signal == "UP" else -1.0
        row: dict[str, Any] = {
            "profile": profile.name,
            "family": profile.family,
            "lookback_min": profile.lookback_min,
            "threshold": profile.threshold,
            "signal_bar_time": minutes.index[position],
            "signal_time": minutes.index[position] + pd.Timedelta(minutes=1),
            "signal": signal,
            "vol_state": str(volatility["vol_state"].iloc[position]),
            "rv10m_bps": float(volatility["rv10m_bps"].iloc[position]),
            "structure_score": float(diagnostics["structure_score"].iloc[position]),
            "z": float(diagnostics["z"].iloc[position]) if pd.notna(diagnostics["z"].iloc[position]) else np.nan,
            "ret_30m_bps": float(diagnostics["ret_30m_bps"].iloc[position]),
            "ret_120m_bps": float(diagnostics["ret_120m_bps"].iloc[position]),
        }
        for horizon in HORIZONS_MIN:
            for delay in DELAYS_MIN:
                entry_position = position + 1 + delay
                settle_position = entry_position + horizon
                suffix = f"h{horizon}_d{delay}"
                if settle_position >= len(minutes):
                    row[f"entry_time_{suffix}"] = (
                        minutes.index[entry_position] if entry_position < len(minutes) else pd.NaT
                    )
                    row[f"settle_time_{suffix}"] = pd.NaT
                    row[f"entry_{suffix}"] = (
                        float(opens[entry_position]) if entry_position < len(minutes) else np.nan
                    )
                    row[f"settle_{suffix}"] = np.nan
                    row[f"status_{suffix}"] = "missing"
                    row[f"pnl_u_{suffix}"] = np.nan
                    continue
                entry = float(opens[entry_position])
                settle = float(opens[settle_position])
                signed = (settle / entry - 1.0) * 10_000.0 * direction
                row[f"entry_time_{suffix}"] = minutes.index[entry_position]
                row[f"settle_time_{suffix}"] = minutes.index[settle_position]
                row[f"entry_{suffix}"] = entry
                row[f"settle_{suffix}"] = settle
                row[f"signed_bps_{suffix}"] = signed
                row[f"status_{suffix}"] = "won" if signed > 0.0 else "lost" if signed < 0.0 else "tie"
                row[f"pnl_u_{suffix}"] = AMOUNT_U * PAYOUT_RATE if signed > 0.0 else -AMOUNT_U if signed < 0.0 else 0.0
        rows.append(row)
    return pd.DataFrame(rows)


def metrics(
    frame: pd.DataFrame,
    horizon: int = 10,
    delay: int = 0,
    *,
    period_start: pd.Timestamp | None = None,
    period_end: pd.Timestamp | None = None,
) -> dict[str, Any]:
    suffix = f"h{horizon}_d{delay}"
    if frame.empty:
        return {
            "trades": 0, "wins": 0, "losses": 0, "ties": 0,
            "winRatePct": None, "wilson95LowerPct": None, "pnlU": 0.0,
            "expectedValueU": None, "maxDrawdownU": 0.0,
            "maxLossStreak": 0, "positiveMonthPct": None,
            "months": 0, "worstMonthPnlU": None, "medianMonthPnlU": None,
        }
    mask = frame[f"status_{suffix}"].isin(("won", "lost", "tie"))
    entry_column = f"entry_time_{suffix}"
    settle_column = f"settle_time_{suffix}"
    if period_start is not None and entry_column in frame.columns:
        entry_time = pd.to_datetime(frame[entry_column], utc=True, errors="coerce")
        mask &= entry_time.ge(period_start)
    if period_end is not None and settle_column in frame.columns:
        settle_time = pd.to_datetime(frame[settle_column], utc=True, errors="coerce")
        mask &= settle_time.lt(period_end)
    settled = frame.loc[mask].copy()
    if settled.empty:
        return metrics(pd.DataFrame(), horizon, delay)
    status = settled[f"status_{suffix}"]
    wins = int(status.eq("won").sum())
    losses = int(status.eq("lost").sum())
    ties = int(status.eq("tie").sum())
    decided = wins + losses
    pnl = pd.to_numeric(settled[f"pnl_u_{suffix}"], errors="coerce").fillna(0.0).to_numpy(float)
    equity = np.r_[0.0, np.cumsum(pnl)]
    drawdown = np.maximum.accumulate(equity) - equity
    streak = maximum = 0
    for value in status:
        streak = streak + 1 if value == "lost" else 0
        maximum = max(maximum, streak)
    month = pd.to_datetime(settled["signal_time"], utc=True).dt.strftime("%Y-%m")
    monthly = pd.Series(pnl).groupby(month.reset_index(drop=True)).sum()
    lower = wilson_lower(wins, decided)
    return {
        "trades": int(len(settled)),
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "winRatePct": round(wins / decided * 100.0, 4) if decided else None,
        "wilson95LowerPct": round(lower * 100.0, 4) if lower is not None else None,
        "pnlU": round(float(pnl.sum()), 4),
        "expectedValueU": round(float(pnl.mean()), 6),
        "maxDrawdownU": round(float(drawdown.max()), 4),
        "maxLossStreak": maximum,
        "positiveMonthPct": round(float(monthly.gt(0.0).mean()) * 100.0, 4) if len(monthly) else None,
        "months": int(len(monthly)),
        "worstMonthPnlU": round(float(monthly.min()), 4) if len(monthly) else None,
        "medianMonthPnlU": round(float(monthly.median()), 4) if len(monthly) else None,
    }


def select_for_state(candidates: pd.DataFrame, state: str, train_end: pd.Timestamp) -> dict[str, Any] | None:
    if "settle_time_h10_d0" in candidates.columns:
        settled_before_cutoff = pd.to_datetime(
            candidates["settle_time_h10_d0"], utc=True, errors="coerce"
        ).lt(train_end)
    else:
        # Synthetic/unit-test rows may omit explicit timestamps. The primary
        # label always settles ten minutes after signal_time.
        settled_before_cutoff = (
            pd.to_datetime(candidates["signal_time"], utc=True, errors="coerce")
            + pd.Timedelta(minutes=10)
        ).lt(train_end)
    pool = candidates.loc[candidates["vol_state"].eq(state) & settled_before_cutoff]
    ranked = []
    for profile, group in pool.groupby("profile", sort=True):
        executed = apply_shared_cooldown(group)
        summary = metrics(executed, 10, 0)
        delayed = metrics(executed, 10, 1) if "status_h10_d1" in executed.columns else summary
        minimum_months = 2 if train_end <= pd.Timestamp("2026-04-01T00:00:00Z") else 3
        eligible = (
            summary["trades"] >= 60
            and summary["months"] >= minimum_months
            and delayed["trades"] >= 60
            and delayed["months"] >= minimum_months
            and summary["pnlU"] > 0.0
            and delayed["pnlU"] > 0.0
            and summary["winRatePct"] is not None
            and summary["winRatePct"] > BREAKEVEN_WR
            and delayed["winRatePct"] is not None
            and delayed["winRatePct"] > BREAKEVEN_WR
            and summary["positiveMonthPct"] is not None
            and summary["positiveMonthPct"] >= (200.0 / 3.0)
            and delayed["positiveMonthPct"] is not None
            and delayed["positiveMonthPct"] >= (200.0 / 3.0)
            and summary["worstMonthPnlU"] is not None
            and summary["worstMonthPnlU"] >= -25.0
            and delayed["worstMonthPnlU"] is not None
            and delayed["worstMonthPnlU"] >= -25.0
        )
        if not eligible:
            continue
        score = (
            min(float(summary["positiveMonthPct"]), float(delayed["positiveMonthPct"])),
            min(float(summary["medianMonthPnlU"]), float(delayed["medianMonthPnlU"])),
            min(
                float(summary["wilson95LowerPct"] or 0.0),
                float(delayed["wilson95LowerPct"] or 0.0),
            ),
            -max(float(summary["maxDrawdownU"]), float(delayed["maxDrawdownU"])),
        )
        family = str(group["family"].iloc[0])
        ranked.append((score, str(profile), family, summary, delayed))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    score, profile, family, summary, delayed = ranked[0]
    return {
        "profile": profile,
        "family": family,
        "trainMetrics": summary,
        "trainOneMinuteDelay": delayed,
        "score": list(score),
    }


def mapped_test(
    candidates: pd.DataFrame,
    mapping: dict[str, str | None],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    parts = []
    for state, profile in mapping.items():
        if profile is None:
            continue
        parts.append(candidates.loc[
            candidates["vol_state"].eq(state)
            & candidates["profile"].eq(profile)
            & candidates["signal_time"].ge(start)
            & candidates["signal_time"].lt(end)
        ])
    frame = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=candidates.columns)
    return apply_shared_cooldown(frame)


def summarize(
    frame: pd.DataFrame,
    *,
    period_start: pd.Timestamp | None = None,
    period_end: pd.Timestamp | None = None,
) -> dict[str, Any]:
    return {
        "byHorizonDelay": {
            f"h{horizon}_d{delay}": metrics(
                frame,
                horizon,
                delay,
                period_start=period_start,
                period_end=period_end,
            )
            for horizon in HORIZONS_MIN
            for delay in DELAYS_MIN
        },
        "byStateH10D0": {
            str(state): metrics(
                group, 10, 0, period_start=period_start, period_end=period_end
            )
            for state, group in frame.groupby("vol_state", sort=True)
        } if not frame.empty else {},
        "byFamilyH10D0": {
            str(family): metrics(
                group, 10, 0, period_start=period_start, period_end=period_end
            )
            for family, group in frame.groupby("family", sort=True)
        } if not frame.empty else {},
        "byDirectionH10D0": {
            str(signal): metrics(
                group, 10, 0, period_start=period_start, period_end=period_end
            )
            for signal, group in frame.groupby("signal", sort=True)
        } if not frame.empty else {},
    }


def run(input_path: str | Path) -> dict[str, Any]:
    minutes = load_minutes(input_path)
    volatility = build_volatility_states(minutes)
    candidates = pd.concat(
        [generate_candidates(minutes, volatility, profile) for profile in PROFILES],
        ignore_index=True,
    ).sort_values(["signal_time", "profile"], kind="stable").reset_index(drop=True)
    fold_reports = []
    test_parts = []
    for name, start, end in FOLDS:
        selection = {
            state: select_for_state(candidates, state, start)
            for state in STATES
        }
        mapping = {
            state: item["profile"] if item is not None else None
            for state, item in selection.items()
        }
        trades = mapped_test(candidates, mapping, start, end)
        if not trades.empty:
            tagged = trades.copy()
            tagged["fold"] = name
            test_parts.append(tagged)
        fold_reports.append({
            "name": name,
            "trainEndExclusive": start,
            "testStart": start,
            "testEndExclusive": end,
            "selection": selection,
            "mapping": mapping,
            "test": summarize(trades, period_start=start, period_end=end),
        })
    all_test = pd.concat(test_parts, ignore_index=True) if test_parts else pd.DataFrame(columns=candidates.columns)
    historical = summarize(all_test)
    primary_test = all_test.loc[all_test["fold"].isin(PRIMARY_FOLD_NAMES)].copy()
    reused_diagnostic = all_test.loc[all_test["fold"].eq(REUSED_DIAGNOSTIC_FOLD)].copy()
    state_recommendations = {}
    for state in STATES:
        part = primary_test.loc[primary_test["vol_state"].eq(state)]
        h10 = metrics(part, 10, 0)
        delayed = metrics(part, 10, 1)
        fold_pnls = {
            fold: metrics(part.loc[part["fold"].eq(fold)], 10, 0)["pnlU"]
            for fold in PRIMARY_FOLD_NAMES
        }
        delayed_fold_pnls = {
            fold: metrics(part.loc[part["fold"].eq(fold)], 10, 1)["pnlU"]
            for fold in PRIMARY_FOLD_NAMES
        }
        positive_fold_pct = 100.0 * sum(
            value > 0.0 for value in fold_pnls.values()
        ) / len(PRIMARY_FOLD_NAMES)
        delayed_positive_fold_pct = 100.0 * sum(
            value > 0.0 for value in delayed_fold_pnls.values()
        ) / len(PRIMARY_FOLD_NAMES)
        selected_folds = sum(
            fold["mapping"].get(state) is not None
            for fold in fold_reports
            if fold["name"] in PRIMARY_FOLD_NAMES
        )
        selection_coverage_pct = 100.0 * selected_folds / len(PRIMARY_FOLD_NAMES)
        passed = (
            h10["trades"] >= 100
            and h10["pnlU"] > 0.0
            and delayed["pnlU"] > 0.0
            and h10["winRatePct"] is not None
            and h10["winRatePct"] > BREAKEVEN_WR
            and h10["wilson95LowerPct"] is not None
            and h10["wilson95LowerPct"] > BREAKEVEN_WR
            and positive_fold_pct >= (200.0 / 3.0)
            and delayed_positive_fold_pct >= (200.0 / 3.0)
            and selection_coverage_pct == 100.0
        )
        state_recommendations[state] = {
            "historicalWalkForward": h10,
            "oneMinuteDelay": delayed,
            "foldPnlU": fold_pnls,
            "oneMinuteDelayFoldPnlU": delayed_fold_pnls,
            "positiveFoldPct": round(positive_fold_pct, 4),
            "oneMinuteDelayPositiveFoldPct": round(delayed_positive_fold_pct, 4),
            "selectionCoveragePct": round(selection_coverage_pct, 4),
            "decision": "research_candidate_only" if passed else "no_trade",
            "passed": passed,
        }
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "status": "V16_MULTI_REGIME_HISTORICAL_WALKFORWARD",
        "safety": {
            "researchOnly": True,
            "tradeEnabled": False,
            "realTradingAllowed": False,
            "deploymentPerformed": False,
        },
        "data": {
            "input": str(Path(input_path).resolve()),
            "rows": int(len(minutes)),
            "start": minutes.index[0],
            "end": minutes.index[-1],
            "warning": "July was already opened by V15 and is a reused diagnostic, not a new sealed holdout.",
        },
        "design": {
            "profileCount": len(PROFILES),
            "profiles": [asdict(profile) for profile in PROFILES],
            "normalWindowsMin": [10, 20, 30, 60, 120],
            "strategyFamilies": sorted({profile.family for profile in PROFILES}),
            "horizonsMin": list(HORIZONS_MIN),
            "delaysMin": list(DELAYS_MIN),
            "primaryContractHorizonMin": 10,
            "selection": "training history only; monthly stability gate; no random split",
        },
        "candidateRows": int(len(candidates)),
        "folds": fold_reports,
        "historicalWalkForward": historical,
        "primaryWalkForward": summarize(primary_test),
        "reusedJulyDiagnostic": summarize(reused_diagnostic),
        "stateRecommendations": state_recommendations,
        "decision": {
            "passedStates": [state for state, row in state_recommendations.items() if row["passed"]],
            "deployment": "none",
            "realTradingAllowed": False,
        },
        "outputs": {
            "json": str(OUT_JSON),
            "trades": str(OUT_TRADES),
            "candidates": str(OUT_CANDIDATES),
        },
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    candidates.to_csv(OUT_CANDIDATES, index=False, encoding="utf-8-sig")
    all_test.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(INPUT))
    args = parser.parse_args()
    report = run(args.input)
    print(json.dumps(clean({
        "folds": report["folds"],
        "historicalWalkForward": report["historicalWalkForward"],
        "stateRecommendations": report["stateRecommendations"],
        "decision": report["decision"],
        "outputs": report["outputs"],
    }), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
