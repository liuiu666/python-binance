from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from src.dataset import (
    AssetArrays,
    CHANNEL_NAMES,
    RandomWindowDataset,
    build_cache,
    decision_indices,
    dimensionless_channels,
)


class InMemoryAsset:
    def __init__(self, rows: int = 200):
        self.asset_id = 0
        self.values = np.repeat(
            np.arange(rows, dtype=np.float32)[:, None],
            len(CHANNEL_NAMES),
            axis=1,
        )
        self.times = (
            pd.Timestamp("2025-01-01T00:00:00Z").value
            + np.arange(rows, dtype=np.int64) * 60 * 1_000_000_000
        )
        self.close = np.arange(rows, dtype=np.float32)

    def end_index(self, end_exclusive: str) -> int:
        return int(
            np.searchsorted(
                self.times,
                pd.Timestamp(end_exclusive).value,
                side="left",
            )
        )


class DatasetTests(unittest.TestCase):
    def frame(self, rows: int = 100) -> pd.DataFrame:
        close = 100 + np.arange(rows) * 0.1
        volume = 10 + np.arange(rows) % 7
        return pd.DataFrame({"open": close - 0.05, "high": close + 0.1, "low": close - 0.1,
                             "close": close, "volume": volume, "quote_volume": volume * close,
                             "trades": 20 + np.arange(rows) % 3, "taker_buy_volume": volume * 0.55})

    def test_channels_shape_and_finite(self):
        channels = dimensionless_channels(self.frame())
        self.assertEqual(channels.shape, (100, len(CHANNEL_NAMES)))
        self.assertTrue(np.isfinite(channels).all())

    def test_future_mutation_is_causal(self):
        original = self.frame()
        changed = original.copy()
        changed.loc[51:, "close"] *= 2
        np.testing.assert_allclose(dimensionless_channels(original)[:51], dimensionless_channels(changed)[:51])

    def test_absolute_scale_invariance_for_price_channels(self):
        original = self.frame()
        scaled = original.copy()
        for name in ("open", "high", "low", "close"):
            scaled[name] *= 100
        np.testing.assert_allclose(dimensionless_channels(original)[:, :3],
                                   dimensionless_channels(scaled)[:, :3], rtol=1e-5, atol=1e-5)

    def test_target_offsets_are_future_patch_end_offsets(self):
        asset = InMemoryAsset()
        dataset = RandomWindowDataset(
            [asset],
            "2025-01-01T03:00:00Z",
            context_minutes=20,
            target_end_offsets=[10, 30, 60],
            target_minutes=10,
            seed=7,
            virtual_length=1,
            start_inclusive="2025-01-01T00:20:00Z",
        )
        context, targets, asset_id = dataset[0]
        anchor = int(context[-1, 0].item()) + 1
        boundary = asset.end_index("2025-01-01T00:20:00Z")
        self.assertGreaterEqual(anchor - 20, boundary)
        self.assertEqual(asset_id.item(), 0)
        np.testing.assert_array_equal(
            context[:, 0].numpy(),
            np.arange(anchor - 20, anchor, dtype=np.float32),
        )
        for target, end_offset in zip(targets, (10, 30, 60), strict=True):
            np.testing.assert_array_equal(
                target[:, 0].numpy(),
                np.arange(
                    anchor + end_offset - 10,
                    anchor + end_offset,
                    dtype=np.float32,
                ),
            )

    def test_cache_uses_nanoseconds_and_decision_bar_end(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame = self.frame(700)
            frame.insert(0, "open_time", pd.date_range("2025-01-01", periods=len(frame), freq="min", tz="UTC"))
            csv_path = root / "asset.csv"
            frame.to_csv(csv_path, index=False)
            prefix = root / "asset"
            build_cache([csv_path], prefix)
            asset = AssetArrays(prefix, 0)
            self.assertEqual(asset.times[1] - asset.times[0], 60 * 1_000_000_000)
            decisions = decision_indices(asset, "2025-01-01T08:00:00Z", "2025-01-01T10:00:00Z",
                                         step_minutes=10, context_minutes=480, horizon_minutes=10)
            self.assertEqual(len(decisions), 12)
            minute_ns = 60 * 1_000_000_000
            first_time = asset.times[decisions[0]] + minute_ns
            last_time = asset.times[decisions[-1]] + minute_ns
            self.assertEqual(first_time, pd.Timestamp("2025-01-01T08:00:00Z").value)
            self.assertEqual(last_time, pd.Timestamp("2025-01-01T09:50:00Z").value)
            self.assertEqual(first_time // minute_ns % 10, 0)
            asset.close_maps()


if __name__ == "__main__":
    unittest.main()
