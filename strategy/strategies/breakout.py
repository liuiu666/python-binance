"""
突破策略
逻辑:
- 价格突破最近 N 根 K 线最高点 → 做多
- 价格跌破最近 N 根 K 线最低点 → 做空
- ATR 止损: 入场价 ± 1.5×ATR
- 成交量确认: 突破 K 线成交量 > 前 N 根均量 × 1.5
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from common.logger import get_logger
from strategy.base_strategy import BaseStrategy, Signal

logger = get_logger(__name__)


class BreakoutStrategy(BaseStrategy):
    """
    突破策略

    参数:
    - lookback: 回望周期 (根数), 默认 20
    - volume_ratio: 成交量放大倍数确认, 默认 1.5
    - atr_multiplier: ATR 止损倍数, 默认 1.5
    - quantity: 每次下单数量
    """

    def __init__(
        self,
        symbols: Optional[list] = None,
        lookback: int = 20,
        volume_ratio: float = 1.5,
        atr_multiplier: float = 1.5,
        quantity: float = 0.001,
        leverage: int = 1,
    ) -> None:
        super().__init__(name="breakout", symbols=symbols)
        self._lookback = lookback
        self._volume_ratio = volume_ratio
        self._atr_multiplier = atr_multiplier
        self._quantity = quantity
        self._leverage = leverage

    def _apply_ai_params(self) -> None:
        """应用 AI 调参建议 (仅覆盖白名单中的参数)"""
        params = self.get_ai_params()
        if not params:
            return
        param_map = {
            "lookback": ("_lookback", int),
            "volume_ratio": ("_volume_ratio", float),
            "atr_multiplier": ("_atr_multiplier", float),
            "quantity": ("_quantity", float),
            "leverage": ("_leverage", int),
        }
        for key, (attr, cast) in param_map.items():
            if key in params:
                try:
                    setattr(self, attr, cast(params[key]))
                except (ValueError, TypeError):
                    pass

    async def on_kline(
        self, symbol: str, df: pd.DataFrame
    ) -> Optional[Signal]:
        """
        K 线闭合时的策略逻辑

        检测突破:
        - 当前 close > 前 lookback 根 high 的最大值 → 向上突破做多
        - 当前 close < 前 lookback 根 low 的最小值 → 向下跌破做空
        """
        if not self._enabled:
            return None

        # 应用 AI 参数建议 (如果有)
        self._apply_ai_params()

        # 至少需要 lookback + 1 根 K 线
        if len(df) < self._lookback + 1:
            return None

        # 取回望窗口 (不包含当前 K 线)
        window = df.iloc[-(self._lookback + 1):-1]
        current = df.iloc[-1]

        close_price = float(current["close_price"])
        current_volume = float(current["volume"])

        # 回望窗口的最高价和最低价
        highest = float(window["high_price"].max())
        lowest = float(window["low_price"].min())

        # 回望窗口的平均成交量
        avg_volume = float(window["volume"].mean())
        if avg_volume <= 0:
            avg_volume = 1.0

        # 获取 ATR
        atr = current.get("atr", 0)
        if pd.isna(atr) or atr <= 0:
            atr = close_price * 0.01

        # ---- 向上突破 ----
        if close_price > highest:
            # 成交量确认
            volume_ok = current_volume > avg_volume * self._volume_ratio
            if not volume_ok:
                logger.debug(
                    "breakout.volume_not_confirmed",
                    symbol=symbol,
                    current_vol=current_volume,
                    avg_vol=avg_volume,
                    ratio=current_volume / avg_volume,
                )
                return None

            stop_loss = close_price - self._atr_multiplier * atr
            take_profit = close_price + self._atr_multiplier * atr * 2.0

            signal = self._create_signal(
                symbol=symbol,
                action="OPEN",
                side="BUY",
                quantity=self._quantity,
                price=close_price,
                stop_loss=round(stop_loss, 2),
                take_profit=round(take_profit, 2),
                reason=f"突破{self._lookback}周期高点 {highest:.2f}, 量比={current_volume / avg_volume:.1f}",
                leverage=self._leverage,
            )
            logger.info(
                "breakout.breakout_up",
                symbol=symbol,
                price=close_price,
                resistance=highest,
                volume_ratio=f"{current_volume / avg_volume:.1f}",
            )
            return signal

        # ---- 向下跌破 ----
        if close_price < lowest:
            # 成交量确认
            volume_ok = current_volume > avg_volume * self._volume_ratio
            if not volume_ok:
                logger.debug(
                    "breakout.volume_not_confirmed",
                    symbol=symbol,
                    current_vol=current_volume,
                    avg_vol=avg_volume,
                )
                return None

            stop_loss = close_price + self._atr_multiplier * atr
            take_profit = close_price - self._atr_multiplier * atr * 2.0

            signal = self._create_signal(
                symbol=symbol,
                action="OPEN",
                side="SELL",
                quantity=self._quantity,
                price=close_price,
                stop_loss=round(stop_loss, 2),
                take_profit=round(take_profit, 2),
                reason=f"跌破{self._lookback}周期低点 {lowest:.2f}, 量比={current_volume / avg_volume:.1f}",
                leverage=self._leverage,
            )
            logger.info(
                "breakout.breakout_down",
                symbol=symbol,
                price=close_price,
                support=lowest,
                volume_ratio=f"{current_volume / avg_volume:.1f}",
            )
            return signal

        return None
