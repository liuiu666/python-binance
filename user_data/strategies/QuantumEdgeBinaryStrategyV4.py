"""
QuantumEdgeBinaryStrategy V4 — 带时间过滤的非对称策略
=======================================================

基于 V3 诊断报告的核心发现：
  * UTC 02:00, 06:00, 10:00 时段，整体胜率 < 45%，应过滤
  * 星期四 (Thursday) 胜率仅 40%，应降低仓位或过滤
  * Call 在 UTC 06:00 时段胜率仅 20%，在 UTC 10:00 仅 0%
  * Put 在 V3 所有版本中稳定保持 61.22% — 核心收益来源

V4 新增指标：
  6. Stochastic RSI（随机RSI）— 比普通RSI更灵敏的超买超卖检测
  7. 成交量价格趋势 (VPT 加速度) — 量价关系的二阶导数

V4 改进：
  - 加入 UTC 时段过滤（跳过 02:00, 06:00, 10:00）
  - 加入星期过滤（星期四减少 Call 交易）
  - Call 增加 Stochastic RSI 确认条件
  - Put 保持 V1/V3 已验证的高胜率框架
"""

import numpy as np
from datetime import datetime
from pandas import DataFrame
import talib.abstract as ta
from freqtrade.strategy import IStrategy


class QuantumEdgeBinaryStrategyV4(IStrategy):

    INTERFACE_VERSION = 3
    can_short = True
    timeframe = '5m'
    max_open_trades = 10

    minimal_roi = {"0": 100.0}
    stoploss = -1.00
    trailing_stop = False

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  自研指标（继承 V3）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @staticmethod
    def price_zscore(close, period=20):
        mean = close.rolling(window=period).mean()
        std = close.rolling(window=period).std()
        return ((close - mean) / std.replace(0, np.nan)).fillna(0)

    @staticmethod
    def exhaustion_index(open_price, high, low, close, lookback=5):
        is_bullish = (close > open_price).astype(float)
        bull_count = is_bullish.rolling(window=lookback).sum()
        total_range = (high - low).replace(0, np.nan)
        upper_wick_ratio = (high - close.clip(lower=open_price)) / total_range
        bull_exhaust = (bull_count / lookback) * upper_wick_ratio.fillna(0)
        return bull_exhaust.fillna(0)

    @staticmethod
    def volume_surge_ratio(volume, period=20):
        vol_mean = volume.rolling(window=period).mean()
        return (volume / vol_mean.replace(0, np.nan)).fillna(0)

    @staticmethod
    def multi_rsi(dataframe):
        r7  = ta.RSI(dataframe, timeperiod=7)
        r14 = ta.RSI(dataframe, timeperiod=14)
        r21 = ta.RSI(dataframe, timeperiod=21)
        return (r7 + r14 + r21) / 3.0, r7, r14, r21

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  自研指标 6: Stochastic RSI（随机 RSI）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  将 RSI 的值再做一次随机指标的平滑处理。
    #  StochRSI < 20：RSI 处于其自身的超低区间（极度超卖）
    #  StochRSI > 80：RSI 处于其自身的超高区间（极度超买）
    @staticmethod
    def stoch_rsi(rsi, period=14, smooth_k=3, smooth_d=3):
        rsi_min = rsi.rolling(window=period).min()
        rsi_max = rsi.rolling(window=period).max()
        rsi_range = (rsi_max - rsi_min).replace(0, np.nan)
        stoch = ((rsi - rsi_min) / rsi_range * 100).fillna(50)
        k = stoch.rolling(window=smooth_k).mean()
        d = k.rolling(window=smooth_d).mean()
        return k, d

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  自研指标 7: 成交量价格趋势加速度 (VPT Accel)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  VPT = 累计 (成交量 × 价格变化率)
    #  VPT 的变化速率（加速度）在转折点处变号：
    #  VPT 加速度 > 0：买方势能在增强（做多有利）
    #  VPT 加速度 < 0：卖方势能在增强（做空有利）
    @staticmethod
    def vpt_acceleration(close, volume, smooth=5):
        price_change_pct = close.pct_change()
        vpt = (volume * price_change_pct).cumsum()
        vpt_smooth = vpt.rolling(window=smooth).mean()
        vpt_accel = vpt_smooth.diff(3)  # 3 周期变化率
        return vpt_accel.fillna(0)

    # ═══════════════════════════════════════════════
    #  指标主函数
    # ═══════════════════════════════════════════════
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:

        dataframe['zscore'] = self.price_zscore(dataframe['close'], period=20)
        dataframe['atr']    = ta.ATR(dataframe, timeperiod=14)
        dataframe['bull_exhaust'] = self.exhaustion_index(
            dataframe['open'], dataframe['high'], dataframe['low'], dataframe['close'], lookback=5
        )
        dataframe['vsr'] = self.volume_surge_ratio(dataframe['volume'], period=20)

        consensus, r7, r14, r21 = self.multi_rsi(dataframe)
        dataframe['rsi_consensus'] = consensus
        dataframe['rsi_7']  = r7
        dataframe['rsi_7_prev'] = r7.shift(1)

        # Stochastic RSI
        stoch_k, stoch_d = self.stoch_rsi(r14, period=14)
        dataframe['stoch_k'] = stoch_k
        dataframe['stoch_d'] = stoch_d

        # VPT 加速度
        dataframe['vpt_accel'] = self.vpt_acceleration(dataframe['close'], dataframe['volume'])

        # K 线买卖压力
        hl = (dataframe['high'] - dataframe['low']).replace(0, np.nan)
        dataframe['cpi'] = ((dataframe['close'] - dataframe['low']) / hl).fillna(0.5)

        # 时间特征（UTC）
        dataframe['hour'] = dataframe['date'].dt.hour
        dataframe['weekday'] = dataframe['date'].dt.dayofweek  # 0=Mon, 3=Thu

        return dataframe

    # ═══════════════════════════════════════════════
    #  信号生成 V4（带时段过滤）
    # ═══════════════════════════════════════════════
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:

        # ── 全局时段过滤：跳过诊断确认的最差时段 ──
        # UTC 02:00 胜率 0.0%  / UTC 06:00 胜率 16.7% / UTC 10:00 胜率 16.7%
        bad_hours  = dataframe['hour'].isin([2, 6, 10])
        is_thursday = dataframe['weekday'] == 3  # 星期四整体胜率 40%

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        #  看涨 Call — V4 增强版（加入 Stochastic RSI + VPT 加速度）
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        #  ① Z-Score < -1.8        → 价格显著偏低
        #  ② RSI共识 < 30          → 多周期超卖
        #  ③ RSI-7 > RSI-7前值     → 最灵敏RSI已拐头
        #  ④ StochRSI-K < 25       → RSI本身也在极低区间（双重超卖确认）
        #  ⑤ VPT加速度 > 0         → 量价关系显示买方势能开始积累
        #  ⑥ CPI > 0.50            → K线收在中部以上
        #  ⑦ ~bad_hours            → 非最差时段
        #  ⑧ ~is_thursday          → 非星期四
        dataframe.loc[
            (
                (dataframe['zscore'] < -1.8) &
                (dataframe['rsi_consensus'] < 30) &
                (dataframe['rsi_7'] > dataframe['rsi_7_prev']) &
                (dataframe['stoch_k'] < 25) &
                (dataframe['vpt_accel'] > 0) &
                (dataframe['cpi'] > 0.50) &
                (~bad_hours) &
                (~is_thursday) &
                (dataframe['volume'] > 0)
            ),
            'enter_long'
        ] = 1

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        #  看跌 Put — V3 已验证 61.22% 框架 + 时段过滤
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        #  ① Z-Score > +1.8        → 价格显著偏高
        #  ② RSI共识 > 72          → 多周期超买
        #  ③ 上涨衰竭 > 0.3        → 连续阳线 + 上影线
        #  ④ VSR > 1.2             → 放量（最后冲刺特征）
        #  ⑤ CPI < 0.6             → 收盘未在最强位
        #  ⑥ StochRSI-K > 75       → RSI本身也在极高区间
        #  ⑦ ~bad_hours            → 非最差时段
        dataframe.loc[
            (
                (dataframe['zscore'] > 1.8) &
                (dataframe['rsi_consensus'] > 72) &
                (dataframe['bull_exhaust'] > 0.3) &
                (dataframe['vsr'] > 1.2) &
                (dataframe['cpi'] < 0.6) &
                (dataframe['stoch_k'] > 75) &
                (~bad_hours) &
                (dataframe['volume'] > 0)
            ),
            'enter_short'
        ] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe['exit_long'] = 0
        dataframe['exit_short'] = 0
        return dataframe

    def custom_exit(self, pair: str, trade: 'Trade', current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs):
        if (current_time - trade.open_date_utc).total_seconds() >= 600:
            return "expiry_10m"
        return None
