from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from src.backtest import guard_status, monthly_folds
from src.models import LatentWorldModel, LogisticDirectionModel
from src.state import LatentStateEncoder, transition_matrix


class StateBacktestTests(unittest.TestCase):
    def test_transform_does_not_refit(self):
        rng = np.random.default_rng(7)
        train = rng.normal(size=(1000, 4)).astype(np.float32)
        test = rng.normal(size=(50, 4)).astype(np.float32)
        encoder = LatentStateEncoder(n_states=4, random_state=7, max_fit_rows=1000).fit(train)
        centers = encoder.cluster.cluster_centers_.copy()
        first = encoder.transform(test).state
        second = encoder.transform(test).state
        np.testing.assert_array_equal(first, second)
        np.testing.assert_allclose(centers, encoder.cluster.cluster_centers_)

    def test_transition_rows_sum_to_one(self):
        matrix = transition_matrix(np.array([0, 0, 1]), np.array([1, 1, 0]), 3)
        np.testing.assert_allclose(matrix.sum(axis=1), np.ones(3))

    def test_models_fit_and_predict(self):
        rng = np.random.default_rng(17)
        x = rng.normal(size=(500, 5)).astype(np.float32)
        y = (x[:, 0] + 0.2 * x[:, 1] > 0).astype(np.int8)
        current = (x[:, 2] > 0).astype(np.int16)
        future = ((current + (x[:, 3] > 0).astype(np.int16)) % 3).astype(np.int16)
        baseline = LogisticDirectionModel(7).fit(x, y).predict_up(x[:20])
        world = LatentWorldModel(5, 7).fit(x, current, y, future).predict(x[:20], current[:20])
        self.assertEqual(baseline.shape, (20,))
        self.assertEqual(world.p_up.shape, (20,))
        self.assertEqual(world.future_state.shape, (20,))
        self.assertTrue(np.all((world.transition_confidence >= 0) & (world.transition_confidence <= 1)))

    def test_monthly_fold_is_strictly_chronological(self):
        folds = monthly_folds(pd.Timestamp("2020-01-01", tz="UTC"),
                              pd.Timestamp("2022-04-01", tz="UTC"),
                              pd.Timestamp("2022-01-01", tz="UTC"))
        self.assertEqual(len(folds), 3)
        for fold in folds:
            self.assertEqual(fold.train_end, fold.test_start)
            self.assertLess(fold.train_start, fold.train_end)
            self.assertLess(fold.test_start, fold.test_end)

    def test_frozen_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guard.json"
            self.assertEqual(guard_status(path, False), (True, True))
            path.write_text("{}", encoding="utf-8")
            self.assertEqual(guard_status(path, False), (False, False))
            self.assertEqual(guard_status(path, True), (True, False))


if __name__ == "__main__":
    unittest.main()
