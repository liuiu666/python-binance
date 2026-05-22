import numpy as np
import pandas as pd
from datetime import datetime
from pandas import DataFrame
import talib.abstract as ta
from technical import qtpylib
from freqtrade.strategy import IStrategy

class BinaryOptionsStrategy(IStrategy):
    """
    10-Minute Binary Options Backtesting Strategy for BTC.
    Uses Bollinger Bands and RSI to identify entry signals (Call/Put),
    and exits exactly after 10 minutes (2 candles of 5m).
    """
    INTERFACE_VERSION = 3

    # Enable shorting (for Put options)
    can_short = True

    # Timeframe of the candles
    timeframe = '5m'

    # Can open multiple trades
    max_open_trades = 10

    # Minimal ROI set to high value to prevent premature exit
    minimal_roi = {
        "0": 100.0
    }

    # Stoploss set to -100% to prevent stoploss exit
    stoploss = -1.00

    # Disable trailing stop
    trailing_stop = False

    # Indicators configuration
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Bollinger Bands
        bollinger = qtpylib.bollinger_bands(qtpylib.typical_price(dataframe), window=20, stds=2)
        dataframe['bb_lowerband'] = bollinger['lower']
        dataframe['bb_middleband'] = bollinger['mid']
        dataframe['bb_upperband'] = bollinger['upper']

        # RSI
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)

        return dataframe

    # Long entries (Call options)
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                # Close price is below lower Bollinger Band (oversold)
                (dataframe['close'] < dataframe['bb_lowerband']) &
                # RSI is oversold
                (dataframe['rsi'] < 30) &
                # Ensure volume is positive
                (dataframe['volume'] > 0)
            ),
            'enter_long'
        ] = 1

        # Short entries (Put options)
        dataframe.loc[
            (
                # Close price is above upper Bollinger Band (overbought)
                (dataframe['close'] > dataframe['bb_upperband']) &
                # RSI is overbought
                (dataframe['rsi'] > 70) &
                # Ensure volume is positive
                (dataframe['volume'] > 0)
            ),
            'enter_short'
        ] = 1

        return dataframe

    # Exit trend (not used, we rely on custom_exit)
    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe['exit_long'] = 0
        dataframe['exit_short'] = 0
        return dataframe

    # Custom exit to enforce 10-minute expiry (600 seconds)
    def custom_exit(self, pair: str, trade: 'Trade', current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs):
        # 10 minutes is exactly 600 seconds
        trade_duration = (current_time - trade.open_date_utc).total_seconds()
        if trade_duration >= 600:
            return "expiry_10m"
        return None
