from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.cluster import MiniBatchKMeans
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler


@dataclass
class StateBatch:
    state: np.ndarray
    distance_confidence: np.ndarray


class LatentStateEncoder:
    """Training-only fitted scaler and KMeans latent state encoder."""

    def __init__(self, n_states: int = 8, random_state: int = 7, max_fit_rows: int = 100000):
        self.n_states = int(n_states)
        self.random_state = int(random_state)
        self.max_fit_rows = int(max_fit_rows)
        self.imputer = SimpleImputer(strategy="median")
        self.scaler = StandardScaler()
        self.cluster = MiniBatchKMeans(
            n_clusters=self.n_states, batch_size=4096, n_init=5,
            random_state=self.random_state, reassignment_ratio=0.01,
        )
        self.fitted = False
        self.support_: np.ndarray | None = None

    def fit(self, values: np.ndarray) -> "LatentStateEncoder":
        matrix = np.asarray(values, dtype=np.float32)
        if len(matrix) < self.n_states * 20:
            raise ValueError("not enough rows to fit latent states")
        if len(matrix) > self.max_fit_rows:
            rng = np.random.default_rng(self.random_state)
            indices = np.sort(rng.choice(len(matrix), self.max_fit_rows, replace=False))
            fit_matrix = matrix[indices]
        else:
            fit_matrix = matrix
        transformed = self.scaler.fit_transform(self.imputer.fit_transform(fit_matrix))
        labels = self.cluster.fit_predict(transformed)
        counts = np.bincount(labels, minlength=self.n_states).astype(float)
        self.support_ = counts / max(counts.max(), 1.0)
        self.fitted = True
        return self

    def transform(self, values: np.ndarray) -> StateBatch:
        if not self.fitted:
            raise RuntimeError("state encoder must be fitted before transform")
        transformed = self.scaler.transform(self.imputer.transform(np.asarray(values, dtype=np.float32)))
        distances = self.cluster.transform(transformed)
        state = distances.argmin(axis=1).astype("int16")
        nearest = distances[np.arange(len(distances)), state]
        scale = np.median(nearest) + 1e-9
        confidence = np.exp(-nearest / scale).astype("float32")
        return StateBatch(state=state, distance_confidence=confidence)

    def state_support(self, states: np.ndarray) -> np.ndarray:
        if self.support_ is None:
            raise RuntimeError("state encoder has no fitted support")
        return self.support_[np.asarray(states, dtype=int)].astype("float32")


def transition_matrix(current: np.ndarray, future: np.ndarray, n_states: int, smoothing: float = 1.0) -> np.ndarray:
    matrix = np.full((n_states, n_states), float(smoothing), dtype=float)
    np.add.at(matrix, (np.asarray(current, dtype=int), np.asarray(future, dtype=int)), 1.0)
    return matrix / matrix.sum(axis=1, keepdims=True)
