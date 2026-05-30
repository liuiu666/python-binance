"""
前置风控模块
功能:
- 单笔最大金额不超过账户净值 N%
- 总持仓不超过 M 个
- 日累计亏损超过阈值后进入只读模式
- 同 symbol 同方向信号去重
- 杠杆不超过配置上限

每条规则独立函数, 返回 (pass: bool, reason: str), 便于日志追溯
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from common.config import settings
from common.logger import get_logger
from common.redis_client import redis_client

logger = get_logger(__name__)

# Redis key 前缀
PENDING_SIGNALS_KEY = "risk:pending_signals"
DAILY_LOSS_KEY = "risk:daily_loss"
READONLY_UNTIL_KEY = "risk:readonly_until"


@dataclass
class RiskCheckResult:
    """风控检查结果"""
    passed: bool
    reason: str
    rule_name: str = ""


@dataclass
class RiskState:
    """风控运行时状态"""
    # 当前持仓数
    open_positions: int = 0
    # 当前账户余额 (USDT)
    account_balance: float = 0.0
    # 当前账户净值 (含未实现盈亏)
    account_equity: float = 0.0
    # 今日已实现亏损
    daily_realized_loss: float = 0.0
    # 是否处于只读模式
    is_readonly: bool = False
    # 待处理信号集合 (signal_id)
    pending_signals: Set[str] = field(default_factory=set)
    # 当前持仓方向: {symbol: "BUY"/"SELL"}
    position_sides: Dict[str, str] = field(default_factory=dict)


class RiskManager:
    """
    前置风控引擎

    所有交易信号在进入订单管理器之前, 必须通过风控检查
    """

    def __init__(self) -> None:
        self._state = RiskState()
        self._rules = [
            ("readonly_check", self._check_readonly),
            ("max_daily_loss", self._check_daily_loss),
            ("max_order_size", self._check_max_order_size),
            ("max_positions", self._check_max_positions),
            ("duplicate_signal", self._check_duplicate_signal),
            ("leverage_limit", self._check_leverage),
            ("same_direction", self._check_same_direction),
        ]

    def update_state(
        self,
        balance: float = 0.0,
        equity: float = 0.0,
        open_positions: int = 0,
        daily_loss: float = 0.0,
        position_sides: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        更新风控状态 (由执行器定期从交易所同步)

        Args:
            balance: 账户余额
            equity: 账户净值
            open_positions: 当前持仓数
            daily_loss: 今日已实现亏损
            position_sides: 当前持仓方向
        """
        self._state.account_balance = balance
        self._state.account_equity = equity
        self._state.open_positions = open_positions
        self._state.daily_realized_loss = daily_loss
        if position_sides is not None:
            self._state.position_sides = position_sides

        # 检查是否应该进入只读模式
        if daily_loss >= settings.max_daily_loss:
            self._state.is_readonly = True

    def set_readonly(self, readonly: bool) -> None:
        """手动设置只读模式"""
        self._state.is_readonly = readonly
        logger.warning("risk.readonly_changed", readonly=readonly)

    async def check_signal(self, signal: Dict) -> RiskCheckResult:
        """
        对交易信号执行完整的风控检查

        Args:
            signal: 交易信号字典, 包含 symbol, side, quantity, price 等

        Returns:
            RiskCheckResult: 检查结果
        """
        for rule_name, rule_fn in self._rules:
            result = await rule_fn(signal)
            if not result.passed:
                logger.warning(
                    "risk.signal_rejected",
                    rule=rule_name,
                    reason=result.reason,
                    signal_id=signal.get("signal_id"),
                    symbol=signal.get("symbol"),
                )
                return result

        logger.info(
            "risk.signal_passed",
            signal_id=signal.get("signal_id"),
            symbol=signal.get("symbol"),
        )
        return RiskCheckResult(passed=True, reason="全部通过", rule_name="all")

    def remove_pending_signal(self, signal_id: str) -> None:
        """
        从待处理集合中移除信号 (订单完成或取消后调用)

        Args:
            signal_id: 信号 ID
        """
        self._state.pending_signals.discard(signal_id)

    # ============================================================
    # 风控规则实现
    # ============================================================

    async def _check_readonly(self, signal: Dict) -> RiskCheckResult:
        """规则 0: 只读模式检查"""
        if self._state.is_readonly:
            return RiskCheckResult(
                passed=False,
                reason=f"系统处于只读模式 (日亏损 {self._state.daily_realized_loss:.2f} >= {settings.max_daily_loss})",
                rule_name="readonly_check",
            )
        return RiskCheckResult(passed=True, reason="非只读模式", rule_name="readonly_check")

    async def _check_daily_loss(self, signal: Dict) -> RiskCheckResult:
        """规则 1: 日最大亏损检查"""
        if self._state.daily_realized_loss >= settings.max_daily_loss:
            self._state.is_readonly = True
            return RiskCheckResult(
                passed=False,
                reason=f"日累计亏损 {self._state.daily_realized_loss:.2f} USDT 已达上限 {settings.max_daily_loss}",
                rule_name="max_daily_loss",
            )
        return RiskCheckResult(passed=True, reason="日亏损未超限", rule_name="max_daily_loss")

    async def _check_max_order_size(self, signal: Dict) -> RiskCheckResult:
        """规则 2: 单笔最大金额检查"""
        quantity = float(signal.get("quantity", 0))
        price = float(signal.get("price", 0))
        order_value = quantity * price
        max_value = self._state.account_equity * settings.max_order_pct / 100

        if max_value <= 0:
            return RiskCheckResult(
                passed=False,
                reason=f"账户净值为零或负, 无法下单",
                rule_name="max_order_size",
            )

        if order_value > max_value:
            return RiskCheckResult(
                passed=False,
                reason=f"单笔金额 {order_value:.2f} USDT 超过净值 {settings.max_order_pct}% ({max_value:.2f} USDT)",
                rule_name="max_order_size",
            )
        return RiskCheckResult(
            passed=True,
            reason=f"单笔金额 {order_value:.2f} <= {max_value:.2f}",
            rule_name="max_order_size",
        )

    async def _check_max_positions(self, signal: Dict) -> RiskCheckResult:
        """规则 3: 最大持仓数检查"""
        action = signal.get("action", "OPEN")
        if action != "OPEN":
            return RiskCheckResult(passed=True, reason="非开仓操作", rule_name="max_positions")

        if self._state.open_positions >= settings.max_positions:
            return RiskCheckResult(
                passed=False,
                reason=f"当前持仓 {self._state.open_positions} 已达上限 {settings.max_positions}",
                rule_name="max_positions",
            )
        return RiskCheckResult(
            passed=True,
            reason=f"持仓数 {self._state.open_positions} < {settings.max_positions}",
            rule_name="max_positions",
        )

    async def _check_duplicate_signal(self, signal: Dict) -> RiskCheckResult:
        """规则 4: 信号去重检查"""
        signal_id = signal.get("signal_id", "")
        if not signal_id:
            return RiskCheckResult(
                passed=False,
                reason="信号缺少 signal_id",
                rule_name="duplicate_signal",
            )

        if signal_id in self._state.pending_signals:
            return RiskCheckResult(
                passed=False,
                reason=f"重复信号 {signal_id}",
                rule_name="duplicate_signal",
            )

        # 加入待处理集合
        self._state.pending_signals.add(signal_id)
        return RiskCheckResult(passed=True, reason="非重复信号", rule_name="duplicate_signal")

    async def _check_leverage(self, signal: Dict) -> RiskCheckResult:
        """规则 5: 杠杆上限检查"""
        leverage = int(signal.get("leverage", 1))
        if leverage > settings.max_leverage:
            return RiskCheckResult(
                passed=False,
                reason=f"杠杆 {leverage}x 超过上限 {settings.max_leverage}x",
                rule_name="leverage_limit",
            )
        return RiskCheckResult(passed=True, reason=f"杠杆 {leverage}x <= {settings.max_leverage}x", rule_name="leverage_limit")

    async def _check_same_direction(self, signal: Dict) -> RiskCheckResult:
        """规则 6: 同方向重复持仓检查"""
        symbol = signal.get("symbol", "")
        side = signal.get("side", "")
        action = signal.get("action", "OPEN")

        if action != "OPEN":
            return RiskCheckResult(passed=True, reason="非开仓操作", rule_name="same_direction")

        current_side = self._state.position_sides.get(symbol)
        if current_side and current_side == side:
            return RiskCheckResult(
                passed=False,
                reason=f"{symbol} 已有 {side} 方向持仓, 拒绝同向加仓",
                rule_name="same_direction",
            )
        return RiskCheckResult(passed=True, reason="无同向持仓", rule_name="same_direction")
