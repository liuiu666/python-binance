"""V37 pre-registered ETH/BTC walk-forward router on V36 normal-tail events.

The model predicts the delayed-entry reversion outcome with a fixed, small
linear feature set.  It is retrained on prior complete calendar months only.
Research only: passing remains a minute-level candidate, never deployment.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from research_cross_market_normal_router_v36 import (
    BREAKEVEN_WR,
    EXECUTIONS,
    HORIZON_MIN,
    MIN_DELAY_EV_RETENTION,
    MIN_POSITIVE_ACTIVE_MONTH_PCT,
    MIN_POSITIVE_PHASE_PCT,
    STATES,
)
from research_full_regime_action_matrix_v25 import (
    apply_horizon_cooldown,
    metrics_from_signed,
)
from research_minute_volatility_normal_v15 import clean


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "tmp" / "v36_cross_market_normal_router_trades_20260730.csv"
OUT_JSON = ROOT / "tmp" / "v37_cross_market_walkforward_20260730.json"
OUT_AUDIT = ROOT / "tmp" / "v37_cross_market_walkforward_audit_20260730.csv"
OUT_PREDICTIONS = ROOT / "tmp" / "v37_cross_market_walkforward_predictions_20260730.csv"
OUT_TRADES = ROOT / "tmp" / "v37_cross_market_walkforward_trades_20260730.csv"

TRAINING_WINDOWS_MONTHS = (12, 24, 36)
PROBABILITY_EDGE = 0.60
MODEL_PROFILES = ("reversion_only", "symmetric_reversion_or_continuation")
MIN_SCOPE_TRADES = {"all": 500, "low": 300, "mid": 300, "high": 300}
FEATURES = (
    "normal_extremity",
    "signed_residual_extremity",
    "signed_btc_shock",
    "signed_eth_shock",
    "shock_divergence",
    "absolute_residual_z",
    "direction",
    "state_low",
    "state_mid",
    "state_high",
)


def load_events(path: str | Path = INPUT) -> pd.DataFrame:
    data = pd.read_csv(path, parse_dates=["signal_time"])
    data = data.loc[data["profile"].eq("btc_direct_reversion_control")].copy()
    data = data.sort_values(["signal_pos", "vol_state"], kind="stable")
    data = data.drop_duplicates("signal_pos", keep="first").reset_index(drop=True)
    direction = pd.to_numeric(data["direction"], errors="raise").astype(np.int8)
    data["normal_extremity"] = -direction * pd.to_numeric(data["normal_z"], errors="coerce")
    data["signed_residual_extremity"] = -direction * pd.to_numeric(
        data["residual_z"], errors="coerce"
    )
    data["signed_btc_shock"] = -direction * pd.to_numeric(
        data["btc_shock"], errors="coerce"
    )
    data["signed_eth_shock"] = -direction * pd.to_numeric(
        data["eth_shock"], errors="coerce"
    )
    data["shock_divergence"] = data["signed_btc_shock"] - data["signed_eth_shock"]
    data["absolute_residual_z"] = pd.to_numeric(data["residual_z"], errors="coerce").abs()
    data["direction"] = direction
    for state in STATES:
        data[f"state_{state}"] = data["vol_state"].astype(str).eq(state).astype(np.int8)
    data["month"] = data["signal_time"].dt.strftime("%Y-%m")
    return data


def make_model() -> Pipeline:
    return Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
            (
                "model",
                LogisticRegression(
                    C=0.1,
                    solver="lbfgs",
                    max_iter=2000,
                    random_state=137,
                ),
            ),
        ]
    )


def walkforward_predictions(data: pd.DataFrame, training_months: int) -> pd.DataFrame:
    month_values = [pd.Period(value, freq="M") for value in sorted(data["month"].unique())]
    parts: list[pd.DataFrame] = []
    for test_period in month_values:
        train_periods = list(
            pd.period_range(test_period - training_months, test_period - 1, freq="M")
        )
        if len(train_periods) != training_months:
            continue
        observed = set(data["month"].unique())
        if any(str(period) not in observed for period in train_periods):
            continue
        test_start = pd.Timestamp(test_period.start_time, tz="UTC")
        train = data.loc[data["month"].isin([str(period) for period in train_periods])].copy()
        train = train.loc[
            train["signal_time"] + pd.Timedelta(minutes=HORIZON_MIN + 2) < test_start
        ]
        test = data.loc[data["month"].eq(str(test_period))].copy()
        if len(train) < 2000 or test.empty:
            continue
        signed = pd.to_numeric(train["signed_bps_delayed"], errors="coerce")
        valid = signed.notna() & signed.ne(0.0)
        train = train.loc[valid]
        labels = signed.loc[valid].gt(0.0).astype(np.int8)
        if labels.nunique() < 2:
            continue
        model = make_model()
        model.fit(train[list(FEATURES)], labels)
        test["reversion_probability"] = model.predict_proba(test[list(FEATURES)])[:, 1]
        test["training_window_months"] = training_months
        test["train_start_month"] = str(train_periods[0])
        test["train_end_month"] = str(train_periods[-1])
        parts.append(test)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def route_predictions(predictions: pd.DataFrame, profile: str) -> pd.DataFrame:
    probability = pd.to_numeric(predictions["reversion_probability"], errors="coerce")
    if profile == "reversion_only":
        selected = predictions.loc[probability.ge(PROBABILITY_EDGE)].copy()
        selected["routed_action"] = "reversion"
    elif profile == "symmetric_reversion_or_continuation":
        use_reversion = probability.ge(PROBABILITY_EDGE)
        use_continuation = probability.le(1.0 - PROBABILITY_EDGE)
        selected = predictions.loc[use_reversion | use_continuation].copy()
        flip = use_continuation.loc[selected.index].to_numpy(bool)
        selected["routed_action"] = np.where(flip, "continuation", "reversion")
        selected.loc[flip, "direction"] = -pd.to_numeric(
            selected.loc[flip, "direction"], errors="raise"
        ).astype(np.int8)
        for execution in EXECUTIONS:
            column = f"signed_bps_{execution}"
            selected.loc[flip, column] = -pd.to_numeric(
                selected.loc[flip, column], errors="coerce"
            )
    else:
        raise ValueError(f"unknown model profile: {profile}")
    selected["model_profile"] = profile
    return apply_horizon_cooldown(selected, HORIZON_MIN)


def _metrics(frame: pd.DataFrame, months: list[str] | None = None) -> dict[str, Any]:
    return {
        execution: metrics_from_signed(
            frame, f"signed_bps_{execution}", calendar_months=months
        )
        for execution in EXECUTIONS
    }


def _time_slice(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    time = pd.to_datetime(frame["signal_time"], utc=True)
    return frame.loc[
        time.ge(pd.Timestamp(start)) & time.lt(pd.Timestamp(end))
    ].copy()


def audit_scope(
    trades: pd.DataFrame,
    *,
    scope: str,
    training_months: int,
    profile: str,
) -> dict[str, Any]:
    scoped = trades if scope == "all" else trades.loc[trades["vol_state"].astype(str).eq(scope)]
    months = sorted(scoped["signal_time"].dt.strftime("%Y-%m").unique())
    overall = _metrics(scoped, months)
    start_year = 2020 + int(np.ceil(training_months / 12.0))
    years = {
        str(year): _metrics(
            _time_slice(
                scoped,
                f"{year}-01-01T00:00:00Z",
                f"{year + 1}-01-01T00:00:00Z",
            )
        )
        for year in range(start_year, 2026)
    }
    recent = _metrics(
        _time_slice(scoped, "2024-01-01T00:00:00Z", "2026-07-01T00:00:00Z")
    )
    phases = {
        str(phase): _metrics(scoped.loc[scoped["phase"].eq(phase)])
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
    retention = (
        delayed_ev / exact_ev
        if exact_ev is not None and exact_ev > 0.0 and delayed_ev is not None
        else None
    )
    gates = {
        "minimumTrades": all(
            overall[key]["trades"] >= MIN_SCOPE_TRADES[scope] for key in EXECUTIONS
        ),
        "wilsonAboveBreakEven": all(
            overall[key]["wilson95LowerPct"] is not None
            and overall[key]["wilson95LowerPct"] > BREAKEVEN_WR
            for key in EXECUTIONS
        ),
        "positiveOverall": all(overall[key]["pnlU"] > 0.0 for key in EXECUTIONS),
        "positiveRecent": all(recent[key]["pnlU"] > 0.0 for key in EXECUTIONS),
        "positiveActiveMonths": all(
            (overall[key]["positiveActiveMonthPct"] or 0.0)
            >= MIN_POSITIVE_ACTIVE_MONTH_PCT
            for key in EXECUTIONS
        ),
        "allOosYearsPositive": bool(years)
        and all(
            years[year][key]["trades"] > 0 and years[year][key]["pnlU"] > 0.0
            for year in years
            for key in EXECUTIONS
        ),
        "phaseStability": all(
            positive_phase_pct[key] >= MIN_POSITIVE_PHASE_PCT
            for key in EXECUTIONS
        ),
        "delayEvRetention": retention is not None
        and retention >= MIN_DELAY_EV_RETENTION,
    }
    passed = all(gates.values())
    return {
        "scope": scope,
        "trainingWindowMonths": training_months,
        "modelProfile": profile,
        "overall": overall,
        "recent2024To2026": recent,
        "years": years,
        "phases": phases,
        "positivePhasePct": positive_phase_pct,
        "delayedVsExactEvRetention": round(float(retention), 6)
        if retention is not None
        else None,
        "gates": gates,
        "passed": passed,
        "decision": "minute_candidate_only" if passed else "no_trade",
    }


def _flat(row: dict[str, Any]) -> dict[str, Any]:
    output = {
        "scope": row["scope"],
        "training_window_months": row["trainingWindowMonths"],
        "model_profile": row["modelProfile"],
        "passed": row["passed"],
        "decision": row["decision"],
        "delayed_vs_exact_ev_retention": row["delayedVsExactEvRetention"],
    }
    for execution in EXECUTIONS:
        for key, value in row["overall"][execution].items():
            output[f"{execution}_{key}"] = value
        output[f"{execution}_recent_pnlU"] = row["recent2024To2026"][execution]["pnlU"]
        output[f"{execution}_positive_phase_pct"] = row["positivePhasePct"][execution]
    output.update({f"gate_{key}": value for key, value in row["gates"].items()})
    return output


def run(path: str | Path = INPUT) -> dict[str, Any]:
    events = load_events(path)
    predictions_by_window: dict[int, pd.DataFrame] = {}
    prediction_parts: list[pd.DataFrame] = []
    trade_parts: list[pd.DataFrame] = []
    audit_rows: list[dict[str, Any]] = []
    audit_nested: dict[str, Any] = {}
    for training_months in TRAINING_WINDOWS_MONTHS:
        predictions = walkforward_predictions(events, training_months)
        predictions_by_window[training_months] = predictions
        prediction_parts.append(predictions)
        mode = f"rolling_{training_months}m"
        audit_nested[mode] = {}
        for profile in MODEL_PROFILES:
            trades = route_predictions(predictions, profile)
            trade_parts.append(trades)
            audit_nested[mode][profile] = {}
            for scope in ("all", *STATES):
                row = audit_scope(
                    trades,
                    scope=scope,
                    training_months=training_months,
                    profile=profile,
                )
                audit_nested[mode][profile][scope] = row
                audit_rows.append(_flat(row))

    all_predictions = pd.concat(prediction_parts, ignore_index=True)
    all_trades = pd.concat(trade_parts, ignore_index=True)
    all_predictions.to_csv(OUT_PREDICTIONS, index=False, encoding="utf-8-sig")
    all_trades.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    audit = pd.DataFrame(audit_rows)
    audit.to_csv(OUT_AUDIT, index=False, encoding="utf-8-sig")

    robust_profiles = []
    for profile in MODEL_PROFILES:
        for scope in ("all", *STATES):
            rows = audit.loc[
                audit["model_profile"].eq(profile) & audit["scope"].eq(scope)
            ]
            if len(rows) == len(TRAINING_WINDOWS_MONTHS) and rows["passed"].all():
                robust_profiles.append({"profile": profile, "scope": scope})
    action_by_state = {
        state: (
            "minute_candidate_only"
            if any(item["scope"] == state for item in robust_profiles)
            else "no_trade"
        )
        for state in STATES
    }
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "status": "V37_CAUSAL_CROSS_MARKET_WALKFORWARD",
        "safety": {
            "researchOnly": True,
            "deploymentPerformed": False,
            "onlineConfigurationChanged": False,
            "shadowUpdated": False,
            "realTradingAllowed": False,
        },
        "data": {
            "input": str(Path(path).resolve()),
            "events": int(len(events)),
            "start": events["signal_time"].min(),
            "end": events["signal_time"].max(),
        },
        "design": {
            "trainingWindowsMonths": list(TRAINING_WINDOWS_MONTHS),
            "testFrequency": "one calendar month",
            "purge": f"labels ending within {HORIZON_MIN + 2} minutes of test month are removed",
            "model": "L2 logistic regression C=0.1 with median imputation and standardization",
            "features": list(FEATURES),
            "trainedTarget": "one-minute-delayed ten-minute reversion win",
            "probabilityEdge": PROBABILITY_EDGE,
            "profiles": list(MODEL_PROFILES),
            "parameterSearch": False,
            "futureMonthLabelsUsedInTraining": False,
        },
        "audit": audit_nested,
        "decision": {
            "robustProfiles": robust_profiles,
            "actionByVolatilityState": action_by_state,
            "minutePlatformPassed": bool(robust_profiles),
            "secondLevelUpgradeAllowed": False,
            "action": "second_execution_validation_required"
            if robust_profiles
            else "no_trade",
        },
        "outputs": {
            "json": str(OUT_JSON.resolve()),
            "auditCsv": str(OUT_AUDIT.resolve()),
            "predictionsCsv": str(OUT_PREDICTIONS.resolve()),
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
