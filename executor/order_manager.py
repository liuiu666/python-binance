"""
订单管理器
功能:
- 接收风控通过的信号, 调用币安 API 下单
- 幂等性: 通过 signal_id 防止重复下单
- 订单状态追踪: PENDING → PARTIALLY_FILLED → FILLED / CANCELED / EXPIRED
- 超时撤单: 限价单超过 N 秒未成交 → 自动撤单
- 成交后写入 PostgreSQL + Redis Streams
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from common.config import settings
from common.logger import get_logger
from common.redis_client import redis_client, STREAM_ORDER
from common.notify import notifier
from common.db import db

from collector.rest_client import rest_client
from executor.rate_limiter import TokenBucketRateLimiter

logger = get_logger(__name__)


class OrderStatus(str, Enum):
    """订单状态枚举"""
    PENDING = "PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


@dataclass
class TrackedOrder:
    """追踪中的订单"""
    signal_id: str
    symbol: str
    side: str           # BUY / SELL
    action: str         # OPEN / CLOSE
    quantity: float
    price: Optional[float]
    order_type: str     # MARKET / LIMIT
    order_id: Optional[int] = None
    status: OrderStatus = OrderStatus.PENDING
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    strategy: str = ""
    reason: str = ""
    created_at: float = field(default_factory=time.time)
    filled_price: Optional[float] = None
    filled_qty: float = 0.0
    fee: float = 0.0
    # 平仓后的盈亏 (由 _handle_close_fill 写入)
    pnl: Optional[float] = None

    # 限价单超时时间 (秒)
    timeout_seconds: float = 60.0


class OrderManager:
    """
    订单管理器
    负责下单、撤单、状态追踪和成交处理
    """

    def __init__(self) -> None:
        self._rate_limiter = TokenBucketRateLimiter()
        # 活跃订单追踪: {order_id: TrackedOrder}
        self._active_orders: Dict[int, TrackedOrder] = {}
        # signal_id → order_id 映射
        self._signal_order_map: Dict[str, int] = {}
        # 超时检查任务
        self._timeout_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        """启动订单管理器"""
        self._running = True
        self._timeout_task = asyncio.create_task(self._timeout_check_loop())
        logger.info("order_manager.started")

    async def stop(self) -> None:
        """停止订单管理器"""
        self._running = False
        if self._timeout_task:
            self._timeout_task.cancel()
            try:
                await self._timeout_task
            except asyncio.CancelledError:
                pass
        logger.info("order_manager.stopped")

    # ============================================================
    # 下单
    # ============================================================

    async def place_order(self, signal: Dict) -> Optional[Dict]:
        """
        根据交易信号下单

        Args:
            signal: 交易信号字典

        Returns:
            币安 API 响应, 失败返回 None
        """
        signal_id = signal.get("signal_id", "")
        symbol = signal.get("symbol", "")
        side = signal.get("side", "BUY")
        action = signal.get("action", "OPEN")
        quantity = float(signal.get("quantity", 0))
        price = signal.get("price")
        stop_loss = signal.get("stop_loss")
        take_profit = signal.get("take_profit")
        strategy = signal.get("strategy", "")
        reason = signal.get("reason", "")

        # 确定订单类型
        if price and float(price) > 0:
            order_type = "LIMIT"
        else:
            order_type = "MARKET"
            price = None

        # 构建订单参数
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": quantity,
        }

        if order_type == "LIMIT":
            params["price"] = price
            params["timeInForce"] = "GTC"

        # 获取限流令牌
        await self._rate_limiter.acquire(weight=1)

        # 调用币安 API
        logger.info(
            "order_manager.placing",
            signal_id=signal_id,
            symbol=symbol,
            side=side,
            type=order_type,
            quantity=quantity,
            price=price,
        )

        result = await rest_client.place_order(params)

        if not result:
            logger.error("order_manager.place_failed", signal_id=signal_id)
            await self._handle_error(signal, "API 返回空")
            return None

        order_id = result.get("orderId")
        status = result.get("status", "")

        # 创建追踪记录
        tracked = TrackedOrder(
            signal_id=signal_id,
            symbol=symbol,
            side=side,
            action=action,
            quantity=quantity,
            price=float(price) if price else None,
            order_type=order_type,
            order_id=order_id,
            stop_loss=float(stop_loss) if stop_loss else None,
            take_profit=float(take_profit) if take_profit else None,
            strategy=strategy,
            reason=reason,
        )

        # 根据返回状态更新
        if status == "FILLED":
            tracked.status = OrderStatus.FILLED
            tracked.filled_price = float(result.get("avgPrice", 0))
            tracked.filled_qty = float(result.get("executedQty", 0))
        elif status == "PARTIALLY_FILLED":
            tracked.status = OrderStatus.PARTIALLY_FILLED
        elif status == "NEW":
            tracked.status = OrderStatus.PENDING
        elif status == "REJECTED":
            tracked.status = OrderStatus.REJECTED
            logger.error(
                "order_manager.rejected",
                signal_id=signal_id,
                reason=result.get("rejectReason"),
            )

        # 注册追踪
        if order_id:
            self._active_orders[order_id] = tracked
            self._signal_order_map[signal_id] = order_id

        logger.info(
            "order_manager.placed",
            signal_id=signal_id,
            order_id=order_id,
            status=status,
        )

        # 通知
        await notifier.notify_trade(
            action=action,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=float(price) if price else float(result.get("avgPrice", 0)),
            reason=reason,
        )

        # 写入 Redis Streams
        await redis_client.xadd(STREAM_ORDER, {
            "signal_id": signal_id,
            "order_id": str(order_id),
            "symbol": symbol,
            "side": side,
            "status": status,
            "price": str(result.get("avgPrice", "")),
            "quantity": str(result.get("executedQty", "")),
        })

        return result

    # ============================================================
    # 撤单
    # ============================================================

    async def cancel_order(self, symbol: str, order_id: int) -> Optional[Dict]:
        """
        撤销订单

        Args:
            symbol: 交易对
            order_id: 订单 ID

        Returns:
            币安 API 响应
        """
        await self._rate_limiter.acquire(weight=1)
        result = await rest_client.cancel_order(symbol, order_id)

        if result:
            tracked = self._active_orders.get(order_id)
            if tracked:
                tracked.status = OrderStatus.CANCELED
            logger.info("order_manager.canceled", order_id=order_id)
        else:
            logger.error("order_manager.cancel_failed", order_id=order_id)

        return result

    async def cancel_all(self, symbol: str) -> None:
        """撤销指定交易对的所有活跃订单"""
        orders_to_cancel = [
            (oid, o) for oid, o in self._active_orders.items()
            if o.symbol == symbol and o.status in (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED)
        ]
        for order_id, order in orders_to_cancel:
            await self.cancel_order(symbol, order_id)

    # ============================================================
    # 订单状态更新 (由 User Stream 触发)
    # ============================================================

    async def handle_order_update(self, event: Dict) -> None:
        """
        处理来自 User Data Stream 的订单状态更新

        Args:
            event: ORDER_TRADE_UPDATE 事件数据
        """
        order_data = event.get("o", {})
        order_id = order_data.get("i")
        status = order_data.get("X", "")
        executed_qty = float(order_data.get("z", 0))
        avg_price = float(order_data.get("L", 0))
        commission = float(order_data.get("n", 0))

        tracked = self._active_orders.get(order_id)
        if not tracked:
            logger.debug("order_manager.unknown_order_update", order_id=order_id)
            return

        old_status = tracked.status

        if status == "FILLED":
            tracked.status = OrderStatus.FILLED
            tracked.filled_price = avg_price
            tracked.filled_qty = executed_qty
            tracked.fee = commission
            await self._handle_fill(tracked)

        elif status == "PARTIALLY_FILLED":
            tracked.status = OrderStatus.PARTIALLY_FILLED
            tracked.filled_qty = executed_qty
            tracked.fee += commission

        elif status == "CANCELED":
            tracked.status = OrderStatus.CANCELED
            await self._handle_cancel(tracked)

        elif status == "EXPIRED":
            tracked.status = OrderStatus.EXPIRED

        logger.info(
            "order_manager.status_update",
            order_id=order_id,
            old_status=old_status.value,
            new_status=status,
            signal_id=tracked.signal_id,
        )

    # ============================================================
    # 内部处理
    # ============================================================

    async def _handle_fill(self, order: TrackedOrder) -> None:
        """
        订单完全成交后的处理
        - 开仓 (action=OPEN): 写入 trades 表, 状态 OPENED
        - 平仓 (action=CLOSE): 更新 trades 表, 计算 PnL, 更新 daily_pnl
        """
        if order.action == "CLOSE":
            await self._handle_close_fill(order)
        else:
            await self._handle_open_fill(order)

        # 发送成交通知
        pnl = order.pnl if order.action == "CLOSE" else None
        await notifier.notify_trade(
            action=order.action,
            symbol=order.symbol,
            side=order.side,
            quantity=order.filled_qty,
            price=order.filled_price or 0,
            pnl=pnl,
            reason=order.reason,
        )

        # 从活跃列表移除
        if order.order_id:
            self._active_orders.pop(order.order_id, None)
            self._signal_order_map.pop(order.signal_id, None)

    async def _handle_open_fill(self, order: TrackedOrder) -> None:
        """开仓成交处理"""
        try:
            async with db.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO trades (signal_id, symbol, side, action, quantity,
                                       entry_price, stop_loss, take_profit,
                                       order_id, strategy, reason, status, opened_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, NOW())
                    ON CONFLICT (signal_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        entry_price = EXCLUDED.entry_price
                    """,
                    order.signal_id, order.symbol, order.side, order.action,
                    order.quantity, order.filled_price, order.stop_loss,
                    order.take_profit, order.order_id, order.strategy,
                    order.reason, "OPENED",
                )
        except Exception:
            logger.exception("order_manager.db_write_error")

    async def _handle_close_fill(self, order: TrackedOrder) -> None:
        """
        平仓成交处理
        查找对应的未平仓交易, 计算盈亏, 更新 trades 和 daily_pnl
        """
        exit_price = order.filled_price or 0
        quantity = order.filled_qty

        try:
            async with db.pool.acquire() as conn:
                # 查找同 symbol 的未平仓交易
                opening_trade = await conn.fetchrow(
                    """
                    SELECT id, signal_id, entry_price, side, strategy
                    FROM trades
                    WHERE symbol = $1 AND status = 'OPENED'
                    ORDER BY opened_at DESC
                    LIMIT 1
                    """,
                    order.symbol,
                )

                pnl = 0.0
                entry_price = 0.0

                if opening_trade:
                    entry_price = float(opening_trade["entry_price"])
                    side = opening_trade["side"]

                    # 计算盈亏: 做多 PnL = (exit - entry) * qty; 做空 PnL = (entry - exit) * qty
                    if side == "BUY":
                        pnl = (exit_price - entry_price) * quantity
                    else:
                        pnl = (entry_price - exit_price) * quantity

                    # 扣除手续费
                    pnl -= order.fee

                    # 更新原交易记录
                    await conn.execute(
                        """
                        UPDATE trades SET
                            status = 'CLOSED',
                            exit_price = $1,
                            pnl = $2,
                            fee = fee + $3,
                            closed_at = NOW()
                        WHERE id = $4
                        """,
                        exit_price, pnl, order.fee, opening_trade["id"],
                    )

                    logger.info(
                        "order_manager.close_pnl",
                        symbol=order.symbol,
                        entry=entry_price,
                        exit=exit_price,
                        pnl=f"{pnl:+.2f}",
                    )
                else:
                    logger.warning(
                        "order_manager.no_opening_trade",
                        symbol=order.symbol,
                    )

                # 更新 daily_pnl 表
                today = "CURRENT_DATE"
                is_win = 1 if pnl > 0 else 0
                is_loss = 1 if pnl < 0 else 0

                await conn.execute(
                    """
                    INSERT INTO daily_pnl (trade_date, total_pnl, total_fee, trade_count, win_count, loss_count)
                    VALUES (CURRENT_DATE, $1, $2, 1, $3, $4)
                    ON CONFLICT (trade_date) DO UPDATE SET
                        total_pnl = daily_pnl.total_pnl + EXCLUDED.total_pnl,
                        total_fee = daily_pnl.total_fee + EXCLUDED.total_fee,
                        trade_count = daily_pnl.trade_count + 1,
                        win_count = daily_pnl.win_count + EXCLUDED.win_count,
                        loss_count = daily_pnl.loss_count + EXCLUDED.loss_count
                    """,
                    pnl, order.fee, is_win, is_loss,
                )

                # 将 PnL 附加到 order 上供通知使用
                order.pnl = pnl

        except Exception:
            logger.exception("order_manager.close_db_error")

    async def _handle_cancel(self, order: TrackedOrder) -> None:
        """订单取消后的处理"""
        if order.order_id:
            self._active_orders.pop(order.order_id, None)
            self._signal_order_map.pop(order.signal_id, None)

    async def _handle_error(self, signal: Dict, reason: str) -> None:
        """下单错误处理"""
        signal_id = signal.get("signal_id", "")
        await redis_client.xadd(STREAM_ORDER, {
            "signal_id": signal_id,
            "symbol": signal.get("symbol", ""),
            "status": "ERROR",
            "reason": reason,
        })

    async def _timeout_check_loop(self) -> None:
        """定期检查限价单是否超时"""
        while self._running:
            await asyncio.sleep(5)
            now = time.time()
            expired = [
                (oid, o) for oid, o in self._active_orders.items()
                if o.order_type == "LIMIT"
                and o.status == OrderStatus.PENDING
                and (now - o.created_at) > o.timeout_seconds
            ]
            for order_id, order in expired:
                logger.warning(
                    "order_manager.timeout_cancel",
                    order_id=order_id,
                    signal_id=order.signal_id,
                    age=f"{now - order.created_at:.0f}s",
                )
                await self.cancel_order(order.symbol, order_id)
