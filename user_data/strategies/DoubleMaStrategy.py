# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort:skip_file
# --- Do not remove these imports ---
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from pandas import DataFrame
from typing import Optional
from functools import reduce

from freqtrade.strategy import (
    IStrategy,
    Trade,
    Order,
    PairLocks,
    informative,
    BooleanParameter,
    CategoricalParameter,
    DecimalParameter,
    IntParameter,
    RealParameter,
    merge_informative_pair,
    stoploss_from_open,
    stoploss_from_absolute,
)

# ==============================
# 双均线交叉 + RSI 过滤策略
#   作者: 自定义
#   适合周期: 5m / 1h
#   说明:
#   - 当短期EMA向上穿越长期EMA，且RSI不超卖时买入
#   - 当短期EMA向下穿越长期EMA，或RSI超买时卖出
# ==============================

import talib.abstract as ta
from technical import qtpylib


class DoubleMaStrategy(IStrategy):
    """
    双均线交叉策略 (EMA Cross + RSI Filter)
    """

    # ==========================
    # 策略基本参数
    # ==========================
    INTERFACE_VERSION = 3

    # 最大持仓数量
    max_open_trades = 3

    # 单笔投入比例（每笔交易使用总资金的多少）
    stake_amount = "unlimited"

    # 时间周期
    timeframe = "5m"

    # 止损（-10%）
    stoploss = -0.10

    # ROI 目标（投资回报率，达到此盈利则平仓）
    minimal_roi = {
        "0": 0.05,    # 随时盈利 5% 则平仓
        "30": 0.03,   # 持仓 30 分钟后，盈利 3% 则平仓
        "60": 0.02,   # 持仓 60 分钟后，盈利 2% 则平仓
        "120": 0.01,  # 持仓 120 分钟后，盈利 1% 则平仓
    }

    # 移动止损 —— 从最高点回撤 2% 触发
    trailing_stop = True
    trailing_stop_positive = 0.02
    trailing_stop_positive_offset = 0.03  # 盈利超过 3% 后才开启移动止损
    trailing_only_offset_is_reached = True

    # ==========================
    # 超参数搜索空间（贝叶斯优化）
    # ==========================
    buy_ema_short = IntParameter(5, 30, default=10, space="buy")
    buy_ema_long  = IntParameter(20, 100, default=50, space="buy")
    buy_rsi_limit = IntParameter(20, 50, default=30, space="buy")
    buy_stochrsi_limit = DecimalParameter(0.1, 0.4, default=0.2, space="buy")

    sell_rsi_limit = IntParameter(60, 85, default=75, space="sell")
    sell_stochrsi_limit = DecimalParameter(0.6, 0.9, default=0.8, space="sell")

    # ==========================
    # 技术指标计算
    # ==========================
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """计算所有技术指标"""

        # EMA 短期 / 长期
        for val in self.buy_ema_short.range:
            dataframe[f"ema_{val}"] = ta.EMA(dataframe, timeperiod=val)
        for val in self.buy_ema_long.range:
            dataframe[f"ema_{val}"] = ta.EMA(dataframe, timeperiod=val)

        # RSI（14周期）
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)

        # StochRSI（随机相对强弱指标）
        stochrsi = ta.STOCHRSI(dataframe, timeperiod=14, fastk_period=3, fastd_period=3)
        dataframe["fastk"] = stochrsi["fastk"]
        dataframe["fastd"] = stochrsi["fastd"]

        # MACD
        macd = ta.MACD(dataframe)
        dataframe["macd"]        = macd["macd"]
        dataframe["macdsignal"]  = macd["macdsignal"]
        dataframe["macdhist"]    = macd["macdhist"]

        # 布林带
        bollinger = qtpylib.bollinger_bands(
            qtpylib.typical_price(dataframe), window=20, stds=2
        )
        dataframe["bb_lowerband"] = bollinger["lower"]
        dataframe["bb_middleband"] = bollinger["mid"]
        dataframe["bb_upperband"]  = bollinger["upper"]
        dataframe["bb_percent"] = (
            (dataframe["close"] - dataframe["bb_lowerband"])
            / (dataframe["bb_upperband"] - dataframe["bb_lowerband"])
        )

        # 成交量均线
        dataframe["volume_mean"] = dataframe["volume"].rolling(20).mean()

        return dataframe

    # ==========================
    # 买入信号
    # ==========================
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """定义买入信号"""

        ema_short = dataframe[f"ema_{self.buy_ema_short.value}"]
        ema_long  = dataframe[f"ema_{self.buy_ema_long.value}"]

        dataframe.loc[
            (
                # 趋势过滤：短期 EMA 在长期 EMA 之上（处于上升通道）
                (ema_short > ema_long) &
                # StochRSI 金叉且处于超卖区（回调买入点）
                qtpylib.crossed_above(dataframe["fastk"], dataframe["fastd"]) &
                (dataframe["fastk"] < self.buy_stochrsi_limit.value) &
                # RSI 处于健康的中间区间（非极度超卖/死鱼行情）
                (dataframe["rsi"] > self.buy_rsi_limit.value) &
                # 成交量高于均值（确认放量）
                (dataframe["volume"] > dataframe["volume_mean"] * 0.8) &
                # K线有效（排除异常数据）
                (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1

        return dataframe

    # ==========================
    # 卖出信号
    # ==========================
    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """定义卖出信号"""

        ema_short = dataframe[f"ema_{self.buy_ema_short.value}"]
        ema_long  = dataframe[f"ema_{self.buy_ema_long.value}"]

        dataframe.loc[
            (
                # EMA 短期下穿长期（死叉，强力趋势离场）
                qtpylib.crossed_below(ema_short, ema_long) |
                # StochRSI 形成死叉且处于超买区（高位止盈）
                (
                    qtpylib.crossed_below(dataframe["fastk"], dataframe["fastd"]) &
                    (dataframe["fastk"] > self.sell_stochrsi_limit.value)
                ) |
                # RSI 超买（双重止盈过滤）
                (dataframe["rsi"] > self.sell_rsi_limit.value)
            ),
            "exit_long",
        ] = 1

        return dataframe
