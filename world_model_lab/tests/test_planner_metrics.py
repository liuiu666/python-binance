from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from src.metrics import evaluate_predictions, success_assessment, wilson_interval
from src.planner import action_values, plan_actions


class PlannerMetricTests(unittest.TestCase):
    def test_action_ev(self):
        up, down = action_values(np.array([1.0, 0.0, 0.5]), 0.8)
        np.testing.assert_allclose(up, [0.8, -1.0, -0.1])
        np.testing.assert_allclose(down, [-1.0, 0.8, -0.1])

    def test_skip_and_direction(self):
        plan = plan_actions(np.array([0.5, 0.8, 0.2]), payout_rate=0.8, min_ev=0.03)
        self.assertEqual(plan["signal"].tolist(), ["SKIP", "UP", "DOWN"])

    def test_uncertainty_can_only_remove_trade(self):
        plain = plan_actions(np.array([0.6]), payout_rate=0.8, min_ev=0.0)
        penalized = plan_actions(np.array([0.6]), payout_rate=0.8, min_ev=0.0,
                                 transition_confidence=np.array([0.0]), uncertainty_penalty=0.2)
        self.assertEqual(plain.iloc[0]["signal"], "UP")
        self.assertEqual(penalized.iloc[0]["signal"], "SKIP")

    def test_metrics_and_success(self):
        time = pd.date_range("2026-02-01", periods=4, freq="10min", tz="UTC")
        frame = pd.DataFrame({"time": time, "signal": ["UP", "DOWN", "SKIP", "UP"],
                              "up": [1, 0, 1, 0], "tie": [False] * 4,
                              "p_up": [0.8, 0.2, 0.7, 0.6]})
        metrics = evaluate_predictions(frame, 0.8, 5.0)
        self.assertEqual(metrics["trades"], 3)
        self.assertEqual(metrics["wins"], 2)
        self.assertAlmostEqual(metrics["pnl"], 3.0)
        low, high = wilson_interval(2, 3)
        self.assertLess(low, 2 / 3)
        self.assertGreater(high, 2 / 3)
        assessment = success_assessment(metrics, {**metrics, "winRate": 50.0, "pnl": 0.0}, {
            "min_win_rate_pct": 50, "min_trades": 1, "min_pnl": 0,
            "require_majority_positive_months": True, "require_world_model_beats_logistic": True,
        })
        self.assertTrue(assessment["passed"])


if __name__ == "__main__":
    unittest.main()
