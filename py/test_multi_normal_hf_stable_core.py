from __future__ import annotations

import json
import unittest

from multi_normal_hf_stable_core import (
    MultiNormalHFStableConfig,
    evaluate_snapshot,
)


def snapshot(**overrides):
    row = {
        "trend": "flat",
        "volatility": "sigma_low",
        "range": "range_tight",
        "normal_quality": "normal_ready",
        "normal_pos": "upper_edge",
        "sprint": "none",
        "volume": "vol_normal",
        "price": 101.4,
        "normal_center": 100.0,
        "normal_sigma": 1.0,
        "normal_low": 99.0,
        "normal_high": 101.0,
        "z": 1.4,
        "inside1_ratio": 0.6,
        "observed_pct": 96.0,
        "center_slope_bps": 0.5,
        "sigma_bps": 1.0,
        "sigma10_bps": 2.5,
        "range10_bps": 15.0,
        "ret10_bps": 2.0,
        "ret30_bps": 3.0,
        "ret60_bps": 4.0,
        "flow5": -0.05,
        "imb20": 0.0,
        "sigma_expand": 1.0,
        "sec_ret30_bps": -0.5,
        "sec_ret60_bps": -0.8,
        "flow30_now": -0.02,
        "flow30_delta": 0.01,
    }
    row.update(overrides)
    return row


class MultiNormalHFStableCoreTest(unittest.TestCase):
    def setUp(self):
        self.cfg = MultiNormalHFStableConfig()

    def test_lowvol_upper_tail_fades_down(self):
        result = evaluate_snapshot(snapshot(), self.cfg)
        self.assertEqual("DOWN", result["signal"])
        self.assertEqual("lowvol_normal_reversion", result["module"])
        self.assertEqual("lowvol_normal", result["market_state_detail"]["code"])
        self.assertEqual("ready", result["signal_paths"][0]["status"])
        self.assertAlmostEqual(101.2, result["signal_paths"][0]["observation"]["upper_watch_price"])

    def test_lowvol_lower_tail_fades_up(self):
        result = evaluate_snapshot(
            snapshot(z=-1.4, normal_pos="lower_edge", flow5=0.05),
            self.cfg,
        )
        self.assertEqual("UP", result["signal"])

    def test_lowvol_tail_over_limit_is_not_faded(self):
        result = evaluate_snapshot(snapshot(z=1.81), self.cfg)
        self.assertIsNone(result["signal"])
        self.assertEqual("flat_tail_may_be_regime_shift", result["reason"])

    def test_lowvol_tail_is_blocked_while_short_move_still_runs_outward(self):
        result = evaluate_snapshot(snapshot(sec_ret30_bps=1.26), self.cfg)
        self.assertIsNone(result["signal"])
        self.assertEqual("lowvol_short_move_still_outward", result["reason"])
        self.assertAlmostEqual(-1.26, result["signed_ret30_bps"])
        self.assertAlmostEqual(-1.25, result["min_signed_ret30_bps"])

    def test_lowvol_short_move_guard_scales_with_sigma(self):
        allowed = evaluate_snapshot(snapshot(sec_ret30_bps=1.25), self.cfg)
        blocked = evaluate_snapshot(snapshot(sec_ret30_bps=1.46, sigma10_bps=2.9), self.cfg)
        self.assertEqual("DOWN", allowed["signal"])
        self.assertIsNone(blocked["signal"])

    def test_lowvol_path_reports_flow_as_only_missing_condition(self):
        result = evaluate_snapshot(snapshot(flow5=0.05), self.cfg)
        path = result["signal_paths"][0]
        self.assertIsNone(result["signal"])
        self.assertEqual("watching", path["status"])
        self.assertEqual(8, path["passed"])
        self.assertEqual(["成交流转向"], [item["label"] for item in path["checks"] if item["ok"] is False])

    def test_high_vol_trend_uses_dynamic_lower_z_threshold(self):
        result = evaluate_snapshot(
            snapshot(
                trend="trend_up",
                sprint="up_sprint",
                z=0.6,
                sigma10_bps=8.0,
                flow5=0.2,
                imb20=0.05,
            ),
            self.cfg,
        )
        self.assertEqual("DOWN", result["signal"])
        self.assertEqual(0.5, result["z_required"])

    def test_mid_vol_trend_keeps_strict_z_threshold(self):
        result = evaluate_snapshot(
            snapshot(
                trend="trend_up",
                sprint="up_sprint",
                z=0.6,
                sigma10_bps=7.99,
                flow5=0.2,
                imb20=0.05,
            ),
            self.cfg,
        )
        self.assertIsNone(result["signal"])
        self.assertEqual(1.2, result["z_required"])

    def test_downtrend_exhaustion_reverses_up(self):
        result = evaluate_snapshot(
            snapshot(
                trend="trend_down",
                sprint="down_walk",
                z=-1.3,
                sigma10_bps=6.0,
                flow5=-0.2,
                imb20=-0.02,
            ),
            self.cfg,
        )
        self.assertEqual("UP", result["signal"])

    def test_orderbook_must_not_still_support_trend(self):
        blocked = evaluate_snapshot(
            snapshot(
                trend="trend_up",
                sprint="up_sprint",
                z=1.3,
                sigma10_bps=6.0,
                flow5=0.2,
                imb20=0.081,
            ),
            self.cfg,
        )
        allowed = evaluate_snapshot(
            snapshot(
                trend="trend_up",
                sprint="up_sprint",
                z=1.3,
                sigma10_bps=6.0,
                flow5=0.2,
                imb20=0.08,
            ),
            self.cfg,
        )
        self.assertIsNone(blocked["signal"])
        self.assertEqual("DOWN", allowed["signal"])

    def test_future_columns_cannot_change_decision(self):
        winning = evaluate_snapshot(snapshot(future10_bps=-1000.0), self.cfg)
        losing = evaluate_snapshot(snapshot(future10_bps=1000.0), self.cfg)
        self.assertEqual(winning["signal"], losing["signal"])
        self.assertEqual(winning["reason"], losing["reason"])

    def test_diagnostics_are_strict_json(self):
        payload = evaluate_snapshot(snapshot(), self.cfg)
        json.dumps(payload, ensure_ascii=False, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
