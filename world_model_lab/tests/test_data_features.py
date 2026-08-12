from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from src.data import sample_decisions
from src.features import build_features, feature_columns


def minute_frame(rows: int = 900) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=rows, freq="min", tz="UTC")
    close = 100.0 + np.arange(rows, dtype=float) * 0.01
    volume = 10.0 + np.sin(np.arange(rows) / 10.0)
    return pd.DataFrame({
        "open": close - 0.01, "high": close + 0.02, "low": close - 0.02,
        "close": close, "volume": volume, "quote_volume": volume * close,
        "trades": 20.0 + np.arange(rows) % 5, "taker_buy_volume": volume * 0.55,
    }, index=index)


class FeatureTests(unittest.TestCase):
    def test_future_mutation_does_not_change_current_features(self):
        original = minute_frame()
        changed = original.copy()
        cutoff = original.index[700]
        changed.loc[changed.index > cutoff, "close"] *= 1.5
        left = build_features(original).loc[:cutoff]
        right = build_features(changed).loc[:cutoff]
        pd.testing.assert_frame_equal(left, right)

    def test_ten_minute_label_and_bar_end_time(self):
        features = build_features(minute_frame())
        samples = sample_decisions(features, step_minutes=10, horizon_minutes=10)
        row = samples.iloc[0]
        self.assertEqual(row["time"].minute % 10, 0)
        self.assertEqual(row["settle_time"] - row["time"], pd.Timedelta(minutes=10))
        # The feature at 10:00 decision belongs to the 09:59 opening-time bar.
        source_time = row["time"] - pd.Timedelta(minutes=1)
        self.assertAlmostEqual(row["entry"], features.loc[source_time, "close"])
        self.assertAlmostEqual(row["settle"], features.loc[source_time + pd.Timedelta(minutes=10), "close"])

    def test_future_targets_are_not_features(self):
        samples = sample_decisions(build_features(minute_frame()), step_minutes=10, horizon_minutes=10)
        self.assertTrue(any(name.startswith("future__") for name in samples.columns))
        self.assertFalse(any(name.startswith("future__") for name in feature_columns(samples)))


if __name__ == "__main__":
    unittest.main()
