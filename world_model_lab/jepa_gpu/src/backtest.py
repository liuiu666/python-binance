from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from .metrics import evaluate_predictions, plan_actions
from .probe import fit_method


@dataclass(frozen=True)
class Fold:
    label_months: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp


def _utc_timestamp(value: str | pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def monthly_walk_forward_folds(
    start: str | pd.Timestamp,
    end_exclusive: str | pd.Timestamp,
    label_months: int,
    label_history_start: str | pd.Timestamp | None = None,
) -> list[Fold]:
    """Return monthly tests with a full trailing calendar-month label window."""
    development_start = _utc_timestamp(start)
    history_start = development_start if label_history_start is None else _utc_timestamp(label_history_start)
    development_end = _utc_timestamp(end_exclusive)
    months = int(label_months)
    if months <= 0:
        raise ValueError("label_months must be positive")
    test_starts = pd.date_range(development_start, development_end, freq="MS", inclusive="left")
    folds: list[Fold] = []
    for test_start in test_starts:
        train_start = test_start - pd.DateOffset(months=months)
        if train_start < history_start or train_start >= test_start:
            continue
        test_end = min(test_start + pd.offsets.MonthBegin(1), development_end)
        folds.append(Fold(months, train_start, test_start, test_start, test_end))
    return folds


def select_fold_rows(samples: pd.DataFrame, fold: Fold) -> tuple[np.ndarray, np.ndarray]:
    """Select labels observable at fold start and outcomes inside development."""
    times = pd.to_datetime(samples["time"], utc=True)
    settlements = pd.to_datetime(samples["settle_time"], utc=True)
    ties = samples["tie"].astype(bool).to_numpy()
    train = (
        (times >= fold.train_start)
        & (times < fold.train_end)
        & (settlements <= fold.test_start)
        & ~ties
    )
    test = (
        (times >= fold.test_start)
        & (times < fold.test_end)
        & (settlements <= fold.test_end)
    )
    return np.flatnonzero(train.to_numpy()), np.flatnonzero(test.to_numpy())


def _prediction_frame(samples: pd.DataFrame, row_indices: np.ndarray, probabilities: np.ndarray) -> pd.DataFrame:
    columns = ["time", "settle_time", "entry", "settle", "raw_move", "up", "tie"]
    output = samples.iloc[row_indices][columns].reset_index(drop=True).copy()
    output["p_up"] = np.asarray(probabilities, dtype=np.float64)
    return output


def walk_forward_predictions(
    samples: pd.DataFrame,
    features: Mapping[str, np.ndarray],
    *,
    development_start: str | pd.Timestamp,
    development_end_exclusive: str | pd.Timestamp,
    label_months: int,
    label_history_start: str | pd.Timestamp | None = None,
    seed: int = 7,
    min_train_rows: int = 2,
) -> tuple[dict[str, pd.DataFrame], list[dict[str, Any]]]:
    """Fit each method independently in each monthly, leakage-safe fold."""
    if int(min_train_rows) <= 0:
        raise ValueError("min_train_rows must be positive")
    row_count = len(samples)
    for method, matrix in features.items():
        if len(matrix) != row_count:
            raise ValueError(f"feature rows for {method} do not align with samples")

    outputs: dict[str, list[pd.DataFrame]] = {method: [] for method in features}
    fold_reports: list[dict[str, Any]] = []
    for fold_index, fold in enumerate(
        monthly_walk_forward_folds(development_start, development_end_exclusive, int(label_months), label_history_start)
    ):
        train_rows, test_rows = select_fold_rows(samples, fold)
        report = {
            "labelMonths": int(label_months),
            "trainStart": fold.train_start.isoformat(),
            "trainEndExclusive": fold.train_end.isoformat(),
            "latestAllowedTrainSettleTime": fold.test_start.isoformat(),
            "testStart": fold.test_start.isoformat(),
            "testEndExclusive": fold.test_end.isoformat(),
            "trainRows": int(len(train_rows)),
            "testRows": int(len(test_rows)),
            "evaluated": bool(len(train_rows) >= int(min_train_rows) and len(test_rows)),
        }
        fold_reports.append(report)
        if not report["evaluated"]:
            continue
        labels = samples.iloc[train_rows]["up"].astype(np.int8).to_numpy()
        for method, matrix in features.items():
            model = fit_method(method, np.asarray(matrix)[train_rows], labels, seed=int(seed) + fold_index)
            probabilities = model.predict_up(np.asarray(matrix)[test_rows])
            outputs[method].append(_prediction_frame(samples, test_rows, probabilities))

    combined: dict[str, pd.DataFrame] = {}
    for method, parts in outputs.items():
        if parts:
            combined[method] = pd.concat(parts, ignore_index=True).sort_values("time").reset_index(drop=True)
    return combined, fold_reports


def evaluate_min_ev_grid(
    predictions: Mapping[str, pd.DataFrame],
    *,
    payout_rate: float,
    stake: float,
    min_ev_grid: Sequence[float],
) -> dict[str, dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for min_ev_value in min_ev_grid:
        min_ev = float(min_ev_value)
        threshold = f"{min_ev:.4f}"
        candidates[threshold] = {}
        for method, prediction in predictions.items():
            plan = plan_actions(prediction["p_up"].to_numpy(), payout_rate=float(payout_rate), min_ev=min_ev)
            planned = pd.concat([prediction.reset_index(drop=True), plan.drop(columns=["p_up"])], axis=1)
            candidates[threshold][method] = evaluate_predictions(
                planned,
                payout_rate=float(payout_rate),
                stake=float(stake),
            )
    return candidates


def select_and_confirm_min_ev(
    predictions: Mapping[str, pd.DataFrame],
    *,
    selection_start: str | pd.Timestamp,
    selection_end_exclusive: str | pd.Timestamp,
    confirmation_end_exclusive: str | pd.Timestamp,
    payout_rate: float,
    stake: float,
    min_ev_grid: Sequence[float],
    min_selection_trades: int,
) -> dict[str, Any]:
    """Select thresholds on one period and score them once on the next."""
    selection_start_time = _utc_timestamp(selection_start)
    selection_end = _utc_timestamp(selection_end_exclusive)
    confirmation_end = _utc_timestamp(confirmation_end_exclusive)
    if not selection_start_time < selection_end < confirmation_end:
        raise ValueError("threshold selection and confirmation boundaries must increase")
    if int(min_selection_trades) <= 0:
        raise ValueError("min_selection_trades must be positive")

    methods: dict[str, Any] = {}
    for method, prediction in predictions.items():
        times = pd.to_datetime(prediction["time"], utc=True)
        selection_rows = prediction[
            (times >= selection_start_time) & (times < selection_end)
        ].reset_index(drop=True)
        confirmation_rows = prediction[
            (times >= selection_end) & (times < confirmation_end)
        ].reset_index(drop=True)
        candidates: dict[str, Any] = {}
        eligible: list[tuple[float, float]] = []
        for min_ev_value in min_ev_grid:
            min_ev = float(min_ev_value)
            threshold = f"{min_ev:.4f}"
            metrics = evaluate_min_ev_grid(
                {method: selection_rows},
                payout_rate=float(payout_rate),
                stake=float(stake),
                min_ev_grid=[min_ev],
            )[threshold][method]
            candidates[threshold] = metrics
            if metrics["trades"] >= int(min_selection_trades):
                eligible.append((float(metrics["pnl"]), min_ev))

        if not eligible:
            methods[method] = {
                "selectedMinEv": None,
                "selectionCandidates": candidates,
                "selectionMetrics": None,
                "confirmationMetrics": None,
                "eligible": False,
            }
            continue
        # Maximum selection PnL; lower threshold wins an exact tie.
        _, selected = max(eligible, key=lambda item: (item[0], -item[1]))
        selected_key = f"{selected:.4f}"
        confirmation = evaluate_min_ev_grid(
            {method: confirmation_rows},
            payout_rate=float(payout_rate),
            stake=float(stake),
            min_ev_grid=[selected],
        )[selected_key][method]
        methods[method] = {
            "selectedMinEv": selected,
            "selectionCandidates": candidates,
            "selectionMetrics": candidates[selected_key],
            "confirmationMetrics": confirmation,
            "eligible": True,
        }
    return {
        "selectionStart": selection_start_time.isoformat(),
        "selectionEndExclusive": selection_end.isoformat(),
        "confirmationStart": selection_end.isoformat(),
        "confirmationEndExclusive": confirmation_end.isoformat(),
        "minimumSelectionTrades": int(min_selection_trades),
        "selectionRule": "maximize selection PnL among thresholds meeting minimum trades; exact ties choose lower min_ev",
        "methods": methods,
    }


def development_gate(
    curves: Mapping[str, Any],
    *,
    few_shot_months: Sequence[int] = (1, 3),
    minimum_confirmation_trades: int = 300,
) -> dict[str, Any]:
    """Apply the preregistered gate using confirmation-period metrics only."""
    candidates: list[dict[str, Any]] = []
    for months_value in few_shot_months:
        months = int(months_value)
        curve = curves.get(str(months))
        if curve is None:
            continue
        methods = curve["thresholdSelectionAndConfirmation"]["methods"]
        handcrafted = methods.get("handcrafted_logistic", {}).get("confirmationMetrics")
        random_encoder = methods.get("random_frozen_linear", {}).get("confirmationMetrics")
        if handcrafted is None or random_encoder is None:
            continue
        for method in ("pretrained_linear", "pretrained_mlp2"):
            result = methods.get(method, {})
            metrics = result.get("confirmationMetrics")
            if metrics is None:
                continue
            reasons = {
                "minimumTrades": metrics["trades"] >= int(minimum_confirmation_trades),
                "positivePnl": metrics["pnl"] > 0,
                "majorityPositiveMonths": metrics["positiveMonths"] > metrics["months"] / 2,
                "pnlAboveHandcrafted": metrics["pnl"] > handcrafted["pnl"],
                "pnlAboveRandomEncoder": metrics["pnl"] > random_encoder["pnl"],
                "winRateAboveHandcrafted": (
                    metrics["winRate"] is not None
                    and handcrafted["winRate"] is not None
                    and metrics["winRate"] > handcrafted["winRate"]
                ),
                "winRateAboveRandomEncoder": (
                    metrics["winRate"] is not None
                    and random_encoder["winRate"] is not None
                    and metrics["winRate"] > random_encoder["winRate"]
                ),
            }
            candidates.append(
                {
                    "labelMonths": months,
                    "method": method,
                    "selectedMinEv": result.get("selectedMinEv"),
                    "confirmationMetrics": metrics,
                    "checks": reasons,
                    "passed": all(reasons.values()),
                }
            )
    passed = [candidate for candidate in candidates if candidate["passed"]]
    return {
        "passed": bool(passed),
        "minimumConfirmationTrades": int(minimum_confirmation_trades),
        "fewShotLabelMonths": [int(item) for item in few_shot_months],
        "rule": (
            "On confirmation only: pretrained linear or MLP with 1/3 label months must have "
            "at least 300 trades, positive PnL, majority positive months, and both PnL and "
            "win rate above handcrafted logistic and random frozen encoder."
        ),
        "passingCandidates": passed,
        "allCandidates": candidates,
        "frozenHoldoutAutomaticallyRun": False,
    }


def run_learning_curves(
    samples: pd.DataFrame,
    features: Mapping[str, np.ndarray],
    *,
    development_start: str | pd.Timestamp,
    development_end_exclusive: str | pd.Timestamp,
    label_months_values: Sequence[int],
    label_history_start: str | pd.Timestamp | None,
    threshold_selection_end_exclusive: str | pd.Timestamp,
    min_selection_trades: int,
    payout_rate: float,
    stake: float,
    min_ev_grid: Sequence[float],
    seed: int = 7,
    min_train_rows: int = 2,
) -> dict[str, Any]:
    curves: dict[str, Any] = {}
    for label_months_value in label_months_values:
        label_months = int(label_months_value)
        predictions, folds = walk_forward_predictions(
            samples,
            features,
            development_start=development_start,
            development_end_exclusive=development_end_exclusive,
            label_months=label_months,
            label_history_start=label_history_start,
            seed=seed,
            min_train_rows=min_train_rows,
        )
        curves[str(label_months)] = {
            "labelMonths": label_months,
            "folds": folds,
            "metricsByMinEv": evaluate_min_ev_grid(
                predictions,
                payout_rate=payout_rate,
                stake=stake,
                min_ev_grid=min_ev_grid,
            ),
            "thresholdSelectionAndConfirmation": select_and_confirm_min_ev(
                predictions,
                selection_start=development_start,
                selection_end_exclusive=threshold_selection_end_exclusive,
                confirmation_end_exclusive=development_end_exclusive,
                payout_rate=payout_rate,
                stake=stake,
                min_ev_grid=min_ev_grid,
                min_selection_trades=min_selection_trades,
            ),
        }
    return curves
