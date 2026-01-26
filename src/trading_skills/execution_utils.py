"""
模块功能：交易执行工具函数
主要作用：
1. 价格和数量的精度截断（Clamp）
2. 根据 Orderbook 预估滑点和成交均价
3. 提供通用的数值处理辅助函数
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any


def _d(v: Any) -> Decimal:
    return Decimal(str(v))


def _decimals_from_step(step: Decimal) -> int:
    s = format(step.normalize(), "f")
    if "." not in s:
        return 0
    return len(s.split(".", 1)[1])


def round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return _d(value)
    q = (_d(value) / step).to_integral_value(rounding=ROUND_DOWN)
    return (q * step).quantize(step, rounding=ROUND_DOWN)


def clamp_price(price: Decimal, tick_size: Decimal, precision: int | None) -> Decimal:
    p = round_down_to_step(price, tick_size)
    if precision is None:
        return p
    fmt = Decimal("1." + ("0" * precision))
    return p.quantize(fmt, rounding=ROUND_DOWN)


def clamp_qty(qty: Decimal, step_size: Decimal, precision: int | None) -> Decimal:
    q = round_down_to_step(qty, step_size)
    if precision is None:
        return q
    fmt = Decimal("1." + ("0" * precision))
    return q.quantize(fmt, rounding=ROUND_DOWN)


@dataclass(frozen=True)
class SlippageEstimate:
    mid_price: Decimal
    avg_fill_price: Decimal
    slippage_ratio: Decimal
    enough_liquidity: bool


def estimate_slippage_from_orderbook(
    order_book: dict[str, Any],
    *,
    side: str,
    qty: Decimal,
) -> SlippageEstimate:
    bids = order_book.get("bids", []) if isinstance(order_book, dict) else []
    asks = order_book.get("asks", []) if isinstance(order_book, dict) else []

    best_bid = _d(bids[0][0]) if bids and isinstance(bids[0], list) and len(bids[0]) >= 2 else Decimal("0")
    best_ask = _d(asks[0][0]) if asks and isinstance(asks[0], list) and len(asks[0]) >= 2 else Decimal("0")
    mid = (best_bid + best_ask) / 2 if best_bid > 0 and best_ask > 0 else max(best_bid, best_ask)

    levels = asks if side.upper() == "BUY" else bids
    remaining = qty
    notional = Decimal("0")
    filled = Decimal("0")

    for lvl in levels:
        if not isinstance(lvl, list) or len(lvl) < 2:
            continue
        price = _d(lvl[0])
        available = _d(lvl[1])
        if price <= 0 or available <= 0:
            continue
        take = available if available <= remaining else remaining
        notional += take * price
        filled += take
        remaining -= take
        if remaining <= 0:
            break

    enough = filled >= qty and qty > 0
    avg = (notional / filled) if filled > 0 else Decimal("0")
    if mid > 0 and avg > 0:
        slip = (avg - mid) / mid if side.upper() == "BUY" else (mid - avg) / mid
    else:
        slip = Decimal("0")

    return SlippageEstimate(
        mid_price=mid,
        avg_fill_price=avg,
        slippage_ratio=slip,
        enough_liquidity=enough,
    )

