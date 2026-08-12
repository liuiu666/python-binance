from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler


@dataclass
class WorldPrediction:
    p_up: np.ndarray
    future_state: np.ndarray
    transition_confidence: np.ndarray


def _logistic(random_state: int) -> Pipeline:
    return Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("scale", StandardScaler()),
        ("model", LogisticRegression(C=0.1, max_iter=1000, random_state=random_state)),
    ])


class LogisticDirectionModel:
    def __init__(self, random_state: int = 7):
        self.model = _logistic(random_state)

    def fit(self, x: np.ndarray, y: np.ndarray) -> "LogisticDirectionModel":
        self.model.fit(x, y)
        return self

    def predict_up(self, x: np.ndarray) -> np.ndarray:
        return self.model.predict_proba(x)[:, 1]


class LatentWorldModel:
    """Predict direction and action-relevant future latent state.

    The direction head conditions on the current latent state. A separate
    transition head predicts the state at settlement. Its confidence and state
    support are consumed by the planner as conservative penalties.
    """

    def __init__(self, feature_count: int, random_state: int = 7):
        numeric = list(range(feature_count))
        state = [feature_count]
        preprocess = ColumnTransformer([
            ("numeric", Pipeline([("imputer", SimpleImputer(strategy="median")), ("scale", StandardScaler())]), numeric),
            ("state", OneHotEncoder(handle_unknown="ignore"), state),
        ])
        self.direction = Pipeline([
            ("preprocess", preprocess),
            ("model", LogisticRegression(C=0.1, max_iter=1000, random_state=random_state)),
        ])
        self.transition = Pipeline([
            ("preprocess", preprocess),
            ("model", LogisticRegression(C=0.2, max_iter=1000, random_state=random_state)),
        ])

    @staticmethod
    def _join(x: np.ndarray, state: np.ndarray) -> np.ndarray:
        return np.column_stack([np.asarray(x, dtype=np.float32), np.asarray(state, dtype=np.int16)])

    def fit(self, x: np.ndarray, current_state: np.ndarray, y_up: np.ndarray, future_state: np.ndarray) -> "LatentWorldModel":
        joined = self._join(x, current_state)
        self.direction.fit(joined, y_up)
        self.transition.fit(joined, future_state)
        return self

    def predict(self, x: np.ndarray, current_state: np.ndarray) -> WorldPrediction:
        joined = self._join(x, current_state)
        p_up = self.direction.predict_proba(joined)[:, 1]
        transition_probability = self.transition.predict_proba(joined)
        return WorldPrediction(
            p_up=p_up,
            future_state=self.transition.classes_[transition_probability.argmax(axis=1)].astype("int16"),
            transition_confidence=transition_probability.max(axis=1).astype("float32"),
        )


def momentum_probability(x_ret_10: np.ndarray, scale: float = 0.002) -> np.ndarray:
    values = np.asarray(x_ret_10, dtype=float)
    return np.clip(0.5 + values / max(scale, 1e-9) * 0.25, 0.01, 0.99)
