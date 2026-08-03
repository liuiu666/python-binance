import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))


def load_module(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


v36 = load_module("research_cross_market_normal_router_v36", "py/research_cross_market_normal_router_v36.py")
v37 = load_module("research_cross_market_walkforward_v37", "py/research_cross_market_walkforward_v37.py")


def test_cross_market_features_do_not_change_before_modified_future_rows():
    index = pd.date_range("2020-01-01", periods=2000, freq="min", tz="UTC")
    phase = np.arange(len(index), dtype=float)
    btc = pd.DataFrame({"close": 100.0 * np.exp(np.cumsum(np.sin(phase / 31.0) * 0.0001))}, index=index)
    eth = pd.DataFrame({"close": 50.0 * np.exp(np.cumsum(np.cos(phase / 29.0) * 0.00012))}, index=index)
    baseline = v36.build_features(btc, eth)

    changed = eth.copy()
    changed.loc[index[-10]:, "close"] *= 1.05
    observed = v36.build_features(btc, changed)

    pd.testing.assert_frame_equal(baseline.iloc[:-10], observed.iloc[:-10])


def test_symmetric_router_flips_only_low_reversion_probability_rows():
    frame = pd.DataFrame(
        {
            "signal_pos": [10, 30],
            "reversion_probability": [0.7, 0.3],
            "direction": [1, -1],
            "signed_bps_exact": [2.0, -3.0],
            "signed_bps_delayed": [1.0, -4.0],
            "signed_bps_fixed_settlement": [0.5, -2.0],
        }
    )

    routed = v37.route_predictions(frame, "symmetric_reversion_or_continuation")

    assert routed["routed_action"].tolist() == ["reversion", "continuation"]
    assert routed["direction"].tolist() == [1, 1]
    assert routed["signed_bps_delayed"].tolist() == [1.0, 4.0]
