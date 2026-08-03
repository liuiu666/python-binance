"""V36 fixed ETH/BTC context audit for executable normal-tail signals.

Research only.  The BTC signal, ETH context rules, thresholds, cooldown and
acceptance gates are fixed before reading outcomes.  No profile is selected on
future labels and no online configuration is touched.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research_full_history_regime_walkforward_v31 import load_frozen_history
from research_full_regime_action_matrix_v25 import (
    BREAKEVEN_WR,
    apply_horizon_cooldown,
    metrics_from_signed,
    verify_frozen_input,
)
from research_minute_volatility_normal_v15 import clean, load_minutes
from research_volatility_window_sensitivity_v17 import (
    build_volatility_states,
    volatility_summary,
)


ROOT = Path(__file__).resolve().parents[1]
BTC_INPUTS = (
    ROOT / "data" / "btcusdt_futures_1m_20200101_20240101.csv",
    ROOT / "data" / "btcusdt_futures_1m_20240101_20260730.csv",
)
BTC_MANIFESTS = (
    ROOT / "data" / "btcusdt_futures_1m_20200101_20240101.manifest.json",
    ROOT / "data" / "btcusdt_futures_1m_20240101_20260730.manifest.json",
)
ETH_INPUT = ROOT / "data" / "ethusdt_futures_1m_20200101_20260701.csv"
ETH_MANIFEST = ROOT / "data" / "ethusdt_futures_1m_20200101_20260701.manifest.json"

OUT_JSON = ROOT / "tmp" / "v36_cross_market_normal_router_20260730.json"
OUT_AUDIT = ROOT / "tmp" / "v36_cross_market_normal_router_audit_20260730.csv"
OUT_TRADES = ROOT / "tmp" / "v36_cross_market_normal_router_trades_20260730.csv"

VOLATILITY_WINDOW_MIN = 120
NORMAL_WINDOW_MIN = 120
NORMAL_THRESHOLD = 2.0
HORIZON_MIN = 10
BETA_WINDOW_MIN = 1440
RESIDUAL_Z_WINDOW_MIN = 10080
RESIDUAL_Z_MIN_PERIODS = 1440
PROFILE_NAMES = (
    "btc_direct_reversion_control",
    "btc_idiosyncratic_reversion",
    "eth_turn_confirmed_reversion",
    "residual_eth_turn_reversion",
    "common_shock_continuation",
    "common_shock_exhaustion_reversion",
)
STATES = ("low", "mid", "high")
EXECUTIONS = ("exact", "delayed", "fixed_settlement")
MIN_TRADES = 500
MIN_POSITIVE_PHASE_PCT = 80.0
MIN_POSITIVE_ACTIVE_MONTH_PCT = 60.0
MIN_DELAY_EV_RETENTION = 0.60


def _rolling_z(value: pd.Series, window: int, min_periods: int) -> pd.Series:
    history = value.shift(1)
    center = history.rolling(window, min_periods=min_periods).mean()
    scale = history.rolling(window, min_periods=min_periods).std(ddof=1)
    return (value - center) / scale.replace(0.0, np.nan)


def build_features(btc: pd.DataFrame, eth: pd.DataFrame) -> pd.DataFrame:
    """Build features available at each completed minute, never from the future."""
    joined = pd.DataFrame(
        {
            "btc_close": btc["close"].astype(float),
            "eth_close": eth["close"].astype(float),
        }
    ).dropna()
    btc_ret1 = np.log(joined["btc_close"] / joined["btc_close"].shift(1))
    eth_ret1 = np.log(joined["eth_close"] / joined["eth_close"].shift(1))

    beta_history_btc = btc_ret1.shift(1)
    beta_history_eth = eth_ret1.shift(1)
    covariance = beta_history_btc.rolling(
        BETA_WINDOW_MIN, min_periods=BETA_WINDOW_MIN // 2
    ).cov(beta_history_eth)
    variance = beta_history_eth.rolling(
        BETA_WINDOW_MIN, min_periods=BETA_WINDOW_MIN // 2
    ).var(ddof=1)
    beta = (covariance / variance.replace(0.0, np.nan)).clip(0.1, 3.0)

    btc_move10 = np.log(joined["btc_close"] / joined["btc_close"].shift(10)) * 10_000.0
    eth_move10 = np.log(joined["eth_close"] / joined["eth_close"].shift(10)) * 10_000.0
    residual_move10 = btc_move10 - beta * eth_move10
    residual_z = _rolling_z(
        residual_move10, RESIDUAL_Z_WINDOW_MIN, RESIDUAL_Z_MIN_PERIODS
    )

    btc_sigma10 = (
        btc_ret1.shift(1).rolling(120, min_periods=60).std(ddof=0)
        * math.sqrt(10.0)
        * 10_000.0
    )
    eth_sigma10 = (
        eth_ret1.shift(1).rolling(120, min_periods=60).std(ddof=0)
        * math.sqrt(10.0)
        * 10_000.0
    )
    btc_shock = btc_move10 / btc_sigma10.replace(0.0, np.nan)
    eth_shock = eth_move10 / eth_sigma10.replace(0.0, np.nan)

    past_btc = joined["btc_close"].shift(1)
    normal_center = past_btc.rolling(
        NORMAL_WINDOW_MIN, min_periods=NORMAL_WINDOW_MIN
    ).mean()
    normal_sigma = past_btc.rolling(
        NORMAL_WINDOW_MIN, min_periods=NORMAL_WINDOW_MIN
    ).std(ddof=1)
    normal_z = (
        (joined["btc_close"] - normal_center)
        / normal_sigma.replace(0.0, np.nan)
    )

    return pd.DataFrame(
        {
            "normal_z": normal_z,
            "btc_move10_bps": btc_move10,
            "eth_move10_bps": eth_move10,
            "btc_shock": btc_shock,
            "eth_shock": eth_shock,
            "residual_move10_bps": residual_move10,
            "residual_z": residual_z,
            "eth_ret1_bps": eth_ret1 * 10_000.0,
            "eth_ret3_bps": np.log(
                joined["eth_close"] / joined["eth_close"].shift(3)
            )
            * 10_000.0,
        },
        index=joined.index,
    )


def build_candidates(
    btc: pd.DataFrame,
    features: pd.DataFrame,
    volatility: pd.DataFrame,
) -> pd.DataFrame:
    common = btc.index.intersection(features.index).intersection(volatility.index)
    btc = btc.loc[common]
    features = features.loc[common]
    volatility = volatility.loc[common]

    z = features["normal_z"].to_numpy(float)
    state = volatility["vol_state"].astype(str).to_numpy()
    known = np.isin(state, STATES)
    tail = (z <= -NORMAL_THRESHOLD) | (z >= NORMAL_THRESHOLD)
    positions = np.flatnonzero(known & tail)
    positions = positions[positions + 1 + HORIZON_MIN + 1 < len(btc)]
    if not len(positions):
        return pd.DataFrame()

    reversal_direction = np.where(z[positions] <= -NORMAL_THRESHOLD, 1, -1).astype(np.int8)
    residual_z = features["residual_z"].to_numpy(float)[positions]
    eth_ret1 = features["eth_ret1_bps"].to_numpy(float)[positions]
    eth_ret3 = features["eth_ret3_bps"].to_numpy(float)[positions]
    btc_shock = features["btc_shock"].to_numpy(float)[positions]
    eth_shock = features["eth_shock"].to_numpy(float)[positions]
    btc_move10 = features["btc_move10_bps"].to_numpy(float)[positions]

    residual_extreme = (-reversal_direction * residual_z) >= 1.0
    eth_turn = (
        (reversal_direction * eth_ret1 > 0.0)
        & (reversal_direction * eth_ret3 > 0.0)
    )
    common_shock = (
        (np.sign(btc_shock) == np.sign(eth_shock))
        & (np.abs(btc_shock) >= 1.0)
        & (np.abs(eth_shock) >= 0.75)
    )
    trend_direction = np.sign(btc_move10).astype(np.int8)
    trend_aligned_tail = trend_direction == -reversal_direction

    masks: dict[str, tuple[np.ndarray, np.ndarray]] = {
        "btc_direct_reversion_control": (
            np.ones(len(positions), dtype=bool), reversal_direction
        ),
        "btc_idiosyncratic_reversion": (residual_extreme, reversal_direction),
        "eth_turn_confirmed_reversion": (eth_turn, reversal_direction),
        "residual_eth_turn_reversion": (
            residual_extreme & eth_turn, reversal_direction
        ),
        "common_shock_continuation": (
            common_shock & trend_aligned_tail & (trend_direction != 0), trend_direction
        ),
        "common_shock_exhaustion_reversion": (
            common_shock & eth_turn, reversal_direction
        ),
    }

    opens = btc["open"].to_numpy(float)
    signal_time = btc.index[positions] + pd.Timedelta(minutes=1)
    parts: list[pd.DataFrame] = []
    for profile, (mask, direction_all) in masks.items():
        selected = np.flatnonzero(mask & np.isfinite(residual_z))
        if not len(selected):
            continue
        pos = positions[selected]
        direction = direction_all[selected].astype(np.int8)
        exact_entry = pos + 1
        delayed_entry = pos + 2
        exact_settle = exact_entry + HORIZON_MIN
        delayed_settle = delayed_entry + HORIZON_MIN
        fixed_settle = exact_settle
        part = pd.DataFrame(
            {
                "profile": profile,
                "signal_pos": pos.astype(np.int32),
                "signal_time": signal_time[selected],
                "phase": pd.DatetimeIndex(signal_time[selected]).minute % 10,
                "vol_state": state[pos],
                "direction": direction,
                "normal_z": z[pos].astype(np.float32),
                "residual_z": residual_z[selected].astype(np.float32),
                "btc_shock": btc_shock[selected].astype(np.float32),
                "eth_shock": eth_shock[selected].astype(np.float32),
                "signed_bps_exact": (
                    (opens[exact_settle] / opens[exact_entry] - 1.0)
                    * 10_000.0
                    * direction
                ).astype(np.float32),
                "signed_bps_delayed": (
                    (opens[delayed_settle] / opens[delayed_entry] - 1.0)
                    * 10_000.0
                    * direction
                ).astype(np.float32),
                "signed_bps_fixed_settlement": (
                    (opens[fixed_settle] / opens[delayed_entry] - 1.0)
                    * 10_000.0
                    * direction
                ).astype(np.float32),
            }
        )
        parts.append(part)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def _month_keys(index: pd.DatetimeIndex) -> list[str]:
    return [
        value.strftime("%Y-%m")
        for value in pd.period_range(
            index[0].tz_localize(None).to_period("M"),
            index[-1].tz_localize(None).to_period("M"),
            freq="M",
        )
    ]


def _slice(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    time = pd.to_datetime(frame["signal_time"], utc=True)
    return frame.loc[
        time.ge(pd.Timestamp(start)) & time.lt(pd.Timestamp(end))
    ].copy()


def _execution_metrics(frame: pd.DataFrame, months: list[str] | None = None) -> dict[str, Any]:
    return {
        execution: metrics_from_signed(
            frame,
            f"signed_bps_{execution}",
            calendar_months=months,
        )
        for execution in EXECUTIONS
    }


def audit_profile(
    candidates: pd.DataFrame,
    *,
    profile: str,
    state: str,
    months: list[str],
) -> tuple[dict[str, Any], pd.DataFrame]:
    raw = candidates.loc[
        candidates["profile"].eq(profile)
        & candidates["vol_state"].astype(str).eq(state)
    ].copy()
    trades = apply_horizon_cooldown(raw, HORIZON_MIN)
    overall = _execution_metrics(trades, months)

    periods = {
        "development_2020_2022": _execution_metrics(
            _slice(trades, "2020-01-01T00:00:00Z", "2023-01-01T00:00:00Z")
        ),
        "validation_2023": _execution_metrics(
            _slice(trades, "2023-01-01T00:00:00Z", "2024-01-01T00:00:00Z")
        ),
        "recent_2024_2026": _execution_metrics(
            _slice(trades, "2024-01-01T00:00:00Z", "2026-07-01T00:00:00Z")
        ),
    }
    years = {
        str(year): _execution_metrics(
            _slice(
                trades,
                f"{year}-01-01T00:00:00Z",
                f"{year + 1}-01-01T00:00:00Z",
            )
        )
        for year in range(2020, 2026)
    }
    phases = {
        str(phase): _execution_metrics(raw.loc[raw["phase"].eq(phase)])
        for phase in range(10)
    }
    positive_phase_pct = {
        execution: round(
            100.0
            * np.mean(
                [phases[str(phase)][execution]["pnlU"] > 0.0 for phase in range(10)]
            ),
            4,
        )
        for execution in EXECUTIONS
    }

    exact_ev = overall["exact"]["expectedValueU"]
    delayed_ev = overall["delayed"]["expectedValueU"]
    delay_retention = (
        delayed_ev / exact_ev
        if exact_ev is not None and exact_ev > 0.0 and delayed_ev is not None
        else None
    )
    gates = {
        "minimumTrades": all(overall[key]["trades"] >= MIN_TRADES for key in EXECUTIONS),
        "wilsonAboveBreakEven": all(
            overall[key]["wilson95LowerPct"] is not None
            and overall[key]["wilson95LowerPct"] > BREAKEVEN_WR
            for key in EXECUTIONS
        ),
        "positiveOverall": all(overall[key]["pnlU"] > 0.0 for key in EXECUTIONS),
        "positiveActiveMonths": all(
            (overall[key]["positiveActiveMonthPct"] or 0.0)
            >= MIN_POSITIVE_ACTIVE_MONTH_PCT
            for key in EXECUTIONS
        ),
        "allBroadPeriodsPositive": all(
            periods[period][key]["pnlU"] > 0.0
            for period in periods
            for key in EXECUTIONS
        ),
        "allCompleteYearsPositive": all(
            years[year][key]["pnlU"] > 0.0
            for year in years
            for key in EXECUTIONS
        ),
        "phaseStability": all(
            positive_phase_pct[key] >= MIN_POSITIVE_PHASE_PCT
            for key in EXECUTIONS
        ),
        "delayEvRetention": (
            delay_retention is not None and delay_retention >= MIN_DELAY_EV_RETENTION
        ),
    }
    passed = all(gates.values())
    return (
        {
            "profile": profile,
            "volState": state,
            "overall": overall,
            "periods": periods,
            "years": years,
            "phases": phases,
            "positivePhasePct": positive_phase_pct,
            "delayedVsExactEvRetention": (
                round(float(delay_retention), 6) if delay_retention is not None else None
            ),
            "gates": gates,
            "passed": passed,
            "decision": "minute_candidate_only" if passed else "no_trade",
        },
        trades,
    )


def _flat_audit(row: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {
        "profile": row["profile"],
        "vol_state": row["volState"],
        "passed": row["passed"],
        "decision": row["decision"],
        "delayed_vs_exact_ev_retention": row["delayedVsExactEvRetention"],
    }
    for execution in EXECUTIONS:
        for key, value in row["overall"][execution].items():
            output[f"{execution}_{key}"] = value
        output[f"{execution}_positive_phase_pct"] = row["positivePhasePct"][execution]
    output.update({f"gate_{key}": value for key, value in row["gates"].items()})
    return output


def run() -> dict[str, Any]:
    btc, btc_audits = load_frozen_history(BTC_INPUTS, BTC_MANIFESTS)
    eth_frozen = verify_frozen_input(ETH_INPUT, ETH_MANIFEST)
    eth = load_minutes(ETH_INPUT)[["open", "high", "low", "close", "volume"]]
    overlap = btc.index.intersection(eth.index)
    btc = btc.loc[overlap]
    eth = eth.loc[overlap]
    if len(overlap) != len(eth) or not overlap.to_series().diff().dropna().eq(pd.Timedelta(minutes=1)).all():
        raise ValueError("BTC/ETH overlap must be a continuous one-minute panel")

    volatility = build_volatility_states(btc, VOLATILITY_WINDOW_MIN)
    features = build_features(btc, eth)
    candidates = build_candidates(btc, features, volatility)
    months = _month_keys(overlap)

    results: dict[str, Any] = {}
    audit_rows: list[dict[str, Any]] = []
    trade_parts: list[pd.DataFrame] = []
    for state in STATES:
        results[state] = {}
        for profile in PROFILE_NAMES:
            row, trades = audit_profile(
                candidates, profile=profile, state=state, months=months
            )
            results[state][profile] = row
            audit_rows.append(_flat_audit(row))
            if not trades.empty:
                trade_parts.append(trades.assign(audit_state=state))

    audit = pd.DataFrame(audit_rows)
    trades_out = pd.concat(trade_parts, ignore_index=True) if trade_parts else pd.DataFrame()
    audit.to_csv(OUT_AUDIT, index=False, encoding="utf-8-sig")
    trades_out.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    passed = audit.loc[audit["passed"], ["vol_state", "profile"]].to_dict("records")
    action_by_state = {
        state: (
            "minute_candidate_only"
            if any(item["vol_state"] == state for item in passed)
            else "no_trade"
        )
        for state in STATES
    }
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "status": "V36_FIXED_CROSS_MARKET_NORMAL_ROUTER",
        "safety": {
            "researchOnly": True,
            "deploymentPerformed": False,
            "onlineConfigurationChanged": False,
            "shadowUpdated": False,
            "realTradingAllowed": False,
        },
        "data": {
            "btcInputs": btc_audits,
            "ethInput": {
                "path": str(ETH_INPUT.resolve()),
                **eth_frozen,
            },
            "overlapRows": int(len(overlap)),
            "start": overlap[0],
            "end": overlap[-1],
            "missingMinutes": 0,
        },
        "design": {
            "btcNormalWindowMin": NORMAL_WINDOW_MIN,
            "btcNormalThresholdSigma": NORMAL_THRESHOLD,
            "btcVolatilityWindowMin": VOLATILITY_WINDOW_MIN,
            "holdingMin": HORIZON_MIN,
            "executions": list(EXECUTIONS),
            "profiles": list(PROFILE_NAMES),
            "outcomeSelectedProfiles": False,
            "crossMarketFeatures": [
                "causal rolling BTC/ETH beta",
                "ten-minute residual z-score",
                "ETH one/three-minute turn confirmation",
                "standardized common-shock agreement",
            ],
            "volatilitySummary": volatility_summary(volatility),
            "phaseAudit": "all ten minute-of-decade phases are evaluated",
            "acceptance": {
                "minimumTrades": MIN_TRADES,
                "wilson95LowerMustExceedPct": BREAKEVEN_WR,
                "minimumPositiveActiveMonthPct": MIN_POSITIVE_ACTIVE_MONTH_PCT,
                "minimumPositivePhasePct": MIN_POSITIVE_PHASE_PCT,
                "minimumDelayedVsExactEvRetention": MIN_DELAY_EV_RETENTION,
                "allBroadPeriodsAndCompleteYearsMustBePositive": True,
            },
        },
        "candidateRows": int(len(candidates)),
        "audit": results,
        "decision": {
            "passedStateProfiles": passed,
            "actionByVolatilityState": action_by_state,
            "minutePlatformPassed": bool(passed),
            "secondLevelUpgradeAllowed": False,
            "action": "second_execution_validation_required" if passed else "no_trade",
            "reason": (
                "Any minute survivor still needs continuous second/order-flow replay."
                if passed
                else "No fixed cross-market profile passed execution, period, year and phase stability together."
            ),
        },
        "outputs": {
            "json": str(OUT_JSON.resolve()),
            "auditCsv": str(OUT_AUDIT.resolve()),
            "tradesCsv": str(OUT_TRADES.resolve()),
        },
    }
    OUT_JSON.write_text(
        json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> int:
    report = run()
    print(
        json.dumps(
            clean(
                {
                    "status": report["status"],
                    "data": report["data"],
                    "candidateRows": report["candidateRows"],
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
