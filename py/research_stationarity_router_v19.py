"""V19 causal volatility × stationarity router with block-bootstrap gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd

from research_multiregime_strategy_v16 import (
    AMOUNT_U,
    BREAKEVEN_WR,
    DELAYS_MIN,
    FOLDS,
    HORIZONS_MIN,
    PAYOUT_RATE,
    _boundary_mask,
    _score_return,
    apply_shared_cooldown,
    build_normal_features,
    clean,
    load_minutes,
    metrics,
)
from research_volatility_window_sensitivity_v17 import (
    INPUT,
    build_volatility_states,
)
from stationarity_features_v19 import (
    ESTIMATION_WINDOW_MIN,
    build_stationarity_features,
)


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "tmp" / "v19_stationarity_router_20260730.json"
OUT_CANDIDATES = ROOT / "tmp" / "v19_stationarity_router_candidates_20260730.csv"
OUT_TRADES = ROOT / "tmp" / "v19_stationarity_router_trades_20260730.csv"
OUT_PROFILE_AUDIT = ROOT / "tmp" / "v19_stationarity_router_profile_audit_20260730.csv"

VOLATILITY_WINDOW_MIN = 120
BOOTSTRAP_SAMPLES = 10_000
BOOTSTRAP_LOWER_QUANTILE = 0.10
PRIMARY_EVALUATION_FOLDS = ("2026-05", "2026-06")
REUSED_DIAGNOSTIC_FOLD = "2026-07_reused_diagnostic"


@dataclass(frozen=True)
class V19Profile:
    name: str
    family: str
    lookback_min: int
    threshold: float


PROFILES = (
    V19Profile("v19_edge_w60_z2p0", "normal_edge_reversion", 60, 2.0),
    V19Profile("v19_edge_w120_z2p0", "normal_edge_reversion", 120, 2.0),
    V19Profile("v19_reclaim_w60_z2p0", "normal_confirmed_reversal", 60, 2.0),
    V19Profile("v19_reclaim_w120_z2p0", "normal_confirmed_reversal", 120, 2.0),
    V19Profile("v19_momentum_w30_s1p5", "trend_continuation", 30, 1.5),
    V19Profile("v19_momentum_w60_s1p5", "trend_continuation", 60, 1.5),
    V19Profile("v19_exhaustion_w60_s2p0", "trend_exhaustion_reversal", 60, 2.0),
    V19Profile("v19_exhaustion_w120_s2p0", "trend_exhaustion_reversal", 120, 2.0),
)

CELLS = (
    "low|revertible",
    "mid|revertible",
    "high|revertible",
    "low|trend",
    "mid|trend",
    "high|trend",
)


def _signal_masks(
    minutes: pd.DataFrame,
    profile: V19Profile,
) -> tuple[pd.Series, pd.Series, pd.DataFrame]:
    if profile.family in {"normal_edge_reversion", "normal_confirmed_reversal"}:
        normal = build_normal_features(
            minutes,
            SimpleNamespace(
                window_min=profile.lookback_min,
                retest_min=max(10, profile.lookback_min // 3),
            ),
        )
        if profile.family == "normal_edge_reversion":
            up = normal["z"].le(-profile.threshold)
            down = normal["z"].ge(profile.threshold)
        else:
            rebound_up = normal["z"] - normal["past_z_min"]
            rebound_down = normal["past_z_max"] - normal["z"]
            up = (
                normal["past_z_min"].le(-profile.threshold)
                & rebound_up.ge(0.5)
                & normal["z"].between(-1.5, -0.5)
                & normal["ret_1m_bps"].gt(0.0)
            )
            down = (
                normal["past_z_max"].ge(profile.threshold)
                & rebound_down.ge(0.5)
                & normal["z"].between(0.5, 1.5)
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
        counter_move = returns["ret_3m_bps"]
        retraced = long_move.mul(counter_move).lt(0.0) & counter_move.abs().ge(
            long_move.abs() * 0.15
        )
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


def _profile_allowed(profile: V19Profile, volatility: str, structure: str) -> bool:
    if structure == "revertible":
        if volatility in {"low", "mid"}:
            return profile.family in {
                "normal_edge_reversion",
                "normal_confirmed_reversal",
            }
        if volatility == "high":
            return profile.family == "normal_confirmed_reversal"
    if structure == "trend":
        if volatility in {"low", "mid"}:
            return profile.family == "trend_continuation"
        if volatility == "high":
            return profile.family == "trend_exhaustion_reversal"
    return False


def generate_candidates(
    minutes: pd.DataFrame,
    volatility: pd.DataFrame,
    stationarity: pd.DataFrame,
    profile: V19Profile,
) -> pd.DataFrame:
    up, down, diagnostics = _signal_masks(minutes, profile)
    selected = (up | down) & _boundary_mask(minutes.index)
    selected &= volatility["vol_state"].isin(("low", "mid", "high"))
    selected &= stationarity["structure_state"].isin(("revertible", "trend"))
    positions = np.flatnonzero(selected.to_numpy(bool))
    opens = minutes["open"].to_numpy(float)
    rows: list[dict[str, Any]] = []
    for position in positions:
        vol_state = str(volatility["vol_state"].iloc[position])
        structure_state = str(stationarity["structure_state"].iloc[position])
        if not _profile_allowed(profile, vol_state, structure_state):
            continue
        signal = "UP" if bool(up.iloc[position]) else "DOWN"
        direction = 1.0 if signal == "UP" else -1.0
        row: dict[str, Any] = {
            "profile": profile.name,
            "family": profile.family,
            "lookback_min": profile.lookback_min,
            "threshold": profile.threshold,
            "cell": f"{vol_state}|{structure_state}",
            "vol_state": vol_state,
            "structure_state": structure_state,
            "signal_bar_time": minutes.index[position],
            "signal_time": minutes.index[position] + pd.Timedelta(minutes=1),
            "signal": signal,
            "rv10m_bps": float(volatility["rv10m_bps"].iloc[position]),
            "structure_score": float(diagnostics["structure_score"].iloc[position]),
            "z": float(diagnostics["z"].iloc[position])
            if pd.notna(diagnostics["z"].iloc[position])
            else np.nan,
            "efficiency60": float(stationarity["efficiency60"].iloc[position]),
            "momentum60_score": float(stationarity["momentum60_score"].iloc[position]),
            "variance_ratio10": float(stationarity["variance_ratio10"].iloc[position]),
            "half_life_min": float(stationarity["half_life_min"].iloc[position]),
            "adf_t_beta": float(stationarity["adf_t_beta"].iloc[position]),
            "shock_max10": float(stationarity["shock_max10"].iloc[position]),
        }
        for horizon in HORIZONS_MIN:
            for delay in DELAYS_MIN:
                entry_position = position + 1 + delay
                settle_position = entry_position + horizon
                suffix = f"h{horizon}_d{delay}"
                if settle_position >= len(minutes):
                    row[f"entry_time_{suffix}"] = pd.NaT
                    row[f"settle_time_{suffix}"] = pd.NaT
                    row[f"entry_{suffix}"] = np.nan
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
                row[f"status_{suffix}"] = (
                    "won" if signed > 0.0 else "lost" if signed < 0.0 else "tie"
                )
                row[f"pnl_u_{suffix}"] = (
                    AMOUNT_U * PAYOUT_RATE
                    if signed > 0.0
                    else -AMOUNT_U
                    if signed < 0.0
                    else 0.0
                )

        fixed_entry_position = position + 2
        fixed_settle_position = position + 11
        if fixed_settle_position < len(minutes):
            fixed_entry = float(opens[fixed_entry_position])
            fixed_settle = float(opens[fixed_settle_position])
            fixed_signed = (
                (fixed_settle / fixed_entry - 1.0) * 10_000.0 * direction
            )
            row["entry_time_h10_fixed_d1"] = minutes.index[fixed_entry_position]
            row["settle_time_h10_fixed_d1"] = minutes.index[fixed_settle_position]
            row["signed_bps_h10_fixed_d1"] = fixed_signed
            row["status_h10_fixed_d1"] = (
                "won"
                if fixed_signed > 0.0
                else "lost"
                if fixed_signed < 0.0
                else "tie"
            )
            row["pnl_u_h10_fixed_d1"] = (
                AMOUNT_U * PAYOUT_RATE
                if fixed_signed > 0.0
                else -AMOUNT_U
                if fixed_signed < 0.0
                else 0.0
            )
        rows.append(row)
    return pd.DataFrame(rows)


def fixed_metrics(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return metrics(pd.DataFrame(), 10, 0)
    alias = pd.DataFrame(
        {
            "signal_time": frame["signal_time"].to_numpy(),
            "entry_time_h10_d0": frame["entry_time_h10_fixed_d1"].to_numpy(),
            "settle_time_h10_d0": frame["settle_time_h10_fixed_d1"].to_numpy(),
            "status_h10_d0": frame["status_h10_fixed_d1"].to_numpy(),
            "pnl_u_h10_d0": frame["pnl_u_h10_fixed_d1"].to_numpy(),
        }
    )
    return metrics(alias, 10, 0)


def _execution_columns(mode: str) -> tuple[str, str]:
    if mode == "exact":
        return "status_h10_d0", "pnl_u_h10_d0"
    if mode == "fixed":
        return "status_h10_fixed_d1", "pnl_u_h10_fixed_d1"
    raise ValueError(mode)


def _training_months(train_end: pd.Timestamp) -> list[str]:
    last = (train_end - pd.Timedelta(seconds=1)).strftime("%Y-%m")
    return [period.strftime("%Y-%m") for period in pd.period_range("2026-02", last, freq="M")]


def _bootstrap_block_ev(
    frame: pd.DataFrame,
    pnl_column: str,
    *,
    seed_key: str,
) -> dict[str, Any]:
    settled = frame.loc[pd.to_numeric(frame[pnl_column], errors="coerce").notna()].copy()
    if settled.empty:
        return {"blocks": 0, "lower90EvU": None, "probabilityEvNonPositive": None}
    signal_time = pd.to_datetime(settled["signal_time"], utc=True)
    epoch_days = (
        signal_time.to_numpy(dtype="datetime64[ns]").astype(np.int64)
        // (24 * 60 * 60 * 1_000_000_000)
    )
    settled["block"] = epoch_days // 7
    block = settled.groupby("block")[pnl_column].agg(["sum", "count"])
    if len(block) < 4:
        return {"blocks": int(len(block)), "lower90EvU": None, "probabilityEvNonPositive": None}
    digest = hashlib.sha256(seed_key.encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], "little", signed=False)
    rng = np.random.default_rng(seed)
    indices = rng.integers(0, len(block), size=(BOOTSTRAP_SAMPLES, len(block)))
    sums = block["sum"].to_numpy(float)[indices].sum(axis=1)
    counts = block["count"].to_numpy(float)[indices].sum(axis=1)
    ev = sums / counts
    return {
        "blocks": int(len(block)),
        "lower90EvU": round(float(np.quantile(ev, BOOTSTRAP_LOWER_QUANTILE)), 6),
        "probabilityEvNonPositive": round(float(np.mean(ev <= 0.0)), 6),
    }


def training_summary(
    frame: pd.DataFrame,
    mode: str,
    train_end: pd.Timestamp,
    *,
    seed_key: str,
) -> dict[str, Any]:
    summary = metrics(frame, 10, 0) if mode == "exact" else fixed_metrics(frame)
    status_column, pnl_column = _execution_columns(mode)
    settled = frame.loc[frame[status_column].isin(("won", "lost", "tie"))].copy()
    months = _training_months(train_end)
    month = pd.to_datetime(settled["signal_time"], utc=True).dt.strftime("%Y-%m")
    pnl = pd.to_numeric(settled[pnl_column], errors="coerce").fillna(0.0)
    monthly_pnl = pnl.groupby(month).sum().reindex(months, fill_value=0.0)
    monthly_trades = settled.groupby(month).size().reindex(months, fill_value=0)
    summary.update(
        {
            "fixedCalendarMonths": months,
            "fixedPositiveMonthPct": round(float(monthly_pnl.gt(0.0).mean()) * 100.0, 4)
            if months
            else None,
            "fixedWorstMonthPnlU": round(float(monthly_pnl.min()), 4)
            if months
            else None,
            "monthsWithAtLeast20Trades": int(monthly_trades.ge(20).sum()),
            "monthlyPnlU": {str(key): round(float(value), 4) for key, value in monthly_pnl.items()},
            "monthlyTrades": {str(key): int(value) for key, value in monthly_trades.items()},
            "bootstrap": _bootstrap_block_ev(frame, pnl_column, seed_key=seed_key),
        }
    )
    return summary


def _eligible_training(
    exact: dict[str, Any],
    fixed: dict[str, Any],
    frame: pd.DataFrame,
) -> bool:
    if len(exact["fixedCalendarMonths"]) < 3:
        return False
    required_positive_pct = 100.0 * math.ceil(
        2.0 * len(exact["fixedCalendarMonths"]) / 3.0
    ) / len(exact["fixedCalendarMonths"])
    execution_ok = all(
        row["trades"] >= 90
        and row["monthsWithAtLeast20Trades"] >= 2
        and row["pnlU"] > 0.0
        and row["winRatePct"] is not None
        and row["winRatePct"] > BREAKEVEN_WR
        and row["fixedPositiveMonthPct"] >= required_positive_pct
        and row["bootstrap"]["lower90EvU"] is not None
        and row["bootstrap"]["lower90EvU"] > 0.0
        for row in (exact, fixed)
    )
    if not execution_ok:
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
    train_end: pd.Timestamp,
) -> dict[str, Any] | None:
    if len(_training_months(train_end)) < 3:
        return None
    known = pd.to_datetime(
        candidates["settle_time_h10_d0"], utc=True, errors="coerce"
    ).lt(train_end)
    known &= pd.to_datetime(
        candidates["settle_time_h10_fixed_d1"], utc=True, errors="coerce"
    ).lt(train_end)
    pool = candidates.loc[candidates["cell"].eq(cell) & known]
    ranked = []
    for profile, raw_group in pool.groupby("profile", sort=True):
        group = apply_shared_cooldown(raw_group)
        exact = training_summary(
            group, "exact", train_end, seed_key=f"{cell}|{profile}|{train_end}|exact"
        )
        fixed = training_summary(
            group, "fixed", train_end, seed_key=f"{cell}|{profile}|{train_end}|fixed"
        )
        if not _eligible_training(exact, fixed, group):
            continue
        meta = next(item for item in PROFILES if item.name == profile)
        score = (
            min(exact["bootstrap"]["lower90EvU"], fixed["bootstrap"]["lower90EvU"]),
            min(exact["expectedValueU"], fixed["expectedValueU"]),
            meta.lookback_min,
            -max(exact["trades"], fixed["trades"]),
        )
        ranked.append((score, profile, meta, exact, fixed))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    score, profile, meta, exact, fixed = ranked[0]
    return {
        "profile": profile,
        "family": meta.family,
        "lookbackMin": meta.lookback_min,
        "trainExact": exact,
        "trainFixedSettlement": fixed,
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


def execution_summary(frame: pd.DataFrame) -> dict[str, Any]:
    return {
        "exact": metrics(frame, 10, 0),
        "shiftedOneMinute": metrics(frame, 10, 1),
        "fixedSettlementOneMinute": fixed_metrics(frame),
        "h5Exact": metrics(frame, 5, 0),
        "h20Exact": metrics(frame, 20, 0),
    }


def walk_forward(candidates: pd.DataFrame) -> tuple[list[dict[str, Any]], pd.DataFrame]:
    reports = []
    trade_parts = []
    for name, start, end in FOLDS:
        selection = {cell: select_profile(candidates, cell, start) for cell in CELLS}
        mapping = {
            cell: item["profile"] if item is not None else None
            for cell, item in selection.items()
        }
        trades = mapped_test(candidates, mapping, start, end)
        if not trades.empty:
            tagged = trades.copy()
            tagged["fold"] = name
            trade_parts.append(tagged)
        reports.append(
            {
                "name": name,
                "trainingMonths": _training_months(start),
                "selection": selection,
                "mapping": mapping,
                "test": execution_summary(trades),
            }
        )
    all_trades = (
        pd.concat(trade_parts, ignore_index=True)
        if trade_parts
        else pd.DataFrame(columns=[*candidates.columns, "fold"])
    )
    return reports, all_trades


def _holm_adjust(pvalues: dict[str, float]) -> dict[str, float]:
    ordered = sorted(pvalues.items(), key=lambda item: item[1])
    adjusted: dict[str, float] = {}
    running = 0.0
    total = len(ordered)
    for rank, (key, value) in enumerate(ordered):
        candidate = min(1.0, (total - rank) * value)
        running = max(running, candidate)
        adjusted[key] = round(running, 6)
    return adjusted


def final_cell_audit(
    reports: list[dict[str, Any]],
    trades: pd.DataFrame,
) -> dict[str, Any]:
    primary = trades.loc[trades["fold"].isin(PRIMARY_EVALUATION_FOLDS)] if not trades.empty else trades
    output: dict[str, Any] = {}
    raw_pvalues: dict[str, float] = {}
    for cell in CELLS:
        part = primary.loc[primary["cell"].eq(cell)] if not primary.empty else primary
        exact = metrics(part, 10, 0)
        fixed = fixed_metrics(part)
        exact_bootstrap = _bootstrap_block_ev(
            part, "pnl_u_h10_d0", seed_key=f"final|{cell}|exact"
        )
        fixed_bootstrap = _bootstrap_block_ev(
            part, "pnl_u_h10_fixed_d1", seed_key=f"final|{cell}|fixed"
        )
        mappings = {
            report["name"]: report["mapping"].get(cell)
            for report in reports
            if report["name"] in PRIMARY_EVALUATION_FOLDS
        }
        fold_pnl = {}
        for fold in PRIMARY_EVALUATION_FOLDS:
            fold_part = part.loc[part["fold"].eq(fold)] if not part.empty else part
            fold_pnl[fold] = {
                "exact": metrics(fold_part, 10, 0)["pnlU"],
                "fixed": fixed_metrics(fold_part)["pnlU"],
            }
        exact_probability = exact_bootstrap["probabilityEvNonPositive"]
        fixed_probability = fixed_bootstrap["probabilityEvNonPositive"]
        pvalue = max(
            1.0 if exact_probability is None else float(exact_probability),
            1.0 if fixed_probability is None else float(fixed_probability),
        )
        raw_pvalues[cell] = pvalue
        output[cell] = {
            "mappings": mappings,
            "exact": exact,
            "fixedSettlement": fixed,
            "exactBootstrap": exact_bootstrap,
            "fixedBootstrap": fixed_bootstrap,
            "foldPnlU": fold_pnl,
            "rawBootstrapPValue": pvalue,
        }
    adjusted = _holm_adjust(raw_pvalues)
    for cell, row in output.items():
        row["holmAdjustedPValue"] = adjusted[cell]
        row["passed"] = bool(
            all(value is not None for value in row["mappings"].values())
            and row["exact"]["trades"] >= 90
            and row["fixedSettlement"]["trades"] >= 90
            and row["exact"]["wilson95LowerPct"] is not None
            and row["exact"]["wilson95LowerPct"] > BREAKEVEN_WR
            and row["fixedSettlement"]["wilson95LowerPct"] is not None
            and row["fixedSettlement"]["wilson95LowerPct"] > BREAKEVEN_WR
            and row["exactBootstrap"]["lower90EvU"] is not None
            and row["exactBootstrap"]["lower90EvU"] > 0.0
            and row["fixedBootstrap"]["lower90EvU"] is not None
            and row["fixedBootstrap"]["lower90EvU"] > 0.0
            and all(
                value["exact"] > 0.0 and value["fixed"] > 0.0
                for value in row["foldPnlU"].values()
            )
            and row["holmAdjustedPValue"] <= 0.05
        )
        row["decision"] = "research_candidate_only" if row["passed"] else "no_trade"
    return output


def fixed_profile_audit(candidates: pd.DataFrame) -> pd.DataFrame:
    months = [period.strftime("%Y-%m") for period in pd.period_range("2026-02", "2026-07", freq="M")]
    rows: list[dict[str, Any]] = []
    for (cell, profile), raw_group in candidates.groupby(["cell", "profile"], sort=True):
        group = apply_shared_cooldown(raw_group)
        exact = metrics(group, 10, 0)
        shifted = metrics(group, 10, 1)
        fixed = fixed_metrics(group)
        exact_bootstrap = _bootstrap_block_ev(
            group, "pnl_u_h10_d0", seed_key=f"history|{cell}|{profile}|exact"
        )
        fixed_bootstrap = _bootstrap_block_ev(
            group, "pnl_u_h10_fixed_d1", seed_key=f"history|{cell}|{profile}|fixed"
        )
        month = pd.to_datetime(group["signal_time"], utc=True).dt.strftime("%Y-%m")
        row: dict[str, Any] = {
            "cell": str(cell),
            "profile": str(profile),
            "family": str(group["family"].iloc[0]),
            "lookback_min": int(group["lookback_min"].iloc[0]),
            "trades": exact["trades"],
            "exact_win_rate_pct": exact["winRatePct"],
            "exact_wilson95_lower_pct": exact["wilson95LowerPct"],
            "exact_pnl_u": exact["pnlU"],
            "shifted_win_rate_pct": shifted["winRatePct"],
            "shifted_pnl_u": shifted["pnlU"],
            "fixed_win_rate_pct": fixed["winRatePct"],
            "fixed_wilson95_lower_pct": fixed["wilson95LowerPct"],
            "fixed_pnl_u": fixed["pnlU"],
            "exact_bootstrap_lower90_ev_u": exact_bootstrap["lower90EvU"],
            "fixed_bootstrap_lower90_ev_u": fixed_bootstrap["lower90EvU"],
        }
        positive_exact = positive_fixed = 0
        months_with_20 = 0
        for month_name in months:
            part = group.loc[month.eq(month_name)]
            exact_month = metrics(part, 10, 0)
            fixed_month = fixed_metrics(part)
            row[f"{month_name}_trades"] = exact_month["trades"]
            row[f"{month_name}_exact_pnl_u"] = exact_month["pnlU"]
            row[f"{month_name}_fixed_pnl_u"] = fixed_month["pnlU"]
            positive_exact += int(exact_month["pnlU"] > 0.0)
            positive_fixed += int(fixed_month["pnlU"] > 0.0)
            months_with_20 += int(exact_month["trades"] >= 20)
        row["positive_exact_months"] = positive_exact
        row["positive_fixed_months"] = positive_fixed
        row["months_with_20_trades"] = months_with_20
        row["retrospective_candidate"] = bool(
            exact["trades"] >= 90
            and months_with_20 >= 2
            and exact["winRatePct"] is not None
            and exact["winRatePct"] > BREAKEVEN_WR
            and fixed["winRatePct"] is not None
            and fixed["winRatePct"] > BREAKEVEN_WR
            and exact["pnlU"] > 0.0
            and fixed["pnlU"] > 0.0
            and positive_exact >= 4
            and positive_fixed >= 4
            and exact_bootstrap["lower90EvU"] is not None
            and exact_bootstrap["lower90EvU"] > 0.0
            and fixed_bootstrap["lower90EvU"] is not None
            and fixed_bootstrap["lower90EvU"] > 0.0
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
    volatility = build_volatility_states(minutes, VOLATILITY_WINDOW_MIN)
    stationarity = build_stationarity_features(minutes)
    candidates = pd.concat(
        [generate_candidates(minutes, volatility, stationarity, profile) for profile in PROFILES],
        ignore_index=True,
    )
    if not candidates.empty:
        candidates = candidates.sort_values(["signal_time", "profile"], kind="stable").reset_index(drop=True)
    reports, trades = walk_forward(candidates)
    audit = final_cell_audit(reports, trades)
    profile_audit = fixed_profile_audit(candidates)
    candidates.to_csv(OUT_CANDIDATES, index=False, encoding="utf-8-sig")
    trades.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    profile_audit.to_csv(OUT_PROFILE_AUDIT, index=False, encoding="utf-8-sig")

    known_structure = stationarity.loc[
        stationarity["structure_state"].isin(("revertible", "trend", "mixed", "shock")),
        "structure_state",
    ]
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "status": "V19_CAUSAL_STATIONARITY_ROUTER",
        "safety": {
            "researchOnly": True,
            "tradeEnabled": False,
            "deploymentPerformed": False,
            "realTradingAllowed": False,
        },
        "data": {
            "input": str(Path(input_path).resolve()),
            "rows": int(len(minutes)),
            "candidateRows": int(len(candidates)),
            "warning": "All dates are already inspected; even a pass is historical-walk-forward evidence only.",
        },
        "design": {
            "volatilityWindowMin": VOLATILITY_WINDOW_MIN,
            "stationarityEstimationWindowMin": ESTIMATION_WINDOW_MIN,
            "profiles": [asdict(profile) for profile in PROFILES],
            "cells": list(CELLS),
            "shockThreshold": 1.60,
            "revertible": "ER60<=0.30; |M60|<=1.50; 3<=half-life<=30; ADF-style t<=-2.5; VR10<=0.90; no shock",
            "trend": "ER60>=0.40; |M60|>=1.75; VR10>=1.05 or failed stationarity; no shock",
            "shock": "max prior/current 10-minute sigma15/sigma120 >= 1.60",
            "selectionMinimum": "3 training months, 90 trades, 2 months with >=20 trades, exact/fixed WR>55.56%, 2/3 positive months, 7-day block-bootstrap lower90 EV>0",
            "primaryEvaluationFolds": list(PRIMARY_EVALUATION_FOLDS),
            "july": "reused diagnostic only",
        },
        "stateOccupancyPct": {
            state: round(float(known_structure.eq(state).mean()) * 100.0, 4)
            for state in ("revertible", "trend", "mixed", "shock")
        },
        "folds": reports,
        "cellAudit": audit,
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
            "passedCells": [cell for cell, row in audit.items() if row["passed"]],
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
                    "stateOccupancyPct": report["stateOccupancyPct"],
                    "folds": report["folds"],
                    "cellAudit": report["cellAudit"],
                    "fixedProfileAudit": report["fixedProfileAudit"],
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
