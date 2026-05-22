"""
QuantumEdgeBinaryStrategy (QEBS) — 量子边际二元期权策略 V3
==========================================================

自主研发的复合指标系统，专为 10 分钟二元期权设计。

迭代历史：
    V1: 5因子共振（Z-Score/RSI/衰竭/量能/CPI），5m胜率 56.99%
        - Call 52.27% 偏低，Put 61.22% 很强
    V2: 加入反弹/回落确认 → 样本量暴跌至54笔，Call更差
    V3 (当前): 保留 V1 强势的 Put 框架，重设计 Call 逻辑
        - Call 改用"深度超卖 + CPI 强买压 + RSI最短周期先行反转"
        - 移除 bear_exhaust（对Call无贡献），改用 RSI-7 拐点检测
        - Put 保持 V1 参数（已验证 61.22% 胜率）

五大自研指标体系：
    1. Z-Score 偏离度（价格相对于自身均值的标准化距离）
    2. K线衰竭指数 (Exhaustion Index)：检测上涨动力耗竭
    3. 成交量涌入检测 (Volume Surge)：识别异常放量的转折K线
    4. 多周期 RSI 一致性 (Multi-Period RSI Consensus)
    5. K线买卖压力指数 (Candle Pressure Index)
"""

import numpy as np
from datetime import datetime
from pandas import DataFrame
import talib.abstract as ta
from freqtrade.strategy import IStrategy


class QuantumEdgeBinaryStrategy(IStrategy):

    INTERFACE_VERSION = 3
    can_short = True
    timeframe = '5m'
    max_open_trades = 10

    minimal_roi = {"0": 100.0}
    stoploss = -1.00
    trailing_stop = False

    # ──────────────────────────────────────────────
    #  自研指标 1: Z-Score 偏离度
    # ──────────────────────────────────────────────
    @staticmethod
    def price_zscore(close, period=20):
        mean = close.rolling(window=period).mean()
        std = close.rolling(window=period).std()
        z = (close - mean) / std.replace(0, np.nan)
        return z.fillna(0)

    # ──────────────────────────────────────────────
    #  自研指标 2: K线衰竭指数 (Exhaustion Index)
    # ──────────────────────────────────────────────
    @staticmethod
    def exhaustion_index(open_price, high, low, close, lookback=5):
        is_bullish = (close > open_price).astype(float)
        bull_count = is_bullish.rolling(window=lookback).sum()
        is_bearish = (close < open_price).astype(float)
        bear_count = is_bearish.rolling(window=lookback).sum()
        total_range = (high - low).replace(0, np.nan)
        upper_wick_ratio = (high - close.clip(lower=open_price)) / total_range
        lower_wick_ratio = (close.clip(upper=open_price) - low) / total_range
        bull_exhaust = (bull_count / lookback) * upper_wick_ratio.fillna(0)
        bear_exhaust = (bear_count / lookback) * lower_wick_ratio.fillna(0)
        return bull_exhaust.fillna(0), bear_exhaust.fillna(0)

    # ──────────────────────────────────────────────
    #  自研指标 3: 成交量涌入比 (Volume Surge Ratio)
    # ──────────────────────────────────────────────
    @staticmethod
    def volume_surge_ratio(volume, period=20):
        vol_mean = volume.rolling(window=period).mean()
        vsr = volume / vol_mean.replace(0, np.nan)
        return vsr.fillna(0)

    # ──────────────────────────────────────────────
    #  自研指标 4: 多周期 RSI 共振分
    # ──────────────────────────────────────────────
    @staticmethod
    def multi_rsi_consensus(dataframe):
        rsi_7 = ta.RSI(dataframe, timeperiod=7)
        rsi_14 = ta.RSI(dataframe, timeperiod=14)
        rsi_21 = ta.RSI(dataframe, timeperiod=21)
        consensus = (rsi_7 + rsi_14 + rsi_21) / 3.0
        return consensus, rsi_7, rsi_14, rsi_21

    # ═══════════════════════════════════════════════
    #  指标计算主函数
    # ═══════════════════════════════════════════════
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:

        # ── Z-Score ──
        dataframe['zscore'] = self.price_zscore(dataframe['close'], period=20)

        # ── ATR ──
        dataframe['atr'] = ta.ATR(dataframe, timeperiod=14)

        # ── 衰竭指数 ──
        bull_ex, bear_ex = self.exhaustion_index(
            dataframe['open'], dataframe['high'], dataframe['low'], dataframe['close'], lookback=5
        )
        dataframe['bull_exhaust'] = bull_ex
        dataframe['bear_exhaust'] = bear_ex

        # ── 成交量涌入 ──
        dataframe['vsr'] = self.volume_surge_ratio(dataframe['volume'], period=20)

        # ── 多周期 RSI ──
        consensus, rsi7, rsi14, rsi21 = self.multi_rsi_consensus(dataframe)
        dataframe['rsi_consensus'] = consensus
        dataframe['rsi_7'] = rsi7
        dataframe['rsi_14'] = rsi14
        dataframe['rsi_21'] = rsi21

        # ── RSI-7 前值（用于检测拐点）──
        dataframe['rsi_7_prev'] = rsi7.shift(1)

        # ── K 线买卖压力 (CPI) ──
        hl_range = (dataframe['high'] - dataframe['low']).replace(0, np.nan)
        dataframe['cpi'] = ((dataframe['close'] - dataframe['low']) / hl_range).fillna(0.5)

        return dataframe

    # ═══════════════════════════════════════════════
    #  信号生成（多因子共振逻辑 V3）
    # ═══════════════════════════════════════════════
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        #  看涨 Call 信号 — V3 重设计
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        #  核心理念：在深度超卖区域，等待最灵敏的指标 (RSI-7)
        #  率先出现拐点（从前一根的更低值回升），同时要求K线
        #  本身展示强烈的买方压力 (CPI > 0.55)。
        #
        #  ① Z-Score < -1.8    → 价格显著低于均值
        #  ② RSI共识 < 30      → 多周期均处于超卖
        #  ③ RSI-7 > RSI-7前值  → 最快RSI已拐头向上（领先反转信号）
        #  ④ CPI > 0.55        → K线收在上半部（强买压确认）
        #  ⑤ 成交量 > 均值     → VSR > 1.0
        dataframe.loc[
            (
                (dataframe['zscore'] < -1.8) &
                (dataframe['rsi_consensus'] < 30) &
                (dataframe['rsi_7'] > dataframe['rsi_7_prev']) &
                (dataframe['cpi'] > 0.55) &
                (dataframe['vsr'] > 1.0) &
                (dataframe['volume'] > 0)
            ),
            'enter_long'
        ] = 1

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        #  看跌 Put 信号 — 沿用 V1 已验证的高胜率框架
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        #  ① Z-Score > +1.8    → 价格显著高于均值
        #  ② RSI共识 > 72      → 深度超买
        #  ③ 上涨衰竭 > 0.3    → 连续阳线 + 上影线（买方衰竭）
        #  ④ 成交量涌入 > 1.2  → 放量（可能是最后的冲刺）
        #  ⑤ CPI < 0.6         → 收盘未在最强位
        dataframe.loc[
            (
                (dataframe['zscore'] > 1.8) &
                (dataframe['rsi_consensus'] > 72) &
                (dataframe['bull_exhaust'] > 0.3) &
                (dataframe['vsr'] > 1.2) &
                (dataframe['cpi'] < 0.6) &
                (dataframe['volume'] > 0)
            ),
            'enter_short'
        ] = 1

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
