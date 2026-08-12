from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


METHOD_DESCRIPTIONS = {
    "handcrafted_logistic": "Causal rolling channel aggregates + logistic regression",
    "random_frozen_linear": "Random frozen JEPA encoder embedding + logistic linear probe",
    "pretrained_linear": "Pretrained frozen JEPA encoder embedding + logistic linear probe",
    "pretrained_mlp2": "Pretrained frozen JEPA encoder embedding + two-layer MLP probe",
}


class ConstantProbabilityModel:
    def __init__(self, probability: float):
        self.probability = float(probability)

    def predict_up(self, features: np.ndarray) -> np.ndarray:
        return np.full(len(features), self.probability, dtype=np.float64)


@dataclass
class SklearnProbabilityModel:
    estimator: Any

    def predict_up(self, features: np.ndarray) -> np.ndarray:
        probabilities = self.estimator.predict_proba(np.asarray(features))
        classes = np.asarray(self.estimator.classes_)
        matches = np.flatnonzero(classes == 1)
        if not len(matches):
            return np.zeros(len(features), dtype=np.float64)
        return probabilities[:, int(matches[0])].astype(np.float64, copy=False)


def _validated_training(features: np.ndarray, labels: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    x = np.asarray(features, dtype=np.float32)
    y = np.asarray(labels, dtype=np.int8)
    if x.ndim != 2:
        raise ValueError("probe features must be a two-dimensional matrix")
    if y.ndim != 1 or len(x) != len(y):
        raise ValueError("probe labels must be one-dimensional and aligned")
    if not len(x):
        raise ValueError("cannot fit a probe without training rows")
    if not np.isfinite(x).all():
        raise ValueError("probe features contain non-finite values")
    if not np.isin(y, [0, 1]).all():
        raise ValueError("probe labels must be binary")
    return x, y


def fit_linear_probe(features: np.ndarray, labels: np.ndarray, *, seed: int = 7) -> ConstantProbabilityModel | SklearnProbabilityModel:
    """Fit a standardized L2 logistic probe with a deterministic fallback."""
    x, y = _validated_training(features, labels)
    unique = np.unique(y)
    if len(unique) == 1:
        return ConstantProbabilityModel(float(unique[0]))

    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    estimator = make_pipeline(
        StandardScaler(),
        LogisticRegression(
            penalty="l2",
            C=1.0,
            solver="lbfgs",
            max_iter=500,
            random_state=int(seed),
        ),
    )
    estimator.fit(x, y)
    return SklearnProbabilityModel(estimator)


def fit_mlp2_probe(
    features: np.ndarray,
    labels: np.ndarray,
    *,
    seed: int = 7,
    hidden_sizes: tuple[int, int] | None = None,
) -> ConstantProbabilityModel | SklearnProbabilityModel:
    """Fit a standardized two-hidden-layer MLP probe.

    The default width scales conservatively with embedding dimension so the
    few-shot comparison does not silently become a very large classifier.
    """
    x, y = _validated_training(features, labels)
    unique = np.unique(y)
    if len(unique) == 1:
        return ConstantProbabilityModel(float(unique[0]))

    from sklearn.neural_network import MLPClassifier
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    if hidden_sizes is None:
        first = max(16, min(128, x.shape[1]))
        second = max(8, first // 2)
        hidden_sizes = (first, second)
    estimator = make_pipeline(
        StandardScaler(),
        MLPClassifier(
            hidden_layer_sizes=tuple(int(item) for item in hidden_sizes),
            activation="relu",
            solver="adam",
            alpha=1e-3,
            batch_size=min(256, len(x)),
            learning_rate_init=1e-3,
            max_iter=200,
            early_stopping=True,
            validation_fraction=0.1,
            n_iter_no_change=12,
            random_state=int(seed),
        ),
    )
    estimator.fit(x, y)
    return SklearnProbabilityModel(estimator)


def fit_method(method: str, features: np.ndarray, labels: np.ndarray, *, seed: int = 7) -> Any:
    if method in {"handcrafted_logistic", "random_frozen_linear", "pretrained_linear"}:
        return fit_linear_probe(features, labels, seed=seed)
    if method == "pretrained_mlp2":
        return fit_mlp2_probe(features, labels, seed=seed)
    raise ValueError(f"unknown probe method: {method}")
