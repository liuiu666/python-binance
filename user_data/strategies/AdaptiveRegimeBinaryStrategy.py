"""
AdaptiveRegimeBinaryStrategy (ARBS) - 量化数据研究员版 (Regime-Switching V2)
==========================================================================

基于对 505 天 BTC/USDT 5m 数据的多维度市场状态研究开发。
核心设计理念：
  1. 通过 ADX 与 EMA50 动量斜率（经 ATR 归一化）将市场划分为 4 种状态（Regimes）：
     - Quiet Ranging (震荡整理)
     - Volatile Ranging (宽幅震荡)
     - Bullish Trend (强上升趋势)
     - Bearish Trend (强下降趋势)
  2. 不同状态采用非对称的最优交易参数，避免逆势或噪声期交易：
     - Quiet Ranging: 均值回归（双向均可交易）
     - Bullish Trend: 只做 Put (捕获超买后的快速极值回调，不做 Call 避免高位接盘)
     - Bearish Trend: 只做 Call (捕获超卖后的短挤压快速反弹，不做 Put)
     - Volatile Ranging: 观望不交易（避免无序宽幅震荡下的双向止损）
"""

import numpy as np
import talib
from datetime import datetime
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class AdaptiveRegimeBinaryStrategy(IStrategy):

    INTERFACE_VERSION = 3
    can_short = True
    timeframe = '5m'
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
        # Quiet Ranging: adx < 20
        # Volatile Ranging: adx >= 20 且斜率绝对值 < 0.2
        # Bullish Trend: 斜率 >= 0.2
        # Bearish Trend: 斜率 <= -0.2
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

        # 1. Quiet Ranging -> 双向均值回归
        idx_range = dataframe['regime'] == 'Quiet Ranging'
        dataframe.loc[
            idx_range & 
            (dataframe['zscore'] < -1.8) & 
            (dataframe['rsi'] < 30) & 
            (dataframe['volume'] > 0), 
            'enter_long'
        ] = 1
        dataframe.loc[
            idx_range & 
            (dataframe['zscore'] > 1.8) & 
            (dataframe['rsi'] > 70) & 
            (dataframe['volume'] > 0), 
            'enter_short'
        ] = 1

        # 2. Bullish Trend -> 只做 Put (Short) 捕获拉升后的超买回调
        idx_bull = dataframe['regime'] == 'Bullish Trend'
        dataframe.loc[
            idx_bull & 
            (dataframe['zscore'] > 1.8) & 
            (dataframe['rsi'] > 70) & 
            (dataframe['volume'] > 0), 
            'enter_short'
        ] = 1

        # 3. Bearish Trend -> 只做 Call (Long) 捕获超跌后的反弹 (带有优化过滤条件)
        idx_bear = dataframe['regime'] == 'Bearish Trend'
        dataframe.loc[
            idx_bear & 
            (dataframe['zscore'] < -1.6) & 
            (dataframe['rsi'] < 32) & 
            (dataframe['vol_ratio'] >= 1.0) &
            (dataframe['consec_down_3'] == 3) &
            (dataframe['rsi_diff'] <= 0) &
            (dataframe['volume'] > 0), 
            'enter_long'
        ] = 1

        # 4. Volatile Ranging -> 保持 0，不参与交易

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
