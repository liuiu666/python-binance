"""
技术指标计算模块
功能:
- EMA (快/慢线)
- RSI
- ATR
- MACD
- Bollinger Bands
- 使用 pandas-ta-classic 库实现
- 增量计算: 新 K 线到来时只更新最后一行, 不全量重算
"""

from __future__ import annotations

from typing import Dict, Optional

import pandas as pd
import pandas_ta_classic as ta

from common.logger import get_logger

logger = get_logger(__name__)


def compute_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算所有技术指标并附加到 DataFrame

    Args:
        df: 包含 OHLCV 数据的 DataFrame

    Returns:
        附加了指标列的 DataFrame
    """
    if df.empty or len(df) < 30:
        return df

    try:
        # EMA (快线 9, 慢线 21)
        df = compute_ema(df, periods=[9, 21])

        # RSI (14)
        df = compute_rsi(df, period=14)

        # ATR (14)
        df = compute_atr(df, period=14)

        # MACD (12, 26, 9)
        df = compute_macd(df)

        # Bollinger Bands (20, 2.0)
        df = compute_bollinger(df, period=20, std=2.0)

    except Exception:
        logger.exception("indicators.compute_error")

    return df


def compute_ema(df: pd.DataFrame, periods: list = None) -> pd.DataFrame:
    """
    计算指数移动平均线 (EMA)

    Args:
        df: OHLCV DataFrame
        periods: EMA 周期列表, 默认 [9, 21]

    Returns:
        附加了 EMA 列的 DataFrame
    """
    if periods is None:
        periods = [9, 21]

    for period in periods:
        col_name = f"ema_{period}"
        if col_name not in df.columns or len(df) >= period:
            df[col_name] = ta.ema(df["close_price"], length=period)

    return df


def compute_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    计算相对强弱指数 (RSI)

    Args:
        df: OHLCV DataFrame
        period: RSI 周期, 默认 14

    Returns:
        附加了 RSI 列的 DataFrame
    """
    if len(df) < period + 1:
        return df

    df["rsi"] = ta.rsi(df["close_price"], length=period)
    return df


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    计算平均真实范围 (ATR)

    Args:
        df: OHLCV DataFrame
        period: ATR 周期, 默认 14

    Returns:
        附加了 ATR 列的 DataFrame
    """
    if len(df) < period + 1:
        return df

    # pandas-ta 需要标准列名
    high = df["high_price"]
    low = df["low_price"]
    close = df["close_price"]

    df["atr"] = ta.atr(high, low, close, length=period)
    return df


def compute_macd(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """
    计算 MACD 指标

    Args:
        df: OHLCV DataFrame
        fast: 快线周期
        slow: 慢线周期
        signal: 信号线周期

    Returns:
        附加了 MACD 列的 DataFrame
    """
    if len(df) < slow + signal:
        return df

    macd_result = ta.macd(df["close_price"], fast=fast, slow=slow, signal=signal)
    if macd_result is not None:
        # pandas-ta 返回多列, 列名格式: MACD_12_26_9, MACDh_12_26_9, MACDs_12_26_9
        for col in macd_result.columns:
            if "MACD_" in col and "h" not in col.lower() and "s" not in col.lower():
                df["macd"] = macd_result[col]
            elif "MACDh" in col:
                df["macd_histogram"] = macd_result[col]
            elif "MACDs" in col:
                df["macd_signal"] = macd_result[col]

    return df


def compute_bollinger(
    df: pd.DataFrame,
    period: int = 20,
    std: float = 2.0,
) -> pd.DataFrame:
    """
    计算布林带 (Bollinger Bands)

    Args:
        df: OHLCV DataFrame
        period: 周期, 默认 20
        std: 标准差倍数, 默认 2.0

    Returns:
        附加了布林带列的 DataFrame
    """
    if len(df) < period:
        return df

    bb = ta.bbands(df["close_price"], length=period, std=std)
    if bb is not None:
        for col in bb.columns:
            if "BBU" in col:
                df["bb_upper"] = bb[col]
            elif "BBM" in col:
                df["bb_middle"] = bb[col]
            elif "BBL" in col:
                df["bb_lower"] = bb[col]

    return df


def get_latest_indicators(df: pd.DataFrame) -> Dict:
    """
    获取最新一根 K 线的所有指标值

    Args:
        df: 带有指标列的 DataFrame

    Returns:
        指标值字典
    """
    if df.empty:
        return {}

    last = df.iloc[-1]
    result = {}

    indicator_cols = [
        "ema_9", "ema_21", "rsi", "atr",
        "macd", "macd_signal", "macd_histogram",
        "bb_upper", "bb_middle", "bb_lower",
    ]

    for col in indicator_cols:
        if col in df.columns:
            val = last.get(col)
            if pd.notna(val):
                result[col] = float(val)

    return result
