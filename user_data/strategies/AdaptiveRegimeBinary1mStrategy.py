"""
AdaptiveRegimeBinary1mStrategy - 量化数据研究员版 (1分钟K线自适应策略)
========================================================================

专为 1-minute (1m) K线数据设计，执行 10-minute (10分钟) 二元期权到期结算。
核心优化要点（基于 13.0 万根 1m 历史 K 线数据）：
  1. 通过 ADX 与 EMA50 动量斜率（经 ATR 归一化）计算 1m 级别微观市场状态。
  2. 过滤掉高噪震荡区（Quiet Ranging / Volatile Ranging 不进行交易）。
  3. Bullish Trend (强上升微趋势):
     - 只做 Put (反向空): Z-Score > 2.2 且 RSI > 75
  4. Bearish Trend (强下降微趋势):
     - 只做 Call (反向多): Z-Score < -1.5 且 RSI < 25
     - 必须满足成交量高于中位数 (`vol_ratio >= 1.0`)
     - 必须满足 3 根连续阴线超跌力竭 (`consec_down_3 == 3`)
     - 直接买在下跌惯性阴线中 (`rsi_diff <= 0`)，避免阳线确认造成的入场价格偏高。
"""

import numpy as np
import talib
from datetime import datetime
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class AdaptiveRegimeBinary1mStrategy(IStrategy):

    INTERFACE_VERSION = 3
    can_short = True
    timeframe = '1m'
    max_open_trades = 10

    # 10分钟二元期权：不设置任何止损或移动止损，完全通过 custom_exit 到期平仓
    minimal_roi = {"0": 100.0}
    stoploss = -1.00
    trailing_stop = False

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        close = dataframe['close'].values
        high = dataframe['high'].values
        low = dataframe['low'].values
        volume = dataframe['volume'].values

        # ── 基础指标计算 ──
        dataframe['atr'] = talib.ATR(high, low, close, timeperiod=14)
        dataframe['adx'] = talib.ADX(high, low, close, timeperiod=14)
        dataframe['ema50'] = talib.EMA(close, timeperiod=50)
        dataframe['rsi'] = talib.RSI(close, timeperiod=14)

        # ── EMA50 动量斜率 (经 ATR 归一化) ──
        dataframe['ema50_slope'] = (dataframe['ema50'] - dataframe['ema50'].shift(5)) / dataframe['atr']

        # ── Z-Score 计算 ──
        mean20 = dataframe['close'].rolling(window=20).mean()
        std20 = dataframe['close'].rolling(window=20).std()
        dataframe['zscore'] = (dataframe['close'] - mean20) / std20

        # ── 辅助特征：量价与动量衰竭 ──
        vol_median = dataframe['volume'].rolling(window=20).median()
        dataframe['vol_ratio'] = dataframe['volume'] / vol_median.replace(0, np.nan)
        dataframe['vol_ratio'] = dataframe['vol_ratio'].fillna(0)

        is_down_candle = (dataframe['close'] < dataframe['open']).astype(int)
        dataframe['consec_down_3'] = is_down_candle.rolling(window=3).sum()
        dataframe['rsi_diff'] = dataframe['rsi'] - dataframe['rsi'].shift(1)

        # ── 市场状态定义 (Regime) ──
        conditions = [
            (dataframe['adx'] < 20),
            (dataframe['adx'] >= 20) & (dataframe['ema50_slope'].abs() < 0.2),
            (dataframe['ema50_slope'] >= 0.2),
            (dataframe['ema50_slope'] <= -0.2)
        ]
        choices = ['Quiet Ranging', 'Volatile Ranging', 'Bullish Trend', 'Bearish Trend']
        dataframe['regime'] = np.select(conditions, choices, default='Quiet Ranging')

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe['enter_long'] = 0
        dataframe['enter_short'] = 0

        # 1. Bullish Trend -> 只做 Put (Z > 2.2 且 RSI > 75)
        idx_bull = dataframe['regime'] == 'Bullish Trend'
        dataframe.loc[
            idx_bull & 
            (dataframe['zscore'] > 2.2) & 
            (dataframe['rsi'] > 75) & 
            (dataframe['volume'] > 0), 
            'enter_short'
        ] = 1

        # 2. Bearish Trend -> 只做 Call (Z < -1.5 且 RSI < 25 + 严格过滤条件)
        idx_bear = dataframe['regime'] == 'Bearish Trend'
        dataframe.loc[
            idx_bear & 
            (dataframe['zscore'] < -1.5) & 
            (dataframe['rsi'] < 25) & 
            (dataframe['vol_ratio'] >= 1.0) &
            (dataframe['consec_down_3'] == 3) &
            (dataframe['rsi_diff'] <= 0) &
            (dataframe['volume'] > 0), 
            'enter_long'
        ] = 1

        # 3. Quiet Ranging / Volatile Ranging -> 保持 0，不参与交易

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe['exit_long'] = 0
        dataframe['exit_short'] = 0
        return dataframe

    # 10分钟强制平仓
    def custom_exit(self, pair: str, trade: 'Trade', current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs):
        trade_duration = (current_time - trade.open_date_utc).total_seconds()
        if trade_duration >= 600:
            return "expiry_10m"
        return None
