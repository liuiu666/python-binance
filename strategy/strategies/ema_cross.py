"""
EMA 均线交叉策略
逻辑:
- EMA(9) 上穿 EMA(21) → 做多信号
- EMA(9) 下穿 EMA(21) → 做空信号
- ATR 止损: 入场价 ± 2×ATR
- RSI 过滤: 超买 (>70) 不做多, 超卖 (<30) 不做空
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from common.logger import get_logger
from strategy.base_strategy import BaseStrategy, Signal

logger = get_logger(__name__)


class EMACrossStrategy(BaseStrategy):
    """
    EMA 均线交叉策略

    参数:
    - ema_fast: 快线周期, 默认 9
    - ema_slow: 慢线周期, 默认 21
    - atr_multiplier: ATR 止损倍数, 默认 2.0
    - rsi_overbought: RSI 超买阈值, 默认 70
    - rsi_oversold: RSI 超卖阈值, 默认 30
    - quantity: 每次下单数量, 默认 0.001
    """

    def __init__(
        self,
        symbols: Optional[list] = None,
        ema_fast: int = 9,
        ema_slow: int = 21,
        atr_multiplier: float = 2.0,
        rsi_overbought: float = 70.0,
        rsi_oversold: float = 30.0,
        quantity: float = 0.001,
        leverage: int = 1,
    ) -> None:
        super().__init__(name="ema_cross", symbols=symbols)
        self._ema_fast = ema_fast
        self._ema_slow = ema_slow
        self._atr_multiplier = atr_multiplier
        self._rsi_overbought = rsi_overbought
        self._rsi_oversold = rsi_oversold
        self._quantity = quantity
        self._leverage = leverage

    async def on_kline(
        self, symbol: str, df: pd.DataFrame
    ) -> Optional[Signal]:
        """
        K 线闭合时的策略逻辑

        检测 EMA 交叉:
        - 前一根: ema_fast < ema_slow, 当前: ema_fast > ema_slow → 金叉做多
        - 前一根: ema_fast > ema_slow, 当前: ema_fast < ema_slow → 死叉做空
        """
        if not self._enabled:
            return None

        fast_col = f"ema_{self._ema_fast}"
        slow_col = f"ema_{self._ema_slow}"

        # 至少需要 2 根有指标的 K 线
        required_len = self._ema_slow + 2
        if len(df) < required_len:
            return None

        # 检查指标列是否存在
        if fast_col not in df.columns or slow_col not in df.columns:
            return None

        # 取最后两根 K 线
        prev = df.iloc[-2]
        curr = df.iloc[-1]

        prev_fast = prev.get(fast_col)
        prev_slow = prev.get(slow_col)
        curr_fast = curr.get(fast_col)
        curr_slow = curr.get(slow_col)

        # 检查 NaN
        if any(pd.isna(v) for v in [prev_fast, prev_slow, curr_fast, curr_slow]):
            return None

        close_price = float(curr["close_price"])

        # 获取 RSI 和 ATR
        rsi = curr.get("rsi", 50)
        atr = curr.get("atr", 0)
        if pd.isna(rsi):
            rsi = 50
        if pd.isna(atr) or atr <= 0:
            atr = close_price * 0.01  # 默认 1%

        # ---- 金叉: 快线上穿慢线 → 做多 ----
        if prev_fast <= prev_slow and curr_fast > curr_slow:
            # RSI 过滤: 超买区不做多
            if rsi > self._rsi_overbought:
                logger.debug(
                    "ema_cross.skip_overbought",
                    symbol=symbol,
                    rsi=f"{rsi:.1f}",
                )
                return None

            stop_loss = close_price - self._atr_multiplier * atr
            take_profit = close_price + self._atr_multiplier * atr * 1.5

            signal = self._create_signal(
                symbol=symbol,
                action="OPEN",
                side="BUY",
                quantity=self._quantity,
                price=close_price,
                stop_loss=round(stop_loss, 2),
                take_profit=round(take_profit, 2),
                reason=f"EMA{self._ema_fast}上穿EMA{self._ema_slow}, RSI={rsi:.1f}",
                leverage=self._leverage,
            )
            logger.info(
                "ema_cross.golden_cross",
                symbol=symbol,
                price=close_price,
                rsi=f"{rsi:.1f}",
                sl=stop_loss,
                tp=take_profit,
            )
            return signal

        # ---- 死叉: 快线下穿慢线 → 做空 ----
        if prev_fast >= prev_slow and curr_fast < curr_slow:
            # RSI 过滤: 超卖区不做空
            if rsi < self._rsi_oversold:
                logger.debug(
                    "ema_cross.skip_oversold",
                    symbol=symbol,
                    rsi=f"{rsi:.1f}",
                )
                return None

            stop_loss = close_price + self._atr_multiplier * atr
            take_profit = close_price - self._atr_multiplier * atr * 1.5

            signal = self._create_signal(
                symbol=symbol,
                action="OPEN",
                side="SELL",
                quantity=self._quantity,
                price=close_price,
                stop_loss=round(stop_loss, 2),
                take_profit=round(take_profit, 2),
                reason=f"EMA{self._ema_fast}下穿EMA{self._ema_slow}, RSI={rsi:.1f}",
                leverage=self._leverage,
            )
            logger.info(
                "ema_cross.death_cross",
                symbol=symbol,
                price=close_price,
                rsi=f"{rsi:.1f}",
                sl=stop_loss,
                tp=take_profit,
            )
            return signal

        return None
