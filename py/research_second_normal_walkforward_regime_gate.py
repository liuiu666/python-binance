from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_second_normal_drawdown_router as base
import research_second_normal_trend_gate as trend_gate


OUT_JSON = ROOT / "tmp" / "second_normal_walkforward_regime_gate.json"

VALID_DAYS = sorted(trend_gate.VALID_DAYS)

NUMERIC_FEATURES = [
    "p_up",
    "z_score",
    "routeSigma",
    "r10",
    "observed600Pct",
    "observedLookbackPct",
    "zone_position",
    "trend_30s_bps",
    "trend_60s_bps",
    "trend_120s_bps",
    "trend_300s_bps",
    "trend_600s_bps",
    "trend_1800s_bps",
    "trend_3600s_bps",
    "adverse_30s_bps",
    "adverse_60s_bps",
    "adverse_120s_bps",
    "range_10m_bps",
    "range_30m_bps",
    "flow_5m",
    "extreme_age_120s",
    "extreme_age_600s",
    "extreme_age_1800s",
    "reversal_from_extreme_120s_bps",
    "reversal_from_extreme_600s_bps",
    "reversal_from_extreme_1800s_bps",
    "breakout_beyond_120s_bps",
    "breakout_beyond_600s_bps",
    "breakout_beyond_1800s_bps",
]

CATEGORICAL_FEATURES = ["role", "signal", "zone_label"]


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, tuple):
        return [clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        value = float(value)
        return value if math.isfinite(value) else None
    return value


def feature_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for col in NUMERIC_FEATURES:
        if col not in df:
            df[col] = np.nan
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in CATEGORICAL_FEATURES:
        if col not in df:
            df[col] = ""
        df[col] = df[col].astype(str)
    x = df[NUMERIC_FEATURES].copy()
    dummies = pd.get_dummies(df[CATEGORICAL_FEATURES], prefix=CATEGORICAL_FEATURES, dtype=float)
    return pd.concat([x, dummies], axis=1)


def align_features(train_x: pd.DataFrame, test_x: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    columns = sorted(set(train_x.columns) | set(test_x.columns))
    return train_x.reindex(columns=columns, fill_value=0.0), test_x.reindex(columns=columns, fill_value=0.0)


def base_candidate_allowed(row: dict[str, Any], *, obs: float = 88.0) -> bool:
    if str(row.get("day")) not in trend_gate.VALID_DAYS:
        return False
    if float(row.get("observed600Pct", 0.0)) < obs:
        return False
    if float(row.get("observedLookbackPct", 0.0)) < obs:
        return False
    if float(row.get("r10", 0.0)) > 42.0:
        return False
    if row.get("role") == "mid" and float(row.get("routeSigma", 999.0)) >= 20.0:
        return False
    if row.get("signal") == "DOWN" and float(row.get("r10", 0.0)) > 35.0:
        return False
    return True


def base_select_by_day(df: pd.DataFrame) -> list[dict[str, Any]]:
    # The base selection is used only to create walk-forward training labels.
    candidates = df.to_dict("records")
    return base.select_router(
        candidates,
        r10_cap=42.0,
        mid_route_sigma_cap=20.0,
        down_r10_cap=35.0,
        allowed_days=set(VALID_DAYS),
        min_observed_600_pct=88.0,
        min_observed_lookback_pct=88.0,
        global_loss_cool_count=0,
        global_loss_cool_sec=0,
    )


def make_model(name: str):
    if name == "logistic":
        return make_pipeline(
            SimpleImputer(strategy="median"),
            StandardScaler(),
            LogisticRegression(max_iter=1000, class_weight="balanced", C=0.6),
        )
    if name == "rf_depth3":
        return make_pipeline(
            SimpleImputer(strategy="median"),
            RandomForestClassifier(
                n_estimators=180,
                max_depth=3,
                min_samples_leaf=8,
                random_state=17,
                class_weight="balanced_subsample",
            ),
        )
    raise ValueError(name)


def predict_prob(model, train_rows: list[dict[str, Any]], test_rows: list[dict[str, Any]]) -> np.ndarray:
    train_x = feature_frame(train_rows)
    test_x = feature_frame(test_rows)
    train_x, test_x = align_features(train_x, test_x)
    y = np.array([1 if row["won"] else 0 for row in train_rows], dtype=int)
    model.fit(train_x, y)
    return model.predict_proba(test_x)[:, 1]


def walkforward_select(df: pd.DataFrame, *, model_name: str, min_prob: float, min_train: int = 60) -> list[dict[str, Any]]:
    base_train_rows = base_select_by_day(df)
    train_by_day = {day: [row for row in base_train_rows if str(row["day"]) < day] for day in VALID_DAYS}
    all_rows_by_idx: dict[int, list[dict[str, Any]]] = {}
    for row in df[df["day"].astype(str).isin(VALID_DAYS)].to_dict("records"):
        all_rows_by_idx.setdefault(int(row["idx"]), []).append(row)

    accepted: list[dict[str, Any]] = []
    last_idx = -10**12
    loss_count = 0
    cool_until = -10**12
    prob_cache: dict[str, dict[int, float]] = {}

    for day in VALID_DAYS:
        train_rows = train_by_day[day]
        day_indices = sorted(
            idx for idx, rows in all_rows_by_idx.items() if str(rows[0].get("day")) == day
        )
        day_candidates = [
            row
            for idx in day_indices
            for row in all_rows_by_idx[idx]
            if base_candidate_allowed(row, obs=88.0)
        ]
        if len(train_rows) >= min_train and len(set(row["won"] for row in train_rows)) > 1 and day_candidates:
            probs = predict_prob(make_model(model_name), train_rows, day_candidates)
            prob_cache[day] = {f"{int(row['idx'])}:{row['role']}": float(p) for row, p in zip(day_candidates, probs)}
        else:
            prob_cache[day] = {}

        for idx in day_indices:
            if idx - last_idx < 600 or idx < cool_until:
                continue
            rows = all_rows_by_idx[idx]
            route_sigma = float(rows[0]["routeSigma"])
            selected = None
            for role in base.role_order(route_sigma, low_hi=9.0, mid_hi=22.0, high_lo=16.0):
                role_rows = [row for row in rows if row.get("role") == role]
                if not role_rows:
                    continue
                candidate = max(role_rows, key=lambda row: abs(float(row.get("p_up", 0.5)) - 0.5))
                if not base_candidate_allowed(candidate, obs=88.0):
                    continue
                prob = prob_cache[day].get(f"{int(candidate['idx'])}:{candidate['role']}", 1.0)
                candidate["regime_win_prob"] = round(prob, 6)
                if prob < min_prob:
                    continue
                selected = candidate
                break
            if selected is None:
                continue
            accepted.append(selected)
            last_idx = idx
            if selected["won"]:
                loss_count = 0
            else:
                loss_count += 1
                if loss_count >= 2:
                    cool_until = idx + 3600
                    loss_count = 0
    return accepted


def run() -> dict[str, Any]:
    df = trend_gate.build_features()
    results = []
    for model_name in ("logistic", "rf_depth3"):
        for min_prob in (0.48, 0.50, 0.52, 0.55, 0.58, 0.60):
            rows = walkforward_select(df, model_name=model_name, min_prob=min_prob)
            total = base.summarize(rows)
            train = base.summarize([row for row in rows if str(row["day"]) < base.RECENT_CUTOFF])
            recent = base.summarize([row for row in rows if str(row["day"]) >= base.RECENT_CUTOFF])
            score = (
                total["pnl"]
                - total["maxDrawdownU"] * 7.0
                + recent["pnl"]
                + total["trades"] * 0.1
                - total["losingDays"] * 5.0
            )
            results.append(
                {
                    "score": round(float(score), 4),
                    "model": model_name,
                    "minProb": min_prob,
                    "total": total,
                    "train": train,
                    "recent": recent,
                }
            )
    results.sort(key=lambda item: item["score"], reverse=True)
    output = {"results": results}
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    result = run()
    for item in result["results"]:
        print(json.dumps(clean(item), ensure_ascii=False))
