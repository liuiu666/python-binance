"""Causal minute-volatility regimes with state-specific normal reversion.

The final 2026-07-01 onward futures period is never used to choose a profile.
All volatility thresholds are trailing and shifted, so future minutes cannot
change an already assigned state.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data" / "btcusdt_futures_1m_20260131_20260730.csv"
OUT_JSON = ROOT / "tmp" / "v15_minute_volatility_normal_20260730.json"
OUT_TRADES = ROOT / "tmp" / "v15_minute_volatility_normal_20260730_trades.csv"
OUT_CANDIDATES = ROOT / "tmp" / "v15_minute_volatility_normal_20260730_candidates.csv"

STATES = ("low", "mid", "high")
DELAYS_MIN = (0, 1, 2)
HORIZON_MIN = 10
PAYOUT_RATE = 0.8
AMOUNT_U = 5.0
BREAKEVEN_WR = 100.0 / (1.0 + PAYOUT_RATE)
HOLDOUT_START = pd.Timestamp("2026-07-01T00:00:00Z")
VOL_WINDOW_MIN = 60
VOL_HISTORY_MIN = 7 * 24 * 60
VOL_HISTORY_MIN_PERIODS = 3 * 24 * 60
VOL_LOW_QUANTILE = 1.0 / 3.0
VOL_HIGH_QUANTILE = 2.0 / 3.0


@dataclass(frozen=True)
class NormalProfile:
    name: str
    window_min: int
    mode: str
    z_entry: float
    z_reclaim: float = 0.75
    retest_min: int = 20
    inside_min: float = 0.45
    inside_max: float = 0.82


PROFILES = tuple(
    NormalProfile(
        name=f"{mode}_w{window}_z{str(z).replace('.', 'p')}",
        window_min=window,
        mode=mode,
        z_entry=z,
        retest_min=max(10, window // 3),
    )
    for mode in ("edge", "reclaim")
    for window in (30, 60, 120)
    for z in (1.25, 1.75, 2.25)
)


DEVELOPMENT_FOLDS = (
    ("2026-04", pd.Timestamp("2026-04-01T00:00:00Z"), pd.Timestamp("2026-05-01T00:00:00Z")),
    ("2026-05", pd.Timestamp("2026-05-01T00:00:00Z"), pd.Timestamp("2026-06-01T00:00:00Z")),
    ("2026-06", pd.Timestamp("2026-06-01T00:00:00Z"), HOLDOUT_START),
)


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if value is pd.NaT:
        return None
    return value


def load_minutes(path: str | Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    required = {"open_time", "open", "high", "low", "close", "volume"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"minute input missing columns: {missing}")
    if "market" not in frame.columns:
        raise ValueError("minute input must declare market=futures")
    markets = {str(value).lower().strip() for value in frame["market"].dropna().unique()}
    if markets != {"futures"}:
        raise ValueError(f"only futures minutes are accepted, got {sorted(markets)}")
    frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True, errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["open_time", "open", "high", "low", "close"])
    frame = frame.loc[
        frame[["open", "high", "low", "close"]].gt(0.0).all(axis=1)
    ].sort_values("open_time")
    frame = frame.drop_duplicates("open_time", keep="last").set_index("open_time")
    if frame.empty or frame.index.has_duplicates:
        raise ValueError("minute input has no unique usable rows")
    steps = frame.index.to_series().diff().dt.total_seconds().dropna()
    if len(steps) and not steps.eq(60.0).all():
        raise ValueError("minute input must be contiguous; split gaps before research")
    return frame


def build_volatility_states(minutes: pd.DataFrame) -> pd.DataFrame:
    close = minutes["close"].astype(float)
    log_return = np.log(close / close.shift(1))
    rv = log_return.rolling(VOL_WINDOW_MIN, min_periods=VOL_WINDOW_MIN).std(ddof=0)
    rv = rv * math.sqrt(HORIZON_MIN) * 10_000.0
    prior = rv.shift(1)
    low = prior.rolling(VOL_HISTORY_MIN, min_periods=VOL_HISTORY_MIN_PERIODS).quantile(
        VOL_LOW_QUANTILE
    )
    high = prior.rolling(VOL_HISTORY_MIN, min_periods=VOL_HISTORY_MIN_PERIODS).quantile(
        VOL_HIGH_QUANTILE
    )
    state = pd.Series("unknown", index=minutes.index, dtype="object")
    ready = rv.notna() & low.notna() & high.notna()
    state.loc[ready & rv.le(low)] = "low"
    state.loc[ready & rv.gt(low) & rv.lt(high)] = "mid"
    state.loc[ready & rv.ge(high)] = "high"
    return pd.DataFrame(
        {
            "rv10m_bps": rv,
            "prior_q33_bps": low,
            "prior_q67_bps": high,
            "vol_state": state,
            "vol_ratio_to_q67": rv / high.replace(0.0, np.nan),
        },
        index=minutes.index,
    )


def build_normal_features(minutes: pd.DataFrame, profile: NormalProfile) -> pd.DataFrame:
    close = minutes["close"].astype(float)
    past_close = close.shift(1)
    center = past_close.rolling(profile.window_min, min_periods=profile.window_min).mean()
    sigma = past_close.rolling(profile.window_min, min_periods=profile.window_min).std(ddof=1)
    z = (close - center) / sigma.replace(0.0, np.nan)
    prior_z = z.shift(1)
    inside = prior_z.abs().le(1.0).astype(float).rolling(
        profile.window_min,
        min_periods=profile.window_min,
    ).mean()
    return pd.DataFrame(
        {
            "center": center,
            "sigma": sigma,
            "sigma_bps": sigma / close * 10_000.0,
            "z": z,
            "inside1_ratio": inside,
            "past_z_max": prior_z.rolling(
                profile.retest_min,
                min_periods=max(5, profile.retest_min // 2),
            ).max(),
            "past_z_min": prior_z.rolling(
                profile.retest_min,
                min_periods=max(5, profile.retest_min // 2),
            ).min(),
            "ret_1m_bps": np.log(close / close.shift(1)) * 10_000.0,
            "ret_30m_bps": np.log(close / close.shift(30)) * 10_000.0,
            "ret_120m_bps": np.log(close / close.shift(120)) * 10_000.0,
        },
        index=minutes.index,
    )


def _boundary_mask(index: pd.DatetimeIndex) -> np.ndarray:
    signal_times = index + pd.Timedelta(minutes=1)
    return (signal_times.minute % 10 == 0) & (signal_times.second == 0)


def generate_profile_candidates(
    minutes: pd.DataFrame,
    volatility: pd.DataFrame,
    profile: NormalProfile,
) -> pd.DataFrame:
    features = build_normal_features(minutes, profile)
    valid_shape = features["inside1_ratio"].between(profile.inside_min, profile.inside_max)
    if profile.mode == "edge":
        up = valid_shape & features["z"].le(-profile.z_entry)
        down = valid_shape & features["z"].ge(profile.z_entry)
    elif profile.mode == "reclaim":
        up = (
            valid_shape
            & features["past_z_min"].le(-profile.z_entry)
            & features["z"].between(-profile.z_reclaim, 0.0)
            & features["ret_1m_bps"].gt(0.0)
        )
        down = (
            valid_shape
            & features["past_z_max"].ge(profile.z_entry)
            & features["z"].between(0.0, profile.z_reclaim)
            & features["ret_1m_bps"].lt(0.0)
        )
    else:
        raise ValueError(f"unknown normal mode {profile.mode!r}")
    selected = (up | down) & _boundary_mask(minutes.index)
    selected &= volatility["vol_state"].isin(STATES)
    positions = np.flatnonzero(selected.to_numpy(bool))
    rows: list[dict[str, Any]] = []
    opens = minutes["open"].to_numpy(float)
    for position in positions:
        row = {
            "profile": profile.name,
            "window_min": profile.window_min,
            "mode": profile.mode,
            "z_entry": profile.z_entry,
            "z_reclaim": profile.z_reclaim,
            "signal_bar_time": minutes.index[position],
            "signal_time": minutes.index[position] + pd.Timedelta(minutes=1),
            "signal": "UP" if bool(up.iloc[position]) else "DOWN",
            "vol_state": str(volatility["vol_state"].iloc[position]),
            "rv10m_bps": float(volatility["rv10m_bps"].iloc[position]),
            "prior_q33_bps": float(volatility["prior_q33_bps"].iloc[position]),
            "prior_q67_bps": float(volatility["prior_q67_bps"].iloc[position]),
            "z": float(features["z"].iloc[position]),
            "inside1_ratio": float(features["inside1_ratio"].iloc[position]),
            "sigma_bps": float(features["sigma_bps"].iloc[position]),
            "ret_30m_bps": float(features["ret_30m_bps"].iloc[position]),
            "ret_120m_bps": float(features["ret_120m_bps"].iloc[position]),
        }
        direction = 1.0 if row["signal"] == "UP" else -1.0
        for delay in DELAYS_MIN:
            entry_position = position + 1 + delay
            settle_position = entry_position + HORIZON_MIN
            if settle_position >= len(minutes):
                row[f"status_d{delay}"] = "missing"
                row[f"signed_bps_d{delay}"] = np.nan
                row[f"pnl_u_d{delay}"] = np.nan
                continue
            entry = float(opens[entry_position])
            settle = float(opens[settle_position])
            signed = (settle / entry - 1.0) * 10_000.0 * direction
            row[f"entry_time_d{delay}"] = minutes.index[entry_position]
            row[f"settle_time_d{delay}"] = minutes.index[settle_position]
            row[f"entry_d{delay}"] = entry
            row[f"settle_d{delay}"] = settle
            row[f"signed_bps_d{delay}"] = signed
            row[f"status_d{delay}"] = "won" if signed > 0.0 else "lost" if signed < 0.0 else "tie"
            row[f"pnl_u_d{delay}"] = AMOUNT_U * PAYOUT_RATE if signed > 0.0 else -AMOUNT_U if signed < 0.0 else 0.0
        rows.append(row)
    return pd.DataFrame(rows)


def apply_shared_cooldown(frame: pd.DataFrame, cooldown_min: int = HORIZON_MIN) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    kept: list[dict[str, Any]] = []
    last: pd.Timestamp | None = None
    for row in frame.sort_values(["signal_time", "profile"], kind="stable").to_dict("records"):
        timestamp = pd.Timestamp(row["signal_time"])
        if last is not None and (timestamp - last).total_seconds() < cooldown_min * 60:
            continue
        kept.append(row)
        last = timestamp
    return pd.DataFrame(kept)


def wilson_lower(wins: int, decided: int) -> float | None:
    if decided <= 0:
        return None
    z = 1.959963984540054
    p = wins / decided
    denominator = 1.0 + z * z / decided
    center = p + z * z / (2.0 * decided)
    radius = z * math.sqrt(p * (1.0 - p) / decided + z * z / (4.0 * decided**2))
    return max(0.0, (center - radius) / denominator)


def metrics(frame: pd.DataFrame, delay: int = 0) -> dict[str, Any]:
    if frame.empty:
        return {
            "trades": 0, "wins": 0, "losses": 0, "ties": 0,
            "winRatePct": None, "wilson95LowerPct": None, "pnlU": 0.0,
            "expectedValueU": None, "maxDrawdownU": 0.0,
            "maxLossStreak": 0, "positiveDayPct": None,
        }
    status = frame[f"status_d{delay}"].astype(str)
    settled = frame.loc[status.isin(("won", "lost", "tie"))].copy()
    if settled.empty:
        return metrics(pd.DataFrame(), delay)
    status = settled[f"status_d{delay}"].astype(str)
    wins = int(status.eq("won").sum())
    losses = int(status.eq("lost").sum())
    ties = int(status.eq("tie").sum())
    decided = wins + losses
    pnl = pd.to_numeric(settled[f"pnl_u_d{delay}"], errors="coerce").fillna(0.0).to_numpy(float)
    equity = np.r_[0.0, np.cumsum(pnl)]
    drawdown = np.maximum.accumulate(equity) - equity
    streak = maximum = 0
    for value in status:
        streak = streak + 1 if value == "lost" else 0
        maximum = max(maximum, streak)
    local_day = pd.to_datetime(settled["signal_time"], utc=True).dt.tz_convert("Asia/Shanghai").dt.date
    daily = pd.Series(pnl).groupby(local_day.reset_index(drop=True)).sum()
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
        "positiveDayPct": round(float(daily.gt(0.0).mean()) * 100.0, 4) if len(daily) else None,
        "days": int(len(daily)),
    }


def select_profile(
    candidates: pd.DataFrame,
    state: str,
    train_end: pd.Timestamp,
    *,
    train_start: pd.Timestamp | None = None,
) -> dict[str, Any] | None:
    frame = candidates.loc[
        candidates["vol_state"].eq(state)
        & candidates["signal_time"].lt(train_end)
    ]
    if train_start is not None:
        frame = frame.loc[frame["signal_time"].ge(train_start)]
    ranked: list[tuple[tuple[float, ...], str, dict[str, Any]]] = []
    for profile, group in frame.groupby("profile", sort=True):
        executed = apply_shared_cooldown(group)
        summary = metrics(executed, 0)
        eligible = (
            summary["trades"] >= 40
            and summary["pnlU"] > 0.0
            and summary["winRatePct"] is not None
            and summary["winRatePct"] > BREAKEVEN_WR
            and summary["positiveDayPct"] is not None
            and summary["positiveDayPct"] >= 45.0
        )
        if not eligible:
            continue
        score = (
            float(summary["wilson95LowerPct"] or 0.0),
            float(summary["positiveDayPct"] or 0.0),
            float(summary["pnlU"]),
            -float(summary["maxDrawdownU"]),
            float(summary["trades"]),
        )
        ranked.append((score, str(profile), summary))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    score, profile, summary = ranked[0]
    return {"profile": profile, "trainMetrics": summary, "selectionScore": list(score)}


def mapped_candidates(
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
    combined = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=candidates.columns)
    return apply_shared_cooldown(combined)


def state_audit(volatility: pd.DataFrame) -> dict[str, Any]:
    ready = volatility.loc[volatility["vol_state"].isin(STATES)].copy()
    rows = {}
    for state, group in ready.groupby("vol_state", sort=True):
        rows[str(state)] = {
            "minutes": int(len(group)),
            "sharePct": round(len(group) / len(ready) * 100.0, 4),
            "medianRv10mBps": round(float(group["rv10m_bps"].median()), 4),
            "p10Rv10mBps": round(float(group["rv10m_bps"].quantile(0.10)), 4),
            "p90Rv10mBps": round(float(group["rv10m_bps"].quantile(0.90)), 4),
        }
    state = ready["vol_state"]
    runs = state.ne(state.shift(1)).cumsum()
    run_lengths = ready.groupby(runs).agg(state=("vol_state", "first"), minutes=("vol_state", "size"))
    for name in STATES:
        part = run_lengths.loc[run_lengths["state"].eq(name), "minutes"]
        if name in rows:
            rows[name]["medianRunMinutes"] = round(float(part.median()), 2) if len(part) else None
            rows[name]["p90RunMinutes"] = round(float(part.quantile(0.90)), 2) if len(part) else None
    transitions = pd.crosstab(state.shift(1), state, normalize="index") * 100.0
    return {
        "definition": "60-minute realized sigma scaled to 10-minute horizon; low/mid/high from prior seven-day 33/67 percentiles",
        "futureLeakage": "state thresholds use rv.shift(1) only",
        "readyMinutes": int(len(ready)),
        "byState": rows,
        "transitionPct": {
            str(index): {str(column): round(float(value), 4) for column, value in row.items()}
            for index, row in transitions.to_dict("index").items()
        },
    }


def summarize_mapping(frame: pd.DataFrame) -> dict[str, Any]:
    return {
        "byDelay": {str(delay): metrics(frame, delay) for delay in DELAYS_MIN},
        "byStateDelay0": {
            str(state): metrics(group, 0)
            for state, group in frame.groupby("vol_state", sort=True)
        } if not frame.empty else {},
        "byDirectionDelay0": {
            str(signal): metrics(group, 0)
            for signal, group in frame.groupby("signal", sort=True)
        } if not frame.empty else {},
    }


def run(input_path: str | Path) -> dict[str, Any]:
    minutes = load_minutes(input_path)
    volatility = build_volatility_states(minutes)
    candidate_parts = []
    for profile in PROFILES:
        candidate_parts.append(generate_profile_candidates(minutes, volatility, profile))
    candidates = pd.concat(candidate_parts, ignore_index=True).sort_values(
        ["signal_time", "profile"], kind="stable"
    ).reset_index(drop=True)

    folds = []
    fold_trades = []
    first_time = pd.Timestamp(minutes.index[0])
    for name, test_start, test_end in DEVELOPMENT_FOLDS:
        selections = {
            state: select_profile(candidates, state, test_start)
            for state in STATES
        }
        mapping = {
            state: selection["profile"] if selection is not None else None
            for state, selection in selections.items()
        }
        trades = mapped_candidates(candidates, mapping, test_start, test_end)
        if not trades.empty:
            tagged = trades.copy()
            tagged["evaluation"] = f"development_fold_{name}"
            fold_trades.append(tagged)
        folds.append({
            "name": name,
            "trainStart": first_time,
            "trainEndExclusive": test_start,
            "testStart": test_start,
            "testEndExclusive": test_end,
            "selection": selections,
            "mapping": mapping,
            "test": summarize_mapping(trades),
        })

    final_selections = {
        state: select_profile(candidates, state, HOLDOUT_START)
        for state in STATES
    }
    final_mapping = {
        state: selection["profile"] if selection is not None else None
        for state, selection in final_selections.items()
    }
    holdout_end = pd.Timestamp(minutes.index[-1]) + pd.Timedelta(minutes=1)
    holdout = mapped_candidates(candidates, final_mapping, HOLDOUT_START, holdout_end)
    if not holdout.empty:
        tagged = holdout.copy()
        tagged["evaluation"] = "sealed_final_holdout"
        fold_trades.append(tagged)

    fixed_profile_development = {}
    fixed_profile_holdout = {}
    for state in STATES:
        fixed_profile_development[state] = {}
        fixed_profile_holdout[state] = {}
        for profile in PROFILES:
            dev = apply_shared_cooldown(candidates.loc[
                candidates["vol_state"].eq(state)
                & candidates["profile"].eq(profile.name)
                & candidates["signal_time"].lt(HOLDOUT_START)
            ])
            test = apply_shared_cooldown(candidates.loc[
                candidates["vol_state"].eq(state)
                & candidates["profile"].eq(profile.name)
                & candidates["signal_time"].ge(HOLDOUT_START)
            ])
            fixed_profile_development[state][profile.name] = metrics(dev, 0)
            fixed_profile_holdout[state][profile.name] = metrics(test, 0)

    holdout_summary = summarize_mapping(holdout)
    holdout_delays = holdout_summary["byDelay"]
    screen_gates = {
        "atLeast50HoldoutTrades": min(
            (int(row["trades"]) for row in holdout_delays.values()), default=0
        ) >= 50,
        "allMinuteDelaysPositive": bool(holdout_delays) and all(
            float(row["pnlU"]) > 0.0 for row in holdout_delays.values()
        ),
        "allMinuteDelaysAboveBreakeven": bool(holdout_delays) and all(
            row["winRatePct"] is not None and float(row["winRatePct"]) > BREAKEVEN_WR
            for row in holdout_delays.values()
        ),
        "everyActiveStatePositive": bool(holdout_summary["byStateDelay0"]) and all(
            int(row["trades"]) < 10 or float(row["pnlU"]) > 0.0
            for row in holdout_summary["byStateDelay0"].values()
        ),
        "maxDrawdownNoMoreThan25U": bool(holdout_delays) and max(
            float(row["maxDrawdownU"]) for row in holdout_delays.values()
        ) <= 25.0,
    }
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "status": "V15_MINUTE_VOLATILITY_NORMAL_RESEARCH",
        "safety": {
            "researchOnly": True,
            "shadowOnly": True,
            "tradeEnabled": False,
            "realTradingAllowed": False,
            "deploymentPerformed": False,
        },
        "data": {
            "input": str(Path(input_path).resolve()),
            "market": "Binance USD-M Futures",
            "rows": int(len(minutes)),
            "start": minutes.index[0],
            "end": minutes.index[-1],
            "holdoutStart": HOLDOUT_START,
            "holdoutWasExcludedFromSelection": True,
        },
        "volatilityRegimes": state_audit(volatility),
        "candidateDesign": {
            "profiles": [asdict(profile) for profile in PROFILES],
            "profileCount": len(PROFILES),
            "signalCadence": "completed 10-minute boundaries",
            "entry": "next one-minute candle open",
            "settlement": "entry open plus ten minutes, using minute open",
            "delaysMin": list(DELAYS_MIN),
            "normalCenterAndSigma": "prior closes only (shifted one minute)",
            "selectionGate": ">=40 train trades, positive PnL, WR>55.56%, positive-day rate>=45%; rank by Wilson lower then stability",
        },
        "candidateRows": int(len(candidates)),
        "developmentWalkForward": folds,
        "finalSelectionBeforeHoldout": final_selections,
        "finalMapping": final_mapping,
        "sealedHoldout": holdout_summary,
        "fixedProfileDevelopment": fixed_profile_development,
        "fixedProfileHoldoutDiagnostic": fixed_profile_holdout,
        "screen": {
            "passed": all(screen_gates.values()),
            "gates": screen_gates,
            "decision": (
                "eligible only for second-level shadow replay; real trading remains prohibited"
                if all(screen_gates.values()) else
                "reject minute volatility router; do not deploy"
            ),
        },
        "warnings": [
            "Minute bars cannot validate 0/5/10/15-second execution; any passing mapping still requires second-level futures replay.",
            "Fixed-profile holdout tables are diagnostics after the sealed mapping decision and cannot be used to remap this same holdout.",
        ],
        "outputs": {
            "json": str(OUT_JSON),
            "trades": str(OUT_TRADES),
            "candidates": str(OUT_CANDIDATES),
        },
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    candidates.to_csv(OUT_CANDIDATES, index=False, encoding="utf-8-sig")
    pd.concat(fold_trades, ignore_index=True).to_csv(
        OUT_TRADES, index=False, encoding="utf-8-sig"
    ) if fold_trades else pd.DataFrame().to_csv(OUT_TRADES, index=False)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    args = parser.parse_args()
    report = run(args.input)
    print(json.dumps(clean({
        "volatilityRegimes": report["volatilityRegimes"],
        "developmentWalkForward": report["developmentWalkForward"],
        "finalMapping": report["finalMapping"],
        "sealedHoldout": report["sealedHoldout"],
        "screen": report["screen"],
        "outputs": report["outputs"],
    }), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
