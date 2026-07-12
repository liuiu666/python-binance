"""Separate trend formation from mature/exhausted trend using past-only features."""

from __future__ import annotations

import itertools
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_market_regime_classifier as regime  # noqa: E402


OUT_JSON = ROOT / "tmp" / "trend_phase_classifier_research.json"
OUT_TRADES = ROOT / "tmp" / "trend_phase_classifier_top_trades.csv"
HORIZON_SEC = 600


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
    return value


def select_gap(rows: pd.DataFrame) -> pd.DataFrame:
    selected = []
    for _, dataset in rows.groupby("dataset", sort=True):
        last_time = None
        for timestamp, row in dataset.sort_index().iterrows():
            if last_time is not None and (timestamp - last_time).total_seconds() < HORIZON_SEC:
                continue
            selected.append(row)
            last_time = timestamp
    return pd.DataFrame(selected)


def metrics(rows: pd.DataFrame) -> dict:
    if rows.empty:
        return {"n": 0, "wr": 0.0, "pnl": 0, "dd": 0, "medianBp": 0.0}
    values = pd.to_numeric(rows["signed_future_bps"], errors="coerce").dropna()
    equity = peak = drawdown = wins = 0
    for value in values:
        won = value > 0.0
        wins += int(won)
        equity += 4 if won else -5
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "n": int(len(values)),
        "wr": round(wins / len(values) * 100.0, 2),
        "pnl": int(equity),
        "dd": int(drawdown),
        "medianBp": round(float(values.median()), 3),
    }


def report(rows: pd.DataFrame) -> dict:
    return {
        "overall": metrics(rows),
        "train": metrics(rows[rows["split"] == "train"]),
        "test": metrics(rows[rows["split"] == "test"]),
        "byDataset": {name: metrics(group) for name, group in rows.groupby("dataset", sort=True)},
    }


def load_samples() -> pd.DataFrame:
    raw = pd.read_csv(regime.OUT_SAMPLES, parse_dates=["time"])
    raw = raw.set_index("time").sort_index()
    return raw


def scan() -> tuple[list[dict], dict[str, pd.DataFrame]]:
    samples = load_samples()
    candidates: list[dict] = []
    trade_sets: dict[str, pd.DataFrame] = {}
    for base_name in ("balanced", "strict"):
        spec = next(item for item in regime.SPECS if item.name == base_name)
        frame = regime.classify(samples, spec)
        trend = frame["regime"].eq("trend")
        direction = frame["trend_direction"]
        frame["signed_future_bps"] = direction * frame["future_ret_bps"]
        aligned_ret60 = direction * frame["ret_60s_bps"]
        aligned_slope30 = direction * frame["slope_30_bps"]
        aligned_flow = direction * frame["flow_60"]
        aligned_imb = direction * frame["imbalance_60_mean"]
        aligned_bandwalk = direction * frame["bandwalk_signed"]

        for family in ("formation", "pullback_resume"):
            if family == "formation":
                grid = itertools.product(
                    (1.0, 2.0, 4.0),
                    (0.0, 0.08, 0.16),
                    (0.0, 0.08, 0.16),
                    (0.6, 0.8, 1.0),
                    (1.6, 2.0),
                )
                for ret_min, flow_min, imb_min, bandwalk_max, sigma_max in grid:
                    mask = (
                        trend
                        & (aligned_ret60 >= ret_min)
                        & (aligned_flow >= flow_min)
                        & (aligned_imb >= imb_min)
                        & (aligned_bandwalk <= bandwalk_max)
                        & (frame["sigma_expand"] <= sigma_max)
                    )
                    rows = select_gap(frame[mask])
                    result = report(rows)
                    name = f"{base_name}_formation_R{ret_min}_F{flow_min}_I{imb_min}_B{bandwalk_max}_S{sigma_max}"
                    candidates.append({"name": name, "base": base_name, "family": family, "params": {"ret60Min": ret_min, "flowMin": flow_min, "imbMin": imb_min, "bandwalkMax": bandwalk_max, "sigmaMax": sigma_max}, **result})
                    trade_sets[name] = rows
            else:
                grid = itertools.product(
                    (-12.0, -8.0, -5.0),
                    (0.0, 1.0, 2.0),
                    (0.0, 0.08, 0.16),
                    (0.0, 0.08, 0.16),
                    (0.6, 0.8, 1.0),
                )
                for pullback_min, slope_min, flow_min, imb_min, bandwalk_max in grid:
                    mask = (
                        trend
                        & (aligned_ret60 <= 0.0)
                        & (aligned_ret60 >= pullback_min)
                        & (aligned_slope30 >= slope_min)
                        & (aligned_flow >= flow_min)
                        & (aligned_imb >= imb_min)
                        & (aligned_bandwalk <= bandwalk_max)
                        & (frame["sigma_expand"] <= 1.8)
                    )
                    rows = select_gap(frame[mask])
                    result = report(rows)
                    name = f"{base_name}_pullback_P{pullback_min}_SL{slope_min}_F{flow_min}_I{imb_min}_B{bandwalk_max}"
                    candidates.append({"name": name, "base": base_name, "family": family, "params": {"pullbackMin": pullback_min, "slopeMin": slope_min, "flowMin": flow_min, "imbMin": imb_min, "bandwalkMax": bandwalk_max}, **result})
                    trade_sets[name] = rows
    return candidates, trade_sets


def robustness_score(row: dict) -> float:
    train = row["train"]
    test = row["test"]
    if train["n"] < 10 or test["n"] < 8:
        return -99999.0
    if train["wr"] < 55.56 or test["wr"] < 55.56 or train["pnl"] <= 0 or test["pnl"] <= 0:
        return -99999.0
    day_values = list(row["byDataset"].values())
    losing_days = sum(item["pnl"] < 0 for item in day_values)
    return (
        min(train["wr"], test["wr"]) * 2.0
        + row["overall"]["pnl"]
        - row["overall"]["dd"] * 0.8
        - losing_days * 12.0
    )


def run() -> dict:
    candidates, trade_sets = scan()
    for row in candidates:
        row["robustnessScore"] = round(robustness_score(row), 3)
    ranked = sorted(candidates, key=lambda row: row["robustnessScore"], reverse=True)
    survivors = [row for row in ranked if row["robustnessScore"] > -99999.0]
    top = survivors[:20] if survivors else ranked[:20]
    top_names = {row["name"] for row in top[:5]}
    top_trades = []
    for name in top_names:
        rows = trade_sets[name].copy()
        rows["research_case"] = name
        top_trades.append(rows)
    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "tested": len(candidates),
        "robustSurvivors": len(survivors),
        "top": top,
        "conclusion": "A survivor must be profitable and above payout breakeven in both train and test splits.",
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    if top_trades:
        pd.concat(top_trades).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    return output


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
