"""
成交量异常策略 — 基于泊松检测的信号增强和交易决策

逻辑:
- 滚动监测 K 线分钟成交笔数 (trades_count)
- 基于泊松分布模型计算当前成交笔数的偏离程度 (p-value, z-score)
- 当发生异常放量时，根据价格涨跌方向产生做多或做空信号，并附加基于 ATR 的止损/止盈
"""

from __future__ import annotations

from typing import Dict, List, Optional

import pandas as pd

from common.logger import get_logger
from strategy.base_strategy import BaseStrategy, Signal
from strategy.poisson_detector import PoissonDetector, AnomalyLevel

logger = get_logger(__name__)


class VolumeAnomalyStrategy(BaseStrategy):
    """
    成交量异常策略 (基于泊松模型)

    参数:
    - window_size: 滚动窗口大小 (默认 60 根 K 线)
    - ema_alpha: 指数加权系数 (默认 0.05)
    - overdispersion_factor: 过散射因子 (默认 1.2, 稍微容忍波动的离散程度)
    - atr_multiplier: ATR 止损倍数 (默认 2.0)
    - price_change_threshold: 产生信号的最小价格变动比例 (默认 0.08, 0.08%)
    - quantity: 基础下单数量, 默认 0.001
    - leverage: 杠杆倍数, 默认 1
    """

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        window_size: int = 60,
        ema_alpha: float = 0.05,
        overdispersion_factor: float = 1.2,
        atr_multiplier: float = 2.0,
        price_change_threshold: float = 0.08,
        quantity: float = 0.001,
        leverage: int = 1,
    ) -> None:
        super().__init__(name="volume_anomaly", symbols=symbols)
        self._window_size = window_size
        self._ema_alpha = ema_alpha
        self._overdispersion = overdispersion_factor
        self._atr_multiplier = atr_multiplier
        self._price_change_threshold = price_change_threshold
        self._quantity = quantity
        self._leverage = leverage

        # 每一个交易对维护一个独立的 PoissonDetector
        self._detectors: Dict[str, PoissonDetector] = {}

    def _apply_ai_params(self) -> None:
        """应用 AI 调参建议 (仅覆盖白名单中的参数)"""
        params = self.get_ai_params()
        if not params:
            return
        # 参数白名单
        param_map = {
            "window_size": ("_window_size", int),
            "ema_alpha": ("_ema_alpha", float),
            "overdispersion_factor": ("_overdispersion", float),
            "atr_multiplier": ("_atr_multiplier", float),
            "price_change_threshold": ("_price_change_threshold", float),
            "quantity": ("_quantity", float),
            "leverage": ("_leverage", int),
        }
        for key, (attr, cast) in param_map.items():
            if key in params:
                try:
                    setattr(self, attr, cast(params[key]))
                    # 如果重新设置了窗口参数，重置检测器以刷新
                    if key in ["window_size", "ema_alpha", "overdispersion_factor"]:
                        self._detectors.clear()
                except (ValueError, TypeError):
                    pass

    async def on_kline(
        self, symbol: str, df: pd.DataFrame
    ) -> Optional[Signal]:
        """K 线闭合回调，检测成交量异常"""
        if not self._enabled:
            return None

        # 应用 AI 参数建议 (如果有)
        self._apply_ai_params()

        # 确保数据充足
        if len(df) < 5:
            return None

        # 检查并初始化当前 symbol 的检测器
        if symbol not in self._detectors:
            self._detectors[symbol] = PoissonDetector(
                window_size=self._window_size,
                ema_alpha=self._ema_alpha,
                overdispersion_factor=self._overdispersion,
            )

        detector = self._detectors[symbol]

        # 取最后一根 K 线和前一根 K 线
        curr = df.iloc[-1]
        prev = df.iloc[-2]

        trade_count = int(curr.get("trades_count", 0))
        if pd.isna(trade_count):
            return None

        # 更新泊松检测器并获取结果
        result = detector.update(trade_count)

        # 只在"明显异常"或"极端异常"时考虑下单信号
        if result.anomaly_level not in (AnomalyLevel.ANOMALY, AnomalyLevel.EXTREME):
            return None

        # 异常高量方向才触发入场
        if result.direction != "HIGH":
            return None

        close_price = float(curr["close_price"])
        prev_close = float(prev["close_price"])
        price_change_pct = (close_price - prev_close) / prev_close * 100

        # 获取 ATR 进行止损和止盈设置
        atr = curr.get("atr", 0)
        if pd.isna(atr) or atr <= 0:
            atr = close_price * 0.01  # 默认使用 1% 价格作为 ATR 兜底

        # 准备动态调整成交量 (可基于异常度 z-score 放大下单仓位，做信号增强)
        # z-score 越大，偏离越大，可以稍微放大下单数量 (上限为基础数量的 3 倍)
        quantity_multiplier = min(3.0, 1.0 + max(0.0, result.z_score - 3.0) * 0.2)
        adjusted_qty = self._quantity * quantity_multiplier

        # ---- 价格涨 + 异常放量 = 做多 ----
        if price_change_pct >= self._price_change_threshold:
            stop_loss = close_price - self._atr_multiplier * atr
            take_profit = close_price + self._atr_multiplier * atr * 1.5

            signal = self._create_signal(
                symbol=symbol,
                action="OPEN",
                side="BUY",
                quantity=round(adjusted_qty, 4),
                price=close_price,
                stop_loss=round(stop_loss, 2),
                take_profit=round(take_profit, 2),
                reason=f"Poisson成交笔数异常 z={result.z_score:.1f}, p={result.p_value:.6f}, 价格变动={price_change_pct:.2f}%",
                leverage=self._leverage,
            )
            logger.info(
                "volume_anomaly.buy_signal",
                symbol=symbol,
                price=close_price,
                trade_count=trade_count,
                z_score=result.z_score,
                qty=adjusted_qty,
            )
            return signal

        # ---- 价格跌 + 异常放量 = 做空 ----
        elif price_change_pct <= -self._price_change_threshold:
            stop_loss = close_price + self._atr_multiplier * atr
            take_profit = close_price - self._atr_multiplier * atr * 1.5

            signal = self._create_signal(
                symbol=symbol,
                action="OPEN",
                side="SELL",
                quantity=round(adjusted_qty, 4),
                price=close_price,
                stop_loss=round(stop_loss, 2),
                take_profit=round(take_profit, 2),
                reason=f"Poisson成交笔数异常 z={result.z_score:.1f}, p={result.p_value:.6f}, 价格变动={price_change_pct:.2f}%",
                leverage=self._leverage,
            )
            logger.info(
                "volume_anomaly.sell_signal",
                symbol=symbol,
                price=close_price,
                trade_count=trade_count,
                z_score=result.z_score,
                qty=adjusted_qty,
            )
            return signal

        return None
