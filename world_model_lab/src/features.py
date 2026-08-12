from __future__ import annotations

import numpy as np
import pandas as pd

RETURN_WINDOWS = (10, 30, 60, 120, 300, 600)
VOL_WINDOWS = (60, 300, 600)
POSITION_WINDOWS = (120, 300, 600)
FLOW_WINDOWS = (10, 60, 300)


def _safe_ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    return numerator / denominator.replace(0.0, np.nan)


def build_features(minutes: pd.DataFrame) -> pd.DataFrame:
    """Build vectorized causal features from completed minute bars."""
    data = minutes.copy()
    close = data["close"].astype("float64")
    log_return = np.log(close).diff()
    data["realized_vol_10"] = log_return.rolling(10, min_periods=10).std(ddof=0) * np.sqrt(10.0)

    feature_names: list[str] = []
    for width in RETURN_WINDOWS:
        name = f"x_ret_{width}"
        data[name] = np.log(close / close.shift(width))
        feature_names.append(name)
    for width in VOL_WINDOWS:
        name = f"x_vol_{width}"
        data[name] = log_return.rolling(width, min_periods=width).std(ddof=0) * np.sqrt(width)
        feature_names.append(name)
    for width in POSITION_WINDOWS:
        rolling = close.rolling(width, min_periods=width)
        mean = rolling.mean()
        std = rolling.std(ddof=0).replace(0.0, np.nan)
        z_name = f"x_z_{width}"
        pos_name = f"x_range_pos_{width}"
        data[z_name] = (close - mean) / std
        low = rolling.min()
        high = rolling.max()
        data[pos_name] = _safe_ratio(close - low, high - low)
        feature_names.extend([z_name, pos_name])

    taker_share = _safe_ratio(data["taker_buy_volume"].astype(float), data["volume"].astype(float)).clip(0.0, 1.0)
    for width in FLOW_WINDOWS:
        volume = data["volume"].rolling(width, min_periods=width).sum()
        taker = data["taker_buy_volume"].rolling(width, min_periods=width).sum()
        share_name = f"x_taker_share_{width}"
        data[share_name] = _safe_ratio(taker, volume)
        feature_names.append(share_name)
    data["x_taker_share_1"] = taker_share
    feature_names.append("x_taker_share_1")

    for source in ("volume", "quote_volume", "trades"):
        value = data[source].astype(float)
        short = value.rolling(10, min_periods=10).mean()
        long = value.rolling(300, min_periods=300).mean()
        name = f"x_{source}_ratio_10_300"
        data[name] = _safe_ratio(short, long)
        feature_names.append(name)

    minute_of_day = data.index.hour * 60 + data.index.minute
    day_of_week = data.index.dayofweek
    data["x_time_sin"] = np.sin(2.0 * np.pi * minute_of_day / 1440.0)
    data["x_time_cos"] = np.cos(2.0 * np.pi * minute_of_day / 1440.0)
    data["x_week_sin"] = np.sin(2.0 * np.pi * day_of_week / 7.0)
    data["x_week_cos"] = np.cos(2.0 * np.pi * day_of_week / 7.0)
    feature_names.extend(["x_time_sin", "x_time_cos", "x_week_sin", "x_week_cos"])

    for name in feature_names + ["realized_vol_10"]:
        data[name] = data[name].replace([np.inf, -np.inf], np.nan).astype("float32")
    return data[["close", "realized_vol_10", *feature_names]].dropna()


def feature_columns(frame: pd.DataFrame) -> list[str]:
    return [name for name in frame.columns if name.startswith("x_") and not name.startswith("future__")]


def future_feature_columns(frame: pd.DataFrame) -> list[str]:
    return [name for name in frame.columns if name.startswith("future__x_")]
