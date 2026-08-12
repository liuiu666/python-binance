from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .features import feature_columns, future_feature_columns
from .metrics import evaluate_predictions
from .models import LatentWorldModel, LogisticDirectionModel, momentum_probability
from .planner import plan_actions
from .state import LatentStateEncoder


@dataclass(frozen=True)
class Fold:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp


def monthly_folds(start: pd.Timestamp, end_exclusive: pd.Timestamp, warmup_end: pd.Timestamp) -> list[Fold]:
    tests = pd.date_range(max(start, warmup_end), end_exclusive, freq="MS", inclusive="left", tz="UTC")
    folds: list[Fold] = []
    for test_start in tests:
        test_end = min(test_start + pd.offsets.MonthBegin(1), end_exclusive)
        if test_start < test_end:
            folds.append(Fold(start, test_start, test_start, test_end))
    return folds


def _time_slice(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    return frame[(frame["time"] >= start) & (frame["time"] < end)].copy()


def _attach_plan(base: pd.DataFrame, p_up: np.ndarray, cfg: dict[str, Any], min_ev: float,
                 transition_confidence: np.ndarray | None = None,
                 state_support: np.ndarray | None = None) -> pd.DataFrame:
    plan = plan_actions(
        p_up, payout_rate=float(cfg["payout_rate"]), min_ev=min_ev,
        transition_confidence=transition_confidence, state_support=state_support,
        uncertainty_penalty=float(cfg["planner_uncertainty_penalty"]),
        sparse_state_penalty=float(cfg["planner_sparse_state_penalty"]),
    )
    out = base[["time", "settle_time", "entry", "settle", "up", "tie", "raw_move"]].reset_index(drop=True)
    return pd.concat([out, plan.reset_index(drop=True)], axis=1)


def _fit_predict_fold(train: pd.DataFrame, test: pd.DataFrame, cfg: dict[str, Any]) -> dict[str, Any]:
    columns = feature_columns(train)
    future_columns = future_feature_columns(train)
    if not columns or len(columns) != len(future_columns):
        raise ValueError("current and future feature columns do not match")
    x_train = train[columns].to_numpy(np.float32)
    x_test = test[columns].to_numpy(np.float32)
    future_train = train[future_columns].to_numpy(np.float32)
    y_train = train["up"].to_numpy(np.int8)

    encoder = LatentStateEncoder(
        n_states=int(cfg["state_count"]), random_state=int(cfg["random_seed"]),
        max_fit_rows=int(cfg["state_fit_max_rows"]),
    ).fit(x_train)
    current_train = encoder.transform(x_train).state
    current_test = encoder.transform(x_test).state
    future_state_train = encoder.transform(future_train).state

    logistic = LogisticDirectionModel(int(cfg["random_seed"])).fit(x_train, y_train)
    logistic_probability = logistic.predict_up(x_test)
    world = LatentWorldModel(len(columns), int(cfg["random_seed"])).fit(
        x_train, current_train, y_train, future_state_train,
    )
    world_prediction = world.predict(x_test, current_test)
    support = encoder.state_support(current_test)
    ret_index = columns.index("x_ret_10")
    momentum = momentum_probability(x_test[:, ret_index])
    return {
        "logistic": logistic_probability,
        "world_model": world_prediction.p_up,
        "momentum": momentum,
        "transition_confidence": world_prediction.transition_confidence,
        "state_support": support,
        "current_state": current_test,
        "predicted_future_state": world_prediction.future_state,
    }


def walk_forward_predictions(samples: pd.DataFrame, cfg: dict[str, Any]) -> dict[str, pd.DataFrame]:
    start = pd.Timestamp(cfg["development_start"])
    end = pd.Timestamp(cfg["development_end_exclusive"])
    folds = monthly_folds(start, end, start + pd.DateOffset(years=2))
    outputs: dict[str, list[pd.DataFrame]] = {"logistic": [], "world_model": [], "momentum": []}
    for fold in folds:
        train = _time_slice(samples, fold.train_start, fold.train_end)
        test = _time_slice(samples, fold.test_start, fold.test_end)
        # A training label is usable only after its settlement is observable.
        train = train[(train["settle_time"] <= fold.test_start) & ~train["tie"].astype(bool)]
        if len(train) < int(cfg["min_train_rows"]) or test.empty:
            continue
        predicted = _fit_predict_fold(train, test, cfg)
        for model_name in outputs:
            extra_conf = predicted["transition_confidence"] if model_name == "world_model" else None
            extra_support = predicted["state_support"] if model_name == "world_model" else None
            out = test[["time", "settle_time", "entry", "settle", "up", "tie", "raw_move"]].copy()
            out["p_up"] = predicted[model_name]
            if model_name == "world_model":
                out["transition_confidence"] = predicted["transition_confidence"]
                out["state_support"] = predicted["state_support"]
                out["current_state"] = predicted["current_state"]
                out["predicted_future_state"] = predicted["predicted_future_state"]
            outputs[model_name].append(out)
    return {name: pd.concat(parts, ignore_index=True).sort_values("time") for name, parts in outputs.items() if parts}


def evaluate_thresholds(predictions: dict[str, pd.DataFrame], cfg: dict[str, Any]) -> dict[str, Any]:
    candidates: dict[str, dict[str, Any]] = {}
    for min_ev in cfg["planner_min_ev_grid"]:
        key = f"{float(min_ev):.4f}"
        candidates[key] = {}
        for model_name, frame in predictions.items():
            planned = _attach_plan(
                frame, frame["p_up"].to_numpy(), cfg, float(min_ev),
                frame.get("transition_confidence", None), frame.get("state_support", None),
            )
            candidates[key][model_name] = evaluate_predictions(planned, float(cfg["payout_rate"]), float(cfg["stake"]))
    return candidates


def choose_frozen_spec(candidates: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    ranked: list[tuple[tuple[float, ...], str, dict[str, Any]]] = []
    minimum = int(cfg["success"]["min_trades"])
    for key, models in candidates.items():
        world = models["world_model"]
        logistic = models["logistic"]
        eligible = world["trades"] >= minimum and world["pnl"] > 0
        beats = world["pnl"] > logistic["pnl"] and (world["winRate"] or 0) > (logistic["winRate"] or 0)
        score = (float(eligible), float(beats), float(world["winRate"] or 0), float(world["pnl"]), float(world["trades"]), -float(key))
        ranked.append((score, key, models))
    _, selected, models = max(ranked, key=lambda item: item[0])
    return {
        "version": 1,
        "selectedMinEv": float(selected),
        "selectionRule": "eligible(trades,pnl), beats logistic, winRate, pnl, trades, lower minEv",
        "developmentMetrics": models,
        "developmentEndExclusive": cfg["development_end_exclusive"],
        "frozenStart": cfg["frozen_start"],
        "stateCount": int(cfg["state_count"]),
        "randomSeed": int(cfg["random_seed"]),
    }


def fit_development_predict_frozen(samples: pd.DataFrame, cfg: dict[str, Any]) -> dict[str, pd.DataFrame]:
    boundary = pd.Timestamp(cfg["development_end_exclusive"])
    train = samples[(samples["time"] >= pd.Timestamp(cfg["development_start"])) & (samples["time"] < boundary)]
    train = train[(train["settle_time"] <= boundary) & ~train["tie"].astype(bool)]
    test = samples[samples["time"] >= pd.Timestamp(cfg["frozen_start"])]
    if test.empty:
        raise ValueError("frozen holdout is empty")
    predicted = _fit_predict_fold(train, test, cfg)
    outputs: dict[str, pd.DataFrame] = {}
    for model_name in ("logistic", "world_model", "momentum"):
        out = test[["time", "settle_time", "entry", "settle", "up", "tie", "raw_move"]].copy()
        out["p_up"] = predicted[model_name]
        if model_name == "world_model":
            out["transition_confidence"] = predicted["transition_confidence"]
            out["state_support"] = predicted["state_support"]
            out["current_state"] = predicted["current_state"]
            out["predicted_future_state"] = predicted["predicted_future_state"]
        outputs[model_name] = out
    return outputs


def config_hash(config: dict[str, Any]) -> str:
    raw = json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def guard_status(path: Path, force: bool) -> tuple[bool, bool]:
    """Return (allowed, pristine). A forced rerun is explicitly non-pristine."""
    if path.exists() and not force:
        return False, False
    return True, not path.exists()
