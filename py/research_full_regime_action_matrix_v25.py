"""V25 full volatility-state/action-family matrix on frozen futures minutes.

This module is deliberately research-only.  It compares every action family in
every causal volatility state, including an explicit no-trade action.  No
state-to-family mapping is assumed before the walk-forward test.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from research_minute_volatility_normal_v15 import (
    AMOUNT_U,
    BREAKEVEN_WR,
    PAYOUT_RATE,
    STATES,
    _boundary_mask,
    clean,
    load_minutes,
    wilson_lower,
)
from research_volatility_window_sensitivity_v17 import build_volatility_states


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "data" / "btcusdt_futures_1m_20240101_20260730.csv"
MANIFEST = ROOT / "data" / "btcusdt_futures_1m_20240101_20260730.manifest.json"
OUT_JSON = ROOT / "tmp" / "v25_full_regime_action_matrix_20260730.json"
OUT_AUDIT = ROOT / "tmp" / "v25_fixed_profile_audit_20260730.csv"
OUT_MONTHLY = ROOT / "tmp" / "v25_monthly_profile_matrix_20260730.csv"
OUT_SELECTIONS = ROOT / "tmp" / "v25_walkforward_selections_20260730.csv"
OUT_TRADES = ROOT / "tmp" / "v25_walkforward_trades_20260730.csv"

VOLATILITY_WINDOW_MIN = 120
LOOKBACKS_MIN = (10, 20, 30, 60, 120)
THRESHOLDS = (1.5, 2.0, 2.5)
HORIZONS_MIN = (5, 10, 20)
DELAYS_MIN = (0, 1)
TRAINING_WINDOWS_MONTHS = (3, 6)

DIRECT_REVERSION = "direct_mean_reversion"
CONFIRMED_REVERSAL = "confirmed_reversal"
TREND_CONTINUATION = "trend_continuation"
EXHAUSTION_REVERSAL = "exhaustion_reversal"
NO_TRADE = "no_trade"
FAMILIES = (
    DIRECT_REVERSION,
    CONFIRMED_REVERSAL,
    TREND_CONTINUATION,
    EXHAUSTION_REVERSAL,
)


@dataclass(frozen=True)
class V25Profile:
    name: str
    family: str
    lookback_min: int
    threshold: float


def _token(value: float) -> str:
    return str(value).replace(".", "p")


PREFIX = {
    DIRECT_REVERSION: "meanrev",
    CONFIRMED_REVERSAL: "confirmed",
    TREND_CONTINUATION: "continuation",
    EXHAUSTION_REVERSAL: "exhaustion",
}
PROFILES = tuple(
    V25Profile(
        name=f"v25_{PREFIX[family]}_w{window}_s{_token(threshold)}",
        family=family,
        lookback_min=window,
        threshold=threshold,
    )
    for family in FAMILIES
    for window in LOOKBACKS_MIN
    for threshold in THRESHOLDS
)
PROFILE_BY_NAME = {profile.name: profile for profile in PROFILES}


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_frozen_input(path: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    expected = str(manifest.get("sha256", "")).lower()
    observed = sha256_file(path)
    if not expected or observed != expected:
        raise ValueError(
            f"frozen minute SHA-256 mismatch: expected={expected!r}, observed={observed!r}"
        )
    return {
        "manifest": str(Path(manifest_path).resolve()),
        "sha256": observed,
        "manifestAudit": manifest.get("audit", {}),
    }


def calendar_folds(
    index: pd.DatetimeIndex,
) -> list[tuple[str, str, pd.Timestamp, pd.Timestamp, bool]]:
    """Return key, display name, start, end and completeness for each month."""
    end_exclusive = index[-1] + pd.Timedelta(minutes=1)
    starts = pd.date_range(
        index[0].floor("D").replace(day=1),
        end_exclusive.floor("D").replace(day=1),
        freq="MS",
        tz="UTC",
    )
    folds: list[tuple[str, str, pd.Timestamp, pd.Timestamp, bool]] = []
    for value in starts:
        start = pd.Timestamp(value)
        nominal_end = start + pd.DateOffset(months=1)
        end = min(pd.Timestamp(nominal_end), end_exclusive)
        if end <= start:
            continue
        complete = end == nominal_end
        key = start.strftime("%Y-%m")
        name = key if complete else f"{key}_partial"
        folds.append((key, name, start, pd.Timestamp(end), complete))
    return folds


def _family_signal_arrays(
    minutes: pd.DataFrame,
    lookback_min: int,
    boundary_positions: np.ndarray,
) -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Causal UP/DOWN masks and diagnostic value at selected completed bars."""
    close = minutes["close"].astype(float)
    log_return = np.log(close / close.shift(1))
    past_close = close.shift(1)
    center = past_close.rolling(
        lookback_min, min_periods=lookback_min
    ).mean()
    sigma = past_close.rolling(
        lookback_min, min_periods=lookback_min
    ).std(ddof=1)
    z = (close - center) / sigma.replace(0.0, np.nan)
    z_at = z.iloc[boundary_positions].to_numpy(float)

    retest_min = max(5, lookback_min // 3)
    retest_periods = max(5, retest_min // 2)
    prior_z = z.shift(1)
    past_min = prior_z.rolling(retest_min, min_periods=retest_periods).min()
    past_max = prior_z.rolling(retest_min, min_periods=retest_periods).max()
    past_min_at = past_min.iloc[boundary_positions].to_numpy(float)
    past_max_at = past_max.iloc[boundary_positions].to_numpy(float)
    ret1_at = (log_return * 10_000.0).iloc[boundary_positions].to_numpy(float)

    move = np.log(close / close.shift(lookback_min)) * 10_000.0
    scale = (
        log_return.rolling(60, min_periods=60).std(ddof=0)
        * math.sqrt(lookback_min)
        * 10_000.0
    )
    score = move / scale.replace(0.0, np.nan)
    score_at = score.iloc[boundary_positions].to_numpy(float)
    move_at = move.iloc[boundary_positions].to_numpy(float)
    ret3_at = (
        np.log(close / close.shift(3)) * 10_000.0
    ).iloc[boundary_positions].to_numpy(float)
    counter_move = (
        np.sign(move_at) * np.sign(ret3_at) < 0.0
    ) & (np.abs(ret3_at) >= np.abs(move_at) * 0.15)

    output: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for threshold in THRESHOLDS:
        output[f"{DIRECT_REVERSION}|{threshold}"] = (
            z_at <= -threshold,
            z_at >= threshold,
            z_at,
        )
        output[f"{CONFIRMED_REVERSAL}|{threshold}"] = (
            (past_min_at <= -threshold)
            & (z_at >= -0.75)
            & (z_at <= 0.0)
            & (ret1_at > 0.0),
            (past_max_at >= threshold)
            & (z_at >= 0.0)
            & (z_at <= 0.75)
            & (ret1_at < 0.0),
            z_at,
        )
        output[f"{TREND_CONTINUATION}|{threshold}"] = (
            score_at >= threshold,
            score_at <= -threshold,
            score_at,
        )
        output[f"{EXHAUSTION_REVERSAL}|{threshold}"] = (
            (score_at <= -threshold) & counter_move,
            (score_at >= threshold) & counter_move,
            score_at,
        )
    return output


def generate_candidate_matrix(
    minutes: pd.DataFrame,
    volatility: pd.DataFrame,
) -> pd.DataFrame:
    boundary = _boundary_mask(minutes.index)
    known = volatility["vol_state"].isin(STATES).to_numpy(bool)
    # All six execution labels must exist so exact/delayed comparisons use the
    # identical signal set.
    positions = np.flatnonzero(boundary & known)
    positions = positions[positions + 1 + max(DELAYS_MIN) + max(HORIZONS_MIN) < len(minutes)]
    opens = minutes["open"].to_numpy(float)
    state_at = volatility["vol_state"].iloc[positions].astype(str).to_numpy()
    time_at = minutes.index[positions] + pd.Timedelta(minutes=1)
    parts: list[pd.DataFrame] = []

    for lookback in LOOKBACKS_MIN:
        signals = _family_signal_arrays(minutes, lookback, positions)
        for family in FAMILIES:
            for threshold in THRESHOLDS:
                profile = next(
                    item
                    for item in PROFILES
                    if item.family == family
                    and item.lookback_min == lookback
                    and item.threshold == threshold
                )
                up, down, value = signals[f"{family}|{threshold}"]
                selected = np.flatnonzero(up | down)
                if not len(selected):
                    continue
                selected_positions = positions[selected]
                direction = np.where(up[selected], 1, -1).astype(np.int8)
                part = pd.DataFrame(
                    {
                        "profile": profile.name,
                        "signal_pos": selected_positions.astype(np.int32),
                        "signal_time": time_at[selected],
                        "vol_state": state_at[selected],
                        "direction": direction,
                        "signal_value": value[selected].astype(np.float32),
                    }
                )
                for horizon in HORIZONS_MIN:
                    for delay in DELAYS_MIN:
                        entry_position = selected_positions + 1 + delay
                        settle_position = entry_position + horizon
                        signed = (
                            opens[settle_position] / opens[entry_position] - 1.0
                        ) * 10_000.0 * direction
                        part[f"signed_bps_h{horizon}_d{delay}"] = signed.astype(
                            np.float32
                        )
                parts.append(part)

    if not parts:
        columns = [
            "profile", "signal_pos", "signal_time", "vol_state", "direction",
            "signal_value",
            *[
                f"signed_bps_h{horizon}_d{delay}"
                for horizon in HORIZONS_MIN
                for delay in DELAYS_MIN
            ],
        ]
        return pd.DataFrame(columns=columns)
    candidates = pd.concat(parts, ignore_index=True)
    candidates["profile"] = pd.Categorical(
        candidates["profile"], categories=[profile.name for profile in PROFILES]
    )
    candidates["vol_state"] = pd.Categorical(
        candidates["vol_state"], categories=list(STATES)
    )
    return candidates


def apply_horizon_cooldown(frame: pd.DataFrame, horizon_min: int) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    ordered = frame.sort_values("signal_pos", kind="stable")
    positions = ordered["signal_pos"].to_numpy(np.int64)
    keep = np.zeros(len(ordered), dtype=bool)
    last = -10**18
    for index, position in enumerate(positions):
        if position - last >= horizon_min:
            keep[index] = True
            last = int(position)
    return ordered.iloc[np.flatnonzero(keep)].copy()


def common_period_frame(
    frame: pd.DataFrame,
    horizon_min: int,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    """Keep rows completed by both exact and delayed execution in the period."""
    if frame.empty:
        return frame.copy()
    signal_time = pd.to_datetime(frame["signal_time"], utc=True)
    delayed_entry = signal_time + pd.Timedelta(minutes=1)
    delayed_settlement = delayed_entry + pd.Timedelta(minutes=horizon_min)
    return frame.loc[delayed_entry.ge(start) & delayed_settlement.lt(end)].copy()


def metrics_from_signed(
    frame: pd.DataFrame,
    signed_column: str,
    *,
    calendar_months: Iterable[str] | None = None,
) -> dict[str, Any]:
    if frame.empty or signed_column not in frame.columns:
        months = list(calendar_months or [])
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
            "calendarMonths": len(months),
            "activeMonths": 0,
            "positiveMonthPct": 0.0 if months else None,
            "positiveActiveMonthPct": None,
            "worstMonthPnlU": 0.0 if months else None,
        }
    ordered = frame.sort_values("signal_pos", kind="stable")
    signed = pd.to_numeric(ordered[signed_column], errors="coerce")
    valid = signed.notna()
    ordered = ordered.loc[valid].copy()
    signed = signed.loc[valid].to_numpy(float)
    if not len(signed):
        return metrics_from_signed(pd.DataFrame(), signed_column, calendar_months=calendar_months)
    wins = int(np.sum(signed > 0.0))
    losses = int(np.sum(signed < 0.0))
    ties = int(np.sum(signed == 0.0))
    decided = wins + losses
    pnl = np.where(
        signed > 0.0,
        AMOUNT_U * PAYOUT_RATE,
        np.where(signed < 0.0, -AMOUNT_U, 0.0),
    )
    equity = np.r_[0.0, np.cumsum(pnl)]
    drawdown = np.maximum.accumulate(equity) - equity
    streak = maximum = 0
    for value in signed:
        streak = streak + 1 if value < 0.0 else 0
        maximum = max(maximum, streak)
    observed_month = pd.to_datetime(ordered["signal_time"], utc=True).dt.strftime("%Y-%m")
    monthly_pnl = pd.Series(pnl).groupby(observed_month.reset_index(drop=True)).sum()
    month_keys = list(calendar_months) if calendar_months is not None else sorted(monthly_pnl.index)
    monthly_pnl = monthly_pnl.reindex(month_keys, fill_value=0.0)
    monthly_trades = observed_month.value_counts().reindex(month_keys, fill_value=0)
    active = monthly_trades.gt(0)
    lower = wilson_lower(wins, decided)
    return {
        "trades": int(len(signed)),
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "winRatePct": round(100.0 * wins / decided, 4) if decided else None,
        "wilson95LowerPct": round(100.0 * lower, 4) if lower is not None else None,
        "pnlU": round(float(pnl.sum()), 4),
        "expectedValueU": round(float(pnl.mean()), 6),
        "maxDrawdownU": round(float(drawdown.max()), 4),
        "maxLossStreak": maximum,
        "calendarMonths": int(len(month_keys)),
        "activeMonths": int(active.sum()),
        "positiveMonthPct": round(float(monthly_pnl.gt(0.0).mean()) * 100.0, 4)
        if len(month_keys)
        else None,
        "positiveActiveMonthPct": round(
            float(monthly_pnl.loc[active].gt(0.0).mean()) * 100.0, 4
        )
        if active.any()
        else None,
        "worstMonthPnlU": round(float(monthly_pnl.min()), 4)
        if len(month_keys)
        else None,
    }


def _flatten_metrics(prefix: str, summary: dict[str, Any]) -> dict[str, Any]:
    return {
        f"{prefix}_{key}": value
        for key, value in summary.items()
        if key not in {"calendarMonths"}
    }


def _year_periods(data_end: pd.Timestamp) -> list[tuple[str, pd.Timestamp, pd.Timestamp]]:
    return [
        (
            str(year),
            pd.Timestamp(f"{year}-01-01T00:00:00Z"),
            min(pd.Timestamp(f"{year + 1}-01-01T00:00:00Z"), data_end),
        )
        for year in (2024, 2025, 2026)
    ]


def build_fixed_audit(
    candidates: pd.DataFrame,
    folds: list[tuple[str, str, pd.Timestamp, pd.Timestamp, bool]],
    data_end: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[tuple[str, str], np.ndarray]]:
    group_indices = {
        (str(profile), str(state)): np.asarray(indices, dtype=np.int64)
        for (profile, state), indices in candidates.groupby(
            ["profile", "vol_state"], observed=True, sort=False
        ).indices.items()
    }
    calendar_keys = [key for key, _, _, _, _ in folds]
    audit_rows: list[dict[str, Any]] = []
    monthly_rows: list[dict[str, Any]] = []
    full_start = folds[0][2]

    for profile in PROFILES:
        for state in STATES:
            raw = candidates.iloc[group_indices.get((profile.name, state), [])]
            for horizon in HORIZONS_MIN:
                executed = apply_horizon_cooldown(raw, horizon)
                common_full = common_period_frame(executed, horizon, full_start, data_end)
                row: dict[str, Any] = {
                    "vol_state": state,
                    "profile": profile.name,
                    "family": profile.family,
                    "lookback_min": profile.lookback_min,
                    "threshold": profile.threshold,
                    "horizon_min": horizon,
                }
                for delay, label in ((0, "exact"), (1, "delayed")):
                    summary = metrics_from_signed(
                        common_full,
                        f"signed_bps_h{horizon}_d{delay}",
                        calendar_months=calendar_keys,
                    )
                    row.update(_flatten_metrics(f"full_{label}", summary))
                for year, start, end in _year_periods(data_end):
                    common_year = common_period_frame(executed, horizon, start, end)
                    for delay, label in ((0, "exact"), (1, "delayed")):
                        summary = metrics_from_signed(
                            common_year, f"signed_bps_h{horizon}_d{delay}"
                        )
                        row.update(_flatten_metrics(f"y{year}_{label}", summary))
                audit_rows.append(row)

                for key, name, start, end, complete in folds:
                    common_month = common_period_frame(executed, horizon, start, end)
                    for delay, label in ((0, "exact"), (1, "delayed")):
                        summary = metrics_from_signed(
                            common_month, f"signed_bps_h{horizon}_d{delay}"
                        )
                        monthly_rows.append(
                            {
                                "month": key,
                                "fold": name,
                                "complete_month": complete,
                                "vol_state": state,
                                "profile": profile.name,
                                "family": profile.family,
                                "lookback_min": profile.lookback_min,
                                "threshold": profile.threshold,
                                "horizon_min": horizon,
                                "execution": label,
                                **summary,
                            }
                        )

    audit = pd.DataFrame(audit_rows)
    monthly = pd.DataFrame(monthly_rows)
    positive = audit[
        audit["full_exact_pnlU"].gt(0.0)
        & audit["full_delayed_pnlU"].gt(0.0)
    ].copy()
    support: list[int] = []
    window_index = {window: index for index, window in enumerate(LOOKBACKS_MIN)}
    for row in audit.itertuples(index=False):
        peers = positive.loc[
            positive["vol_state"].eq(row.vol_state)
            & positive["family"].eq(row.family)
            & positive["horizon_min"].eq(row.horizon_min)
        ]
        same_window = peers["lookback_min"].eq(row.lookback_min) & peers[
            "threshold"
        ].sub(row.threshold).abs().le(0.5000001)
        neighbor_windows = peers["lookback_min"].map(window_index).sub(
            window_index[row.lookback_min]
        ).abs().le(1) & peers["threshold"].eq(row.threshold)
        support.append(int((same_window | neighbor_windows).sum()))
    audit["parameter_support_count"] = support
    audit["all_years_positive"] = np.logical_and.reduce(
        [
            audit[f"y{year}_{execution}_pnlU"].gt(0.0)
            for year in (2024, 2025, 2026)
            for execution in ("exact", "delayed")
        ]
    )
    audit["strict_fixed_pass"] = (
        audit["full_exact_trades"].ge(60)
        & audit["full_delayed_trades"].ge(60)
        & audit["full_exact_wilson95LowerPct"].gt(BREAKEVEN_WR)
        & audit["full_delayed_wilson95LowerPct"].gt(BREAKEVEN_WR)
        & audit["full_exact_positiveMonthPct"].ge(60.0)
        & audit["full_delayed_positiveMonthPct"].ge(60.0)
        & audit["all_years_positive"]
        & audit["parameter_support_count"].ge(2)
    )

    # Explicit no-trade is part of the action matrix, not an inferred absence.
    no_trade_audit = []
    for state in STATES:
        row = {column: np.nan for column in audit.columns}
        row.update(
            {
                "vol_state": state,
                "profile": NO_TRADE,
                "family": NO_TRADE,
                "lookback_min": 0,
                "threshold": 0.0,
                "horizon_min": 0,
                "parameter_support_count": 0,
                "all_years_positive": False,
                "strict_fixed_pass": True,
            }
        )
        for column in audit.columns:
            if column.endswith(("_trades", "_wins", "_losses", "_ties", "_pnlU", "_maxDrawdownU", "_maxLossStreak", "_activeMonths")):
                row[column] = 0
        no_trade_audit.append(row)
    audit = pd.concat([audit, pd.DataFrame(no_trade_audit)], ignore_index=True)

    no_trade_monthly = []
    for key, name, _, _, complete in folds:
        for state in STATES:
            for execution in ("exact", "delayed"):
                no_trade_monthly.append(
                    {
                        "month": key,
                        "fold": name,
                        "complete_month": complete,
                        "vol_state": state,
                        "profile": NO_TRADE,
                        "family": NO_TRADE,
                        "lookback_min": 0,
                        "threshold": 0.0,
                        "horizon_min": 0,
                        "execution": execution,
                        **metrics_from_signed(pd.DataFrame(), "none"),
                    }
                )
    monthly = pd.concat([monthly, pd.DataFrame(no_trade_monthly)], ignore_index=True)
    return audit, monthly, group_indices


def aggregate_training_months(
    monthly: pd.DataFrame,
    state: str,
    months: list[str],
) -> list[dict[str, Any]]:
    pool = monthly.loc[
        monthly["vol_state"].eq(state)
        & monthly["month"].isin(months)
        & monthly["family"].ne(NO_TRADE)
    ]
    summaries: list[dict[str, Any]] = []
    for (profile, horizon), group in pool.groupby(
        ["profile", "horizon_min"], sort=True
    ):
        meta = PROFILE_BY_NAME[str(profile)]
        row: dict[str, Any] = {
            "profile": str(profile),
            "family": meta.family,
            "lookbackMin": meta.lookback_min,
            "threshold": meta.threshold,
            "horizonMin": int(horizon),
        }
        robust = True
        for execution in ("exact", "delayed"):
            part = group.loc[group["execution"].eq(execution)].set_index("month").reindex(months)
            trades = int(pd.to_numeric(part["trades"], errors="coerce").fillna(0).sum())
            wins = int(pd.to_numeric(part["wins"], errors="coerce").fillna(0).sum())
            losses = int(pd.to_numeric(part["losses"], errors="coerce").fillna(0).sum())
            ties = int(pd.to_numeric(part["ties"], errors="coerce").fillna(0).sum())
            pnl_series = pd.to_numeric(part["pnlU"], errors="coerce").fillna(0.0)
            pnl = float(pnl_series.sum())
            decided = wins + losses
            lower = wilson_lower(wins, decided)
            active = pd.to_numeric(part["trades"], errors="coerce").fillna(0).gt(0)
            positive_active = (
                float(pnl_series.loc[active].gt(0.0).mean()) * 100.0
                if active.any()
                else 0.0
            )
            summary = {
                "trades": trades,
                "wins": wins,
                "losses": losses,
                "ties": ties,
                "winRatePct": 100.0 * wins / decided if decided else None,
                "wilson95LowerPct": 100.0 * lower if lower is not None else None,
                "pnlU": pnl,
                "expectedValueU": pnl / trades if trades else None,
                "activeMonths": int(active.sum()),
                "positiveActiveMonthPct": positive_active,
                "worstMonthPnlU": float(pnl_series.min()),
            }
            row[execution] = summary
            robust &= bool(
                trades >= max(12, 2 * len(months))
                and summary["activeMonths"] >= max(2, math.ceil(len(months) / 2))
                and pnl > 0.0
                and summary["winRatePct"] is not None
                and summary["winRatePct"] > BREAKEVEN_WR
                and positive_active >= 60.0
                and summary["worstMonthPnlU"] >= -20.0
            )
        row["baseEligible"] = robust
        summaries.append(row)

    eligible = [row for row in summaries if row["baseEligible"]]
    windows = {value: index for index, value in enumerate(LOOKBACKS_MIN)}
    for row in summaries:
        peers = [
            peer
            for peer in eligible
            if peer["family"] == row["family"]
            and peer["horizonMin"] == row["horizonMin"]
            and (
                (
                    peer["lookbackMin"] == row["lookbackMin"]
                    and abs(peer["threshold"] - row["threshold"]) <= 0.5000001
                )
                or (
                    peer["threshold"] == row["threshold"]
                    and abs(windows[peer["lookbackMin"]] - windows[row["lookbackMin"]]) <= 1
                )
            )
        ]
        row["parameterSupportCount"] = len(peers)
        row["eligible"] = bool(row["baseEligible"] and len(peers) >= 2)
    return summaries


def select_walkforward_variant(
    monthly: pd.DataFrame,
    state: str,
    months: list[str],
) -> dict[str, Any] | None:
    summaries = aggregate_training_months(monthly, state, months)
    ranked = []
    for row in summaries:
        if not row["eligible"]:
            continue
        exact = row["exact"]
        delayed = row["delayed"]
        score = (
            min(exact["wilson95LowerPct"] or 0.0, delayed["wilson95LowerPct"] or 0.0),
            min(exact["expectedValueU"] or -99.0, delayed["expectedValueU"] or -99.0),
            min(exact["positiveActiveMonthPct"], delayed["positiveActiveMonthPct"]),
            row["parameterSupportCount"],
            -max(exact["trades"], delayed["trades"]),
            row["profile"],
        )
        ranked.append((score, row))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    score, selected = ranked[0]
    return {**selected, "score": list(score)}


def apply_variable_horizon_cooldown(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    ordered = frame.sort_values(["signal_pos", "profile"], kind="stable")
    kept: list[int] = []
    prior_signal = -10**18
    prior_horizon = 0
    for index, row in ordered.iterrows():
        position = int(row["signal_pos"])
        if position - prior_signal < prior_horizon:
            continue
        kept.append(index)
        prior_signal = position
        prior_horizon = int(row["selected_horizon_min"])
    return ordered.loc[kept].copy()


def _walkforward_metrics(frame: pd.DataFrame, execution: str) -> dict[str, Any]:
    column = f"signed_bps_{execution}"
    return metrics_from_signed(frame, column)


def run_walkforward_mode(
    candidates: pd.DataFrame,
    group_indices: dict[tuple[str, str], np.ndarray],
    monthly: pd.DataFrame,
    folds: list[tuple[str, str, pd.Timestamp, pd.Timestamp, bool]],
    training_months: int,
) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame]:
    reports: list[dict[str, Any]] = []
    selection_rows: list[dict[str, Any]] = []
    trade_parts: list[pd.DataFrame] = []
    for fold_index in range(training_months, len(folds)):
        key, name, start, end, complete = folds[fold_index]
        training_keys = [value[0] for value in folds[fold_index - training_months:fold_index]]
        selections = {
            state: select_walkforward_variant(monthly, state, training_keys)
            for state in STATES
        }
        state_parts: list[pd.DataFrame] = []
        for state, selection in selections.items():
            base_selection_row = {
                "mode": f"rolling_{training_months}m",
                "fold": name,
                "month": key,
                "complete_month": complete,
                "train_months": ",".join(training_keys),
                "vol_state": state,
            }
            if selection is None:
                selection_rows.append(
                    {
                        **base_selection_row,
                        "action": NO_TRADE,
                        "profile": NO_TRADE,
                        "family": NO_TRADE,
                        "lookback_min": 0,
                        "threshold": 0.0,
                        "horizon_min": 0,
                        "parameter_support_count": 0,
                    }
                )
                continue
            profile = str(selection["profile"])
            horizon = int(selection["horizonMin"])
            raw = candidates.iloc[group_indices.get((profile, state), [])]
            executed = apply_horizon_cooldown(raw, horizon)
            test = common_period_frame(executed, horizon, start, end)
            if not test.empty:
                test["selected_horizon_min"] = horizon
                test["selected_family"] = selection["family"]
                test["fold"] = name
                test["month"] = key
                test["training_mode"] = f"rolling_{training_months}m"
                test["signed_bps_exact"] = test[f"signed_bps_h{horizon}_d0"]
                test["signed_bps_delayed"] = test[f"signed_bps_h{horizon}_d1"]
                state_parts.append(test)
            selection_rows.append(
                {
                    **base_selection_row,
                    "action": selection["family"],
                    "profile": profile,
                    "family": selection["family"],
                    "lookback_min": selection["lookbackMin"],
                    "threshold": selection["threshold"],
                    "horizon_min": horizon,
                    "parameter_support_count": selection["parameterSupportCount"],
                    "train_exact_trades": selection["exact"]["trades"],
                    "train_exact_win_rate_pct": selection["exact"]["winRatePct"],
                    "train_exact_pnl_u": selection["exact"]["pnlU"],
                    "train_delayed_trades": selection["delayed"]["trades"],
                    "train_delayed_win_rate_pct": selection["delayed"]["winRatePct"],
                    "train_delayed_pnl_u": selection["delayed"]["pnlU"],
                }
            )
        fold_trades = apply_variable_horizon_cooldown(
            pd.concat(state_parts, ignore_index=True) if state_parts else pd.DataFrame()
        )
        if not fold_trades.empty:
            trade_parts.append(fold_trades)
        reports.append(
            {
                "fold": name,
                "month": key,
                "completeMonth": complete,
                "trainingMonths": training_keys,
                "mapping": {
                    state: (
                        {
                            "action": selection["family"],
                            "profile": selection["profile"],
                            "lookbackMin": selection["lookbackMin"],
                            "threshold": selection["threshold"],
                            "horizonMin": selection["horizonMin"],
                        }
                        if selection is not None
                        else {"action": NO_TRADE}
                    )
                    for state, selection in selections.items()
                },
                "test": {
                    "exact": _walkforward_metrics(fold_trades, "exact"),
                    "delayed": _walkforward_metrics(fold_trades, "delayed"),
                },
            }
        )

    trades = (
        pd.concat(trade_parts, ignore_index=True)
        if trade_parts
        else pd.DataFrame(
            columns=[
                "profile", "signal_pos", "signal_time", "vol_state",
                "direction", "selected_family", "selected_horizon_min",
                "fold", "month", "training_mode", "signed_bps_exact",
                "signed_bps_delayed",
            ]
        )
    )
    selections_frame = pd.DataFrame(selection_rows)
    exact = _walkforward_metrics(trades, "exact")
    delayed = _walkforward_metrics(trades, "delayed")
    fold_pnl = {
        report["fold"]: {
            "exact": report["test"]["exact"]["pnlU"],
            "delayed": report["test"]["delayed"]["pnlU"],
        }
        for report in reports
    }
    active_folds = [value for value in fold_pnl.values() if value["exact"] != 0.0 or value["delayed"] != 0.0]
    yearly: dict[str, Any] = {}
    for year in (2024, 2025, 2026):
        part = trades.loc[pd.to_datetime(trades["signal_time"], utc=True).dt.year.eq(year)] if not trades.empty else trades
        yearly[str(year)] = {
            "exact": _walkforward_metrics(part, "exact"),
            "delayed": _walkforward_metrics(part, "delayed"),
        }
    positive_exact = sum(value["exact"] > 0.0 for value in active_folds)
    positive_delayed = sum(value["delayed"] > 0.0 for value in active_folds)
    active_count = len(active_folds)
    selection_count = int(selections_frame["family"].ne(NO_TRADE).sum()) if not selections_frame.empty else 0
    total_cells = int(len(selections_frame))
    years_with_trades = [
        year for year, value in yearly.items() if value["exact"]["trades"] > 0
    ]
    all_active_years_positive = bool(
        len(years_with_trades) >= 2
        and all(
            yearly[year][execution]["pnlU"] > 0.0
            for year in years_with_trades
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
        and active_count >= 6
        and positive_exact / active_count >= 0.60
        and positive_delayed / active_count >= 0.60
        and all_active_years_positive
    )
    summary = {
        "trainingWindowMonths": training_months,
        "folds": reports,
        "overall": {"exact": exact, "delayed": delayed},
        "yearly": yearly,
        "activeFoldCount": active_count,
        "positiveExactActiveFoldPct": round(100.0 * positive_exact / active_count, 4)
        if active_count
        else 0.0,
        "positiveDelayedActiveFoldPct": round(100.0 * positive_delayed / active_count, 4)
        if active_count
        else 0.0,
        "selectionCoveragePct": round(100.0 * selection_count / total_cells, 4)
        if total_cells
        else 0.0,
        "familySelectionCounts": {
            str(key): int(value)
            for key, value in selections_frame["family"].value_counts().items()
        }
        if not selections_frame.empty
        else {NO_TRADE: total_cells},
        "allActiveYearsPositive": all_active_years_positive,
        "passed": passed,
        "decision": "research_candidate_only" if passed else NO_TRADE,
    }
    return summary, selections_frame, trades


def fixed_family_champions(audit: pd.DataFrame) -> dict[str, Any]:
    output: dict[str, Any] = {}
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
    for state in STATES:
        state_output: dict[str, Any] = {}
        for family in FAMILIES:
            part = active.loc[
                active["vol_state"].eq(state) & active["family"].eq(family)
            ].sort_values(
                [
                    "strict_fixed_pass", "all_years_positive",
                    "robust_wilson", "robust_ev", "robust_month_pct",
                    "parameter_support_count",
                ],
                ascending=False,
                kind="stable",
            )
            if part.empty:
                state_output[family] = {"bestObserved": None, "action": NO_TRADE}
                continue
            row = part.iloc[0]
            state_output[family] = {
                "bestObserved": {
                    "profile": str(row["profile"]),
                    "lookbackMin": int(row["lookback_min"]),
                    "threshold": float(row["threshold"]),
                    "horizonMin": int(row["horizon_min"]),
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
                    "allYearsPositive": bool(row["all_years_positive"]),
                    "parameterSupportCount": int(row["parameter_support_count"]),
                    "strictFixedPass": bool(row["strict_fixed_pass"]),
                },
                "action": family if bool(row["strict_fixed_pass"]) else NO_TRADE,
            }
        state_output[NO_TRADE] = {
            "profile": NO_TRADE,
            "trades": 0,
            "pnlU": 0.0,
            "capitalAtRiskU": 0.0,
        }
        output[state] = state_output
    return output


def run(input_path: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    frozen = verify_frozen_input(input_path, manifest_path)
    minutes = load_minutes(input_path)[["open", "high", "low", "close", "volume"]].copy()
    manifest_rows = frozen.get("manifestAudit", {}).get("rows")
    if manifest_rows is not None and int(manifest_rows) != len(minutes):
        raise ValueError("frozen manifest row count does not match loaded minute rows")
    volatility = build_volatility_states(minutes, VOLATILITY_WINDOW_MIN)
    candidates = generate_candidate_matrix(minutes, volatility)
    folds = calendar_folds(minutes.index)
    data_end = minutes.index[-1] + pd.Timedelta(minutes=1)
    audit, monthly, group_indices = build_fixed_audit(
        candidates, folds, data_end
    )
    audit.to_csv(OUT_AUDIT, index=False, encoding="utf-8-sig")
    monthly.to_csv(OUT_MONTHLY, index=False, encoding="utf-8-sig")

    mode_reports: dict[str, Any] = {}
    selection_parts: list[pd.DataFrame] = []
    trade_parts: list[pd.DataFrame] = []
    for training_months in TRAINING_WINDOWS_MONTHS:
        summary, selections, trades = run_walkforward_mode(
            candidates,
            group_indices,
            monthly,
            folds,
            training_months,
        )
        mode_reports[f"rolling_{training_months}m"] = summary
        selection_parts.append(selections)
        trade_parts.append(trades)
    selections_all = pd.concat(selection_parts, ignore_index=True)
    trades_all = pd.concat(trade_parts, ignore_index=True)
    selections_all.to_csv(OUT_SELECTIONS, index=False, encoding="utf-8-sig")
    trades_all.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")

    passed_modes = [
        mode for mode, value in mode_reports.items() if value["passed"]
    ]
    platform_passed = len(passed_modes) == len(TRAINING_WINDOWS_MONTHS)
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "status": "V25_FULL_CAUSAL_VOLATILITY_ACTION_MATRIX",
        "safety": {
            "researchOnly": True,
            "tradeEnabled": False,
            "realTradingAllowed": False,
            "deploymentPerformed": False,
            "onlineConfigurationChanged": False,
        },
        "data": {
            "input": str(Path(input_path).resolve()),
            "rows": int(len(minutes)),
            "start": minutes.index[0],
            "end": minutes.index[-1],
            **frozen,
        },
        "design": {
            "volatilityState": {
                "windowMin": VOLATILITY_WINDOW_MIN,
                "thresholdHistoryMin": 7 * 24 * 60,
                "thresholds": "causal trailing q33/q67, shifted before classification",
                "states": list(STATES),
            },
            "normalLookbacksMin": list(LOOKBACKS_MIN),
            "noteAboutTenMinutes": "10m is one tested lookback and one tested holding horizon; it is not hard-coded as the only normal window.",
            "families": list(FAMILIES),
            "noTradeComparedExplicitly": True,
            "preRestrictedFamiliesByState": False,
            "thresholds": list(THRESHOLDS),
            "horizonsMin": list(HORIZONS_MIN),
            "execution": {
                "exact": "enter at the boundary immediately after the completed signal bar",
                "delayed": "enter one minute later and hold the full selected horizon from actual entry",
            },
            "profileCount": len(PROFILES),
            "profiles": [asdict(profile) for profile in PROFILES],
            "walkForwardTrainingWindowsMonths": list(TRAINING_WINDOWS_MONTHS),
            "selection": "training months only; both executions positive; active-month stability; adjacent parameter support; otherwise no-trade",
        },
        "candidateRows": int(len(candidates)),
        "fixedAudit": {
            "activeVariantStateRows": int(audit["family"].ne(NO_TRADE).sum()),
            "strictPassedRows": int(audit["strict_fixed_pass"].fillna(False).sum() - len(STATES)),
            "stateFamilyChampions": fixed_family_champions(audit),
            "interpretation": "descriptive full-history matrix only; not a causal deployment verdict",
        },
        "walkForward": mode_reports,
        "decision": {
            "passedModes": passed_modes,
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(INPUT))
    parser.add_argument("--manifest", default=str(MANIFEST))
    args = parser.parse_args()
    report = run(args.input, args.manifest)
    print(
        json.dumps(
            clean(
                {
                    "candidateRows": report["candidateRows"],
                    "fixedAudit": report["fixedAudit"],
                    "walkForward": {
                        mode: {
                            key: value[key]
                            for key in (
                                "overall", "yearly", "activeFoldCount",
                                "positiveExactActiveFoldPct",
                                "positiveDelayedActiveFoldPct",
                                "selectionCoveragePct", "familySelectionCounts",
                                "passed", "decision",
                            )
                        }
                        for mode, value in report["walkForward"].items()
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
