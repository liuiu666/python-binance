"""
策略基类 — 所有策略必须继承此类
定义统一的信号格式和接口规范
"""

from __future__ import annotations

import uuid
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

import pandas as pd

from common.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Signal:
    """
    标准化交易信号
    所有策略输出的信号必须符合此格式
    """
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    symbol: str = ""
    action: str = "OPEN"         # OPEN / CLOSE
    side: str = "BUY"            # BUY / SELL
    quantity: float = 0.0
    price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    strategy: str = ""
    reason: str = ""
    leverage: int = 1

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典 (用于 Redis 发布)"""
        d = asdict(self)
        d["signal_id"] = str(d["signal_id"])
        return d


class BaseStrategy(ABC):
    """
    策略基类
    所有具体策略必须实现 on_kline 方法
    """

    def __init__(self, name: str, symbols: Optional[List[str]] = None) -> None:
        self.name = name
        self._symbols = symbols or []
        self._enabled = True
        # 情绪指数 (由 AI 模块写入, 策略可选择性参考)
        self._sentiment_score: float = 0.0
        # AI 参数建议
        self._ai_params: Dict[str, Any] = {}

    @property
    def enabled(self) -> bool:
        """策略是否启用"""
        return self._enabled

    def enable(self) -> None:
        """启用策略"""
        self._enabled = True
        logger.info("strategy.enabled", name=self.name)

    def disable(self) -> None:
        """禁用策略"""
        self._enabled = False
        logger.info("strategy.disabled", name=self.name)

    def set_sentiment(self, score: float) -> None:
        """
        设置情绪指数 (由外部调用)

        Args:
            score: 情绪分数 (-1.0 ~ +1.0)
        """
        self._sentiment_score = max(-1.0, min(1.0, score))

    def get_sentiment(self) -> float:
        """获取当前情绪指数"""
        return self._sentiment_score

    def set_ai_params(self, params: Dict[str, Any]) -> None:
        """
        设置 AI 参数建议 (由外部调用)

        Args:
            params: 参数字典
        """
        self._ai_params = params

    def get_ai_params(self) -> Dict[str, Any]:
        """获取 AI 参数建议"""
        return self._ai_params

    @abstractmethod
    async def on_kline(
        self, symbol: str, df: pd.DataFrame
    ) -> Optional[Signal]:
        """
        K 线闭合回调 (策略核心逻辑)

        Args:
            symbol: 交易对
            df: 包含指标列的 K 线 DataFrame

        Returns:
            交易信号, 无信号返回 None
        """
        ...

    async def on_trade(self, symbol: str, trade: Dict) -> None:
        """
        成交事件回调 (可选重写)

        Args:
            symbol: 交易对
            trade: 成交数据
        """
        pass

    def _create_signal(
        self,
        symbol: str,
        action: str,
        side: str,
        quantity: float,
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        reason: str = "",
        leverage: int = 1,
    ) -> Signal:
        """
        创建标准化信号

        Args:
            symbol: 交易对
            action: OPEN / CLOSE
            side: BUY / SELL
            quantity: 数量
            price: 价格 (None 表示市价)
            stop_loss: 止损价
            take_profit: 止盈价
            reason: 信号原因描述
            leverage: 杠杆

        Returns:
            Signal 实例
        """
        return Signal(
            symbol=symbol,
            action=action,
            side=side,
            quantity=quantity,
            price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strategy=self.name,
            reason=reason,
            leverage=leverage,
        )
