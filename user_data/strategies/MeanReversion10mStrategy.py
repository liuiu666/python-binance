"""
MeanReversion10mStrategy - BTC USDT-M perpetual, 10-minute binary-option style
strategy based on validated A3 signal:

  - ema120_dev (close - EMA(120)) / ATR(120)  in extreme deciles (~ +/- 3.4)
  - vol_z40 > 1                               (anomalous volume burst)
  - rv_z within (-1, +1)                      (avoid chaotic / dead regimes)

Validation (1y BTCUSDT futures, expanding 24-fold walk-forward):
  Trades 5,513 / WR 57.97% / Wilson LB 56.66% / PnL +1199U on 5U stake / MDD 145U / Calmar 8.27

Entries are flat 5U-stake style and exit at 10-minute expiry.

Backtest:
    .venv\\Scripts\\freqtrade backtesting -c user_data/config_backtest.json \
        -s MeanReversion10mStrategy -i 1m
"""
from datetime import datetime
import numpy as np
import pandas as pd
from pandas import DataFrame

from freqtrade.strategy import IStrategy


class MeanReversion10mStrategy(IStrategy):
    INTERFACE_VERSION = 3
    can_short = True
    timeframe = '1m'

    # One open trade at a time keeps the simulation aligned with the
    # 10-bar lockout used in research; raise this only if you bias-test.
    max_open_trades = 1

    # Binary-option style: time-based exit at 10 minutes.
    # minimal_roi {minutes: profit_threshold} -- the entry "10": -1.0 means
    # "after 10 minutes, exit at any P&L >= -100%" -- effectively a hard
    # 10-minute timer matching the binary-option expiry. custom_exit() is
    # kept as a redundant safety net.
    minimal_roi = {"10": -1.0}
    stoploss = -1.00
    trailing_stop = False
    process_only_new_candles = True
    use_exit_signal = False
    exit_profit_only = False

    # Number of 1m candles required before signals are reliable
    # (max of 120 EMA + 60*24 rv_z baseline)
    startup_candle_count = 60 * 24 + 120

    # Strategy parameters - mirror the research script
    EMA_SPAN = 120
    ATR_WIN = 120
    VOL_Z_WIN = 40
    RV_WIN = 60
    RV_BASELINE_WIN = 60 * 24
    EMA_DEV_QUANTILE_WIN = 60 * 24 * 14  # rolling 14 days for the 10/90 cutoffs
    EMA_DEV_LOW_Q = 0.10
    EMA_DEV_HIGH_Q = 0.90
    VOL_Z_THRESHOLD = 1.0
    RV_Z_BAND = 1.0
    EXPIRY_SECS = 10 * 60

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe

        rng = df['high'] - df['low']
        atr120 = rng.rolling(self.ATR_WIN).mean()

        ema120 = df['close'].ewm(span=self.EMA_SPAN, adjust=False).mean()
        df['ema120_dev'] = (df['close'] - ema120) / atr120

        vmed40 = df['volume'].rolling(self.VOL_Z_WIN).median()
        vstd40 = df['volume'].rolling(self.VOL_Z_WIN).std()
        df['vol_z40'] = (df['volume'] - vmed40) / vstd40

        logret1 = np.log(df['close']).diff()
        rv60 = logret1.rolling(self.RV_WIN).std()
        rv60_mean = rv60.rolling(self.RV_BASELINE_WIN).mean()
        rv60_std = rv60.rolling(self.RV_BASELINE_WIN).std()
        df['rv_z'] = (rv60 - rv60_mean) / rv60_std

        # Rolling quantile cutoffs for ema120_dev (14-day window).
        # rolling.quantile is O(N*W); 14d on 1m = 20160 bars. Acceptable speed
        # for 1y backtest on a single pair but heavy. Falls back to expanding
        # quantile during the warmup period.
        ev = df['ema120_dev']
        df['ev_q_lo'] = ev.rolling(self.EMA_DEV_QUANTILE_WIN, min_periods=2 * 60 * 24)\
                         .quantile(self.EMA_DEV_LOW_Q)
        df['ev_q_hi'] = ev.rolling(self.EMA_DEV_QUANTILE_WIN, min_periods=2 * 60 * 24)\
                         .quantile(self.EMA_DEV_HIGH_Q)

        return df

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        df['enter_long'] = 0
        df['enter_short'] = 0

        base_filter = (
            (df['vol_z40'] > self.VOL_Z_THRESHOLD)
            & (df['rv_z'] > -self.RV_Z_BAND)
            & (df['rv_z'] < self.RV_Z_BAND)
            & (df['volume'] > 0)
            & df['ev_q_lo'].notna()
            & df['ev_q_hi'].notna()
        )

        # CALL: price stretched far below EMA(120), expect mean reversion up.
        df.loc[base_filter & (df['ema120_dev'] <= df['ev_q_lo']), 'enter_long'] = 1
        # PUT: price stretched far above EMA(120), expect mean reversion down.
        df.loc[base_filter & (df['ema120_dev'] >= df['ev_q_hi']), 'enter_short'] = 1

        return df

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe['exit_long'] = 0
        dataframe['exit_short'] = 0
        return dataframe

    def custom_exit(self, pair: str, trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs):
        if (current_time - trade.open_date_utc).total_seconds() >= self.EXPIRY_SECS:
            return "expiry_10m"
        return None
