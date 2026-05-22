import pandas as pd
import numpy as np
from datetime import datetime
from pandas import DataFrame
from freqtrade.strategy import IStrategy

class RawSequence2mOptimalStrategy(IStrategy):
    INTERFACE_VERSION = 3
    can_short = True
    timeframe = '1m'
    max_open_trades = 1

    minimal_roi = {"0": 100.0}
    stoploss = -1.00
    trailing_stop = False

    # Volume and Range multipliers for 60.84% win rate optimization
    vol_multiplier = 1.4
    range_multiplier = 1.4

    # Elite combos with individual win rate >= 58.5%
    call_combos = [
        '000100_1_1', '000000_1_1', '011010_0_1', '110100_1_1',
        '010110_0_1', '011000_1_1', '000011_1_1'
    ]
    
    put_combos = [
        '111011_1_1', '101001_0_1', '100101_0_1', '110101_1_0',
        '111001_1_0', '110001_1_1'
    ]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Resample to 2m
        df_2m = dataframe.set_index('date').resample('2min').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna().reset_index()
        
        # Shift date by +1m. The 2m bar [t,t+2m) and the 1m bar
        # [t+1m,t+2m) both close at the same wall-clock t+2m, so merging the
        # 2m signal onto the 1m candle at t+1m introduces no look-ahead.
        # Freqtrade then enters at the OPEN of the 1m candle at t+2m, which
        # equals the 2m bar close -- aligning backtest entry with research.
        df_2m['date'] = df_2m['date'] + pd.Timedelta(minutes=1)
        
        # Indicators on 2m
        df_2m['dir'] = (df_2m['close'] > df_2m['open']).astype(int)
        
        vol_median = df_2m['volume'].rolling(window=20).median()
        # High volume defined by multiplier (default 1.4)
        df_2m['vol_high'] = (df_2m['volume'] > (vol_median * self.vol_multiplier)).astype(int)
        
        full_range = df_2m['high'] - df_2m['low']
        range_mean = full_range.rolling(window=20).mean()
        # Large range defined by multiplier (default 1.4)
        df_2m['large_range'] = (full_range > (range_mean * self.range_multiplier)).astype(int)
        
        # Sequence of length 6
        df_2m['seq_str'] = ""
        for shift in reversed(range(6)):
            df_2m['seq_str'] = df_2m['seq_str'] + df_2m['dir'].shift(shift).fillna(0).astype(int).astype(str)
            
        df_2m['combo_str'] = df_2m['seq_str'] + "_" + df_2m['vol_high'].astype(str) + "_" + df_2m['large_range'].astype(str)
        
        # Merge back onto 1m timeline (signals only appear at even minutes)
        df_2m_sig = df_2m[['date', 'combo_str']].copy()
        dataframe = pd.merge(dataframe, df_2m_sig, on='date', how='left')
        dataframe['combo_str'] = dataframe['combo_str'].fillna("")
        
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe['enter_long'] = 0
        dataframe['enter_short'] = 0

        # Long (Call) entries
        dataframe.loc[
            (dataframe['combo_str'].isin(self.call_combos)) &
            (dataframe['volume'] > 0),
            'enter_long'
        ] = 1

        # Short (Put) entries
        dataframe.loc[
            (dataframe['combo_str'].isin(self.put_combos)) &
            (dataframe['volume'] > 0),
            'enter_short'
        ] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe['exit_long'] = 0
        dataframe['exit_short'] = 0
        return dataframe

    # 10 minutes expiry flat exit
    def custom_exit(self, pair: str, trade: 'Trade', current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs):
        trade_duration = (current_time - trade.open_date_utc).total_seconds()
        if trade_duration >= 600:  # 10 minutes = 600 seconds
            return "expiry_10m"
        return None
