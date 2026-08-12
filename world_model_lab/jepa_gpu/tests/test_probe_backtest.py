from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from src.backtest import (
    Fold,
    development_gate,
    evaluate_min_ev_grid,
    monthly_walk_forward_folds,
    select_and_confirm_min_ev,
    select_fold_rows,
    walk_forward_predictions,
)
from src.embeddings import MINUTE_NS, build_decision_frame, handcrafted_features
from src.metrics import action_values, plan_actions
from src.probe import ConstantProbabilityModel, fit_linear_probe


class FakeAsset:
    def __init__(self, rows: int = 40):
        start = pd.Timestamp("2025-01-01T00:00:00Z").value
        self.times = start + np.arange(rows, dtype=np.int64) * MINUTE_NS
        self.close = (100.0 + np.arange(rows, dtype=np.float32))
        base = np.arange(rows, dtype=np.float32)[:, None]
        self.values = np.concatenate([base + channel for channel in range(8)], axis=1)


class EmbeddingSampleTests(unittest.TestCase):
    def test_open_time_plus_one_minute_and_ten_minute_label(self):
        asset = FakeAsset()
        samples = build_decision_frame(
            asset,
            "2025-01-01T00:00:00Z",
            "2025-01-01T00:31:00Z",
            step_minutes=10,
            context_minutes=5,
            horizon_minutes=10,
            max_settle_time="2025-01-01T00:30:00Z",
        )
        self.assertEqual(samples["time"].dt.strftime("%H:%M").tolist(), ["00:10", "00:20"])
        self.assertEqual(samples["sample_index"].tolist(), [9, 19])
        self.assertEqual(samples["context_start_index"].tolist(), [5, 15])
        self.assertEqual(samples["context_end_open_time"].dt.strftime("%H:%M").tolist(), ["00:09", "00:19"])
        self.assertEqual(samples["settle_time"].dt.strftime("%H:%M").tolist(), ["00:20", "00:30"])
        np.testing.assert_array_equal(samples["entry"], asset.close[[9, 19]])
        np.testing.assert_array_equal(samples["settle"], asset.close[[19, 29]])

    def test_january_has_all_ten_minute_decisions_through_month_end(self):
        history_minutes = 8 * 60
        january_minutes = 31 * 24 * 60
        asset = FakeAsset(rows=history_minutes + january_minutes)
        asset.times -= history_minutes * MINUTE_NS
        samples = build_decision_frame(
            asset,
            "2025-01-01T00:00:00Z",
            "2025-02-01T00:00:00Z",
            step_minutes=10,
            context_minutes=480,
            horizon_minutes=10,
            max_settle_time="2025-02-01T00:00:00Z",
        )
        self.assertEqual(len(samples), 4464)
        self.assertEqual(samples["time"].iloc[0], pd.Timestamp("2025-01-01T00:00:00Z"))
        self.assertEqual(samples["time"].iloc[-1], pd.Timestamp("2025-01-31T23:50:00Z"))
        self.assertEqual(samples["settle_time"].iloc[-1], pd.Timestamp("2025-02-01T00:00:00Z"))

    def test_handcrafted_features_are_causal(self):
        original = FakeAsset(rows=600)
        changed = FakeAsset(rows=600)
        changed.values[501:] += 1_000_000
        first, names = handcrafted_features(original, np.array([500]))
        second, second_names = handcrafted_features(changed, np.array([500]))
        np.testing.assert_allclose(first, second)
        self.assertEqual(names, second_names)
        self.assertEqual(first.shape, (1, 88))


class ProbeTests(unittest.TestCase):
    def test_single_class_probe_has_deterministic_probability(self):
        model = fit_linear_probe(np.ones((4, 3), dtype=np.float32), np.ones(4, dtype=np.int8))
        self.assertIsInstance(model, ConstantProbabilityModel)
        np.testing.assert_array_equal(model.predict_up(np.zeros((2, 3))), np.ones(2))


class BacktestTests(unittest.TestCase):
    @staticmethod
    def samples() -> pd.DataFrame:
        return pd.DataFrame(
            {
                "time": pd.to_datetime(
                    [
                        "2025-01-01T00:00:00Z",
                        "2025-01-31T23:55:00Z",
                        "2025-02-01T00:00:00Z",
                        "2025-02-28T23:55:00Z",
                        "2025-03-01T00:00:00Z",
                    ],
                    utc=True,
                ),
                "settle_time": pd.to_datetime(
                    [
                        "2025-01-01T00:10:00Z",
                        "2025-02-01T00:05:00Z",
                        "2025-02-01T00:10:00Z",
                        "2025-03-01T00:05:00Z",
                        "2025-03-01T00:10:00Z",
                    ],
                    utc=True,
                ),
                "entry": [1.0] * 5,
                "settle": [2.0, 0.0, 2.0, 0.0, 2.0],
                "raw_move": [1.0, -1.0, 1.0, -1.0, 1.0],
                "up": [True, False, True, False, True],
                "tie": [False] * 5,
            }
        )

    def test_training_labels_must_be_settled_by_test_start(self):
        fold = Fold(
            1,
            pd.Timestamp("2025-01-01T00:00:00Z"),
            pd.Timestamp("2025-02-01T00:00:00Z"),
            pd.Timestamp("2025-02-01T00:00:00Z"),
            pd.Timestamp("2025-03-01T00:00:00Z"),
        )
        train, test = select_fold_rows(self.samples(), fold)
        self.assertEqual(train.tolist(), [0])
        self.assertEqual(test.tolist(), [2])

    def test_monthly_folds_use_trailing_label_window(self):
        folds = monthly_walk_forward_folds("2025-01-01T00:00:00Z", "2025-05-01T00:00:00Z", 1)
        self.assertEqual(len(folds), 3)
        self.assertEqual(folds[-1].train_start, pd.Timestamp("2025-03-01T00:00:00Z"))
        self.assertEqual(folds[-1].test_start, pd.Timestamp("2025-04-01T00:00:00Z"))

    def test_twelve_month_curve_has_full_window_from_first_test(self):
        folds = monthly_walk_forward_folds(
            "2025-01-01T00:00:00Z", "2026-02-01T00:00:00Z", 12,
            label_history_start="2024-01-01T00:00:00Z",
        )
        self.assertEqual(len(folds), 13)
        self.assertEqual(folds[0].train_start, pd.Timestamp("2024-01-01T00:00:00Z"))
        self.assertEqual(folds[0].test_start, pd.Timestamp("2025-01-01T00:00:00Z"))
        for fold in folds:
            self.assertEqual(fold.test_start, fold.train_start + pd.DateOffset(months=12))

    def test_walk_forward_records_label_boundary(self):
        samples = self.samples().iloc[:3].copy()
        features = {"handcrafted_logistic": np.arange(6, dtype=np.float32).reshape(3, 2)}

        class FixedModel:
            def predict_up(self, rows):
                return np.full(len(rows), 0.75)

        with patch("src.backtest.fit_method", return_value=FixedModel()):
            predictions, reports = walk_forward_predictions(
                samples,
                features,
                development_start="2025-01-01T00:00:00Z",
                development_end_exclusive="2025-03-01T00:00:00Z",
                label_months=1,
                min_train_rows=1,
            )
        self.assertEqual(len(predictions["handcrafted_logistic"]), 1)
        self.assertEqual(reports[0]["latestAllowedTrainSettleTime"], "2025-02-01T00:00:00+00:00")
        self.assertEqual(reports[0]["trainRows"], 1)

    def test_threshold_selection_does_not_use_confirmation_results(self):
        prediction = pd.DataFrame(
            {
                "time": pd.to_datetime(
                    [
                        "2025-01-01T00:00:00Z",
                        "2025-01-01T00:10:00Z",
                        "2025-02-01T00:00:00Z",
                        "2025-02-01T00:10:00Z",
                    ],
                    utc=True,
                ),
                "settle_time": pd.to_datetime(
                    [
                        "2025-01-01T00:10:00Z",
                        "2025-01-01T00:20:00Z",
                        "2025-02-01T00:10:00Z",
                        "2025-02-01T00:20:00Z",
                    ],
                    utc=True,
                ),
                "entry": [1.0] * 4,
                "settle": [2.0, 0.0, 0.0, 2.0],
                "raw_move": [1.0, -1.0, -1.0, 1.0],
                "up": [True, False, False, True],
                "tie": [False] * 4,
                "p_up": [0.8, 0.52, 0.8, 0.52],
            }
        )
        result = select_and_confirm_min_ev(
            {"method": prediction},
            selection_start="2025-01-01T00:00:00Z",
            selection_end_exclusive="2025-02-01T00:00:00Z",
            confirmation_end_exclusive="2025-03-01T00:00:00Z",
            payout_rate=0.8,
            stake=5.0,
            min_ev_grid=[0.0, 0.3],
            min_selection_trades=1,
        )["methods"]["method"]
        self.assertEqual(result["selectedMinEv"], 0.0)
        self.assertGreater(result["selectionMetrics"]["pnl"], 0.0)
        self.assertLess(result["confirmationMetrics"]["pnl"], 0.0)

    def test_development_gate_uses_few_shot_confirmation_metrics(self):
        def metrics(pnl, win_rate):
            return {
                "trades": 400,
                "pnl": pnl,
                "winRate": win_rate,
                "positiveMonths": 5,
                "months": 7,
            }

        methods = {
            "handcrafted_logistic": {
                "confirmationMetrics": metrics(10.0, 56.0),
            },
            "random_frozen_linear": {
                "confirmationMetrics": metrics(5.0, 55.8),
            },
            "pretrained_linear": {
                "selectedMinEv": 0.03,
                "confirmationMetrics": metrics(30.0, 57.0),
            },
            "pretrained_mlp2": {
                "selectedMinEv": 0.06,
                "confirmationMetrics": metrics(-5.0, 55.0),
            },
        }
        curves = {
            "1": {
                "thresholdSelectionAndConfirmation": {
                    "methods": methods,
                }
            }
        }
        gate = development_gate(curves)
        self.assertTrue(gate["passed"])
        self.assertEqual(len(gate["passingCandidates"]), 1)
        self.assertEqual(
            gate["passingCandidates"][0]["method"],
            "pretrained_linear",
        )
        self.assertFalse(gate["frozenHoldoutAutomaticallyRun"])

    def test_payout_ev_actions_and_metrics(self):
        up_ev, down_ev = action_values(np.array([0.7, 0.3, 0.5]), 0.8)
        np.testing.assert_allclose(up_ev, [0.26, -0.46, -0.1])
        np.testing.assert_allclose(down_ev, [-0.46, 0.26, -0.1])
        planned = plan_actions(np.array([0.7, 0.3, 0.5]), payout_rate=0.8, min_ev=0.0)
        self.assertEqual(planned["signal"].tolist(), ["UP", "DOWN", "SKIP"])

        prediction = pd.DataFrame(
            {
                "time": pd.to_datetime(["2025-01-01T00:00:00Z", "2025-01-01T00:10:00Z"], utc=True),
                "settle_time": pd.to_datetime(["2025-01-01T00:10:00Z", "2025-01-01T00:20:00Z"], utc=True),
                "entry": [1.0, 1.0],
                "settle": [2.0, 0.0],
                "raw_move": [1.0, -1.0],
                "up": [True, False],
                "tie": [False, False],
                "p_up": [0.7, 0.3],
            }
        )
        metrics = evaluate_min_ev_grid(
            {"method": prediction}, payout_rate=0.8, stake=5.0, min_ev_grid=[0.0, 0.3]
        )
        self.assertEqual(metrics["0.0000"]["method"]["trades"], 2)
        self.assertEqual(metrics["0.0000"]["method"]["pnl"], 8.0)
        self.assertEqual(metrics["0.0000"]["method"]["maxDrawdown"], 0.0)
        self.assertEqual(metrics["0.3000"]["method"]["trades"], 0)


if __name__ == "__main__":
    unittest.main()
