"""Classify BTC market state using only past data and validate on the next 10 minutes."""

from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_v2_persistent_reclaim as source  # noqa: E402
from liquidity_v2_core import build_features  # noqa: E402


OUT_JSON = ROOT / "tmp" / "market_regime_classifier_research.json"
OUT_SAMPLES = ROOT / "tmp" / "market_regime_classifier_samples.csv"
SAMPLE_SEC = 30
HORIZON_SEC = 600


@dataclass(frozen=True)
class RegimeSpec:
    name: str
    trend_ret_bps: float
    trend_efficiency: float
    trend_slope_bps: float
    trend_position: float
    trend_bandwalk: float
    trend_votes: int
    normal_inside: float
    normal_efficiency_max: float
    normal_slope_max_bps: float
    normal_ret_max_bps: float
    normal_bandwalk_max: float
    normal_votes: int


SPECS = [
    RegimeSpec("loose", 12.0, 0.08, 4.0, 0.70, 0.30, 4, 0.58, 0.10, 7.0, 22.0, 0.50, 4),
    RegimeSpec("balanced", 15.0, 0.10, 5.0, 0.75, 0.40, 4, 0.62, 0.08, 6.0, 18.0, 0.40, 4),
    RegimeSpec("strict", 20.0, 0.12, 6.0, 0.80, 0.50, 5, 0.66, 0.06, 5.0, 15.0, 0.30, 5),
]


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def build_regime_features(data: pd.DataFrame) -> pd.DataFrame:
    rules = source.load_rules()
    base = build_features(data, rules)
    close = data["close"].astype(float)
    one_sec_path_bps = np.log(close / close.shift(1)).abs() * 10000.0
    path_600 = one_sec_path_bps.rolling(600, min_periods=300).sum()
    ret_60 = np.log(close / close.shift(60)) * 10000.0
    ret_600 = np.log(close / close.shift(600)) * 10000.0
    ret_1800 = np.log(close / close.shift(1800)) * 10000.0
    efficiency_600 = ret_600.abs() / path_600.replace(0.0, np.nan)
    upper_walk = base["z"].gt(1.0).astype(float).rolling(120, min_periods=60).mean()
    lower_walk = base["z"].lt(-1.0).astype(float).rolling(120, min_periods=60).mean()
    bandwalk_signed = upper_walk - lower_walk

    out = base.copy()
    out["ret_60s_bps"] = ret_60
    out["ret_600s_bps"] = ret_600
    out["ret_1800s_bps"] = ret_1800
    out["efficiency_600"] = efficiency_600
    out["bandwalk_signed"] = bandwalk_signed
    out["flow_120_mean"] = base["flow_60"].rolling(120, min_periods=30).mean()
    out["imbalance_60_mean"] = base["imbalance_20"].rolling(60, min_periods=20).mean()
    out["future_ret_bps"] = np.log(close.shift(-HORIZON_SEC) / close) * 10000.0
    out["future_abs_bps"] = out["future_ret_bps"].abs()
    return out


def classify(frame: pd.DataFrame, spec: RegimeSpec) -> pd.DataFrame:
    out = frame.copy()
    direction = np.sign(out["ret_600s_bps"]).replace(0.0, np.nan)
    position_ok = np.where(direction > 0, out["pos_600s"] >= spec.trend_position, out["pos_600s"] <= 1.0 - spec.trend_position)
    trend_checks = pd.DataFrame(
        {
            "ret": out["ret_600s_bps"].abs() >= spec.trend_ret_bps,
            "efficiency": out["efficiency_600"] >= spec.trend_efficiency,
            "slope": direction * out["center_slope_bps"] >= spec.trend_slope_bps,
            "position": position_ok,
            "bandwalk": direction * out["bandwalk_signed"] >= spec.trend_bandwalk,
            "long_alignment": direction * out["ret_1800s_bps"] >= spec.trend_ret_bps,
            "flow": direction * out["flow_120_mean"] >= 0.04,
            "orderbook": direction * out["imbalance_60_mean"] >= 0.08,
        },
        index=out.index,
    )
    normal_checks = pd.DataFrame(
        {
            "inside": out["inside1_ratio"] >= spec.normal_inside,
            "efficiency": out["efficiency_600"] <= spec.normal_efficiency_max,
            "slope": out["center_slope_bps"].abs() <= spec.normal_slope_max_bps,
            "return": out["ret_600s_bps"].abs() <= spec.normal_ret_max_bps,
            "bandwalk": out["bandwalk_signed"].abs() <= spec.normal_bandwalk_max,
            "sigma": out["sigma_expand"] <= 1.6,
            "coverage": out["observed_pct"] >= 90.0,
        },
        index=out.index,
    )
    out["trend_votes"] = trend_checks.sum(axis=1)
    out["normal_votes"] = normal_checks.sum(axis=1)
    out["trend_direction"] = direction
    out["regime"] = "transition"
    out.loc[out["normal_votes"] >= spec.normal_votes, "regime"] = "normal"
    out.loc[out["trend_votes"] >= spec.trend_votes, "regime"] = "trend"
    return out


def outcome_metrics(values: pd.Series) -> dict:
    values = pd.to_numeric(values, errors="coerce").dropna()
    count = len(values)
    return {
        "n": count,
        "winRate": round(float((values > 0.0).mean() * 100.0), 2) if count else 0.0,
        "avgSignedBps": round(float(values.mean()), 3) if count else 0.0,
        "medianSignedBps": round(float(values.median()), 3) if count else 0.0,
        "strongGe5Pct": round(float((values >= 5.0).mean() * 100.0), 2) if count else 0.0,
    }


def trade_metrics(rows: pd.DataFrame, signed_col: str) -> dict:
    if rows.empty:
        return {"trades": 0, "winRate": 0.0, "pnlU": 0, "maxDrawdownU": 0, "medianSignedBps": 0.0}
    equity = peak = max_dd = wins = 0
    signed = pd.to_numeric(rows[signed_col], errors="coerce").fillna(0.0)
    for value in signed:
        won = value > 0.0
        wins += int(won)
        equity += 4 if won else -5
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "trades": int(len(rows)),
        "winRate": round(wins / len(rows) * 100.0, 2),
        "pnlU": int(equity),
        "maxDrawdownU": int(max_dd),
        "medianSignedBps": round(float(signed.median()), 3),
    }


def cooldown_entries(frame: pd.DataFrame, mask: pd.Series, signed: pd.Series) -> pd.DataFrame:
    selected = []
    last_time: pd.Timestamp | None = None
    for timestamp in frame.index[mask.fillna(False)]:
        if last_time is not None and (timestamp - last_time).total_seconds() < HORIZON_SEC:
            continue
        row = frame.loc[timestamp].copy()
        row["signed_signal_bps"] = float(signed.loc[timestamp])
        row["time"] = timestamp
        selected.append(row)
        last_time = timestamp
    return pd.DataFrame(selected)


def episode_stats(regimes: pd.Series) -> dict:
    groups = regimes.ne(regimes.shift()).cumsum()
    episodes = pd.DataFrame({"regime": regimes, "group": groups}).groupby("group").agg(regime=("regime", "first"), samples=("regime", "size"))
    output = {}
    for regime, rows in episodes.groupby("regime"):
        minutes = rows["samples"] * SAMPLE_SEC / 60.0
        output[str(regime)] = {
            "episodes": int(len(rows)),
            "medianMinutes": round(float(minutes.median()), 2),
            "p75Minutes": round(float(minutes.quantile(0.75)), 2),
            "maxMinutes": round(float(minutes.max()), 2),
        }
    return output


def evaluate(frame: pd.DataFrame, spec: RegimeSpec) -> dict:
    classified = classify(frame, spec)
    trend = classified["regime"].eq("trend")
    normal = classified["regime"].eq("normal")
    transition = classified["regime"].eq("transition")
    trend_signed = classified["trend_direction"] * classified["future_ret_bps"]
    revert_direction = -np.sign(classified["z"])
    normal_signed = revert_direction * classified["future_ret_bps"]

    trend_entry_mask = trend
    pullback_resume = (
        trend
        & (classified["trend_direction"] * classified["ret_60s_bps"] <= 0.0)
        & (classified["trend_direction"] * classified["ret_60s_bps"] >= -10.0)
        & (classified["trend_direction"] * classified["slope_30_bps"] > 0.0)
        & (classified["trend_direction"] * classified["flow_60"] > 0.0)
    )
    normal_edge = normal & classified["z"].abs().ge(0.8)
    trend_entries = cooldown_entries(classified, trend_entry_mask, trend_signed)
    pullback_entries = cooldown_entries(classified, pullback_resume, trend_signed)
    normal_entries = cooldown_entries(classified, normal_edge, normal_signed)

    state_counts = classified["regime"].value_counts()
    return {
        "spec": asdict(spec),
        "stateSharePct": {
            state: round(float(state_counts.get(state, 0) / max(len(classified), 1) * 100.0), 2)
            for state in ("normal", "trend", "transition")
        },
        "trendContinuationAllSamples": outcome_metrics(trend_signed[trend]),
        "normalEdgeReversionAllSamples": outcome_metrics(normal_signed[normal_edge]),
        "trendEntries10mGap": trade_metrics(trend_entries, "signed_signal_bps"),
        "trendPullbackResume10mGap": trade_metrics(pullback_entries, "signed_signal_bps"),
        "normalEdgeEntries10mGap": trade_metrics(normal_entries, "signed_signal_bps"),
        "episodes": episode_stats(classified["regime"]),
    }


def load_samples() -> tuple[pd.DataFrame, dict]:
    rules = source.load_rules()
    frames = []
    coverage = {}
    cache = {}
    for name, item in source.DATASETS.items():
        key = (str(item["seconds"]), str(item["orderbook"]))
        if key not in cache:
            cache[key] = source.load_market(Path(item["seconds"]), Path(item["orderbook"]))
        start = pd.Timestamp(item["start"])
        end = pd.Timestamp(item["end"])
        full = cache[key]
        data = full[
            (full.index >= start - pd.Timedelta(seconds=3700))
            & (full.index < end + pd.Timedelta(seconds=HORIZON_SEC + 5))
        ].copy()
        features = build_regime_features(data)
        mask = (features.index >= start) & (features.index < end) & features["future_ret_bps"].notna()
        sampled = features.loc[mask].iloc[::SAMPLE_SEC].copy()
        sampled["dataset"] = name
        sampled["split"] = "train" if name in {"2026-07-05_06", "2026-07-08"} else "test"
        frames.append(sampled)
        coverage[name] = {
            "samples": int(len(sampled)),
            "start": sampled.index.min().isoformat(),
            "end": sampled.index.max().isoformat(),
            "secondCoveragePct": round(float(data.loc[mask, "observed"].mean() * 100.0), 3),
            "orderbookCoveragePct": round(float(data.loc[mask, "ob_available"].mean() * 100.0), 3),
        }
    return pd.concat(frames).sort_index(), coverage


def run() -> dict:
    samples, coverage = load_samples()
    reports = []
    for spec in SPECS:
        reports.append(
            {
                "name": spec.name,
                "overall": evaluate(samples, spec),
                "train": evaluate(samples[samples["split"] == "train"], spec),
                "test": evaluate(samples[samples["split"] == "test"], spec),
                "byDataset": {
                    name: evaluate(rows, spec)
                    for name, rows in samples.groupby("dataset", sort=True)
                },
            }
        )
    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "sampleEverySec": SAMPLE_SEC,
        "validationHorizonSec": HORIZON_SEC,
        "coverage": coverage,
        "reports": reports,
        "note": "All regime inputs use past/current data only. Future 10-minute return is used only as a validation label.",
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    samples.reset_index(names="time").to_csv(OUT_SAMPLES, index=False, encoding="utf-8-sig")
    return output


if __name__ == "__main__":
    result = run()
    print(json.dumps(clean(result), ensure_ascii=False, indent=2))
