"""
模块功能：下单预检风控
主要作用：
1. 在下单前检查账户资金是否充足
2. 检查滑点是否在允许范围内
3. 检查订单数量是否符合交易所规则（最小数量、名义价值）
4. 返回详细的检查报告
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from binance.client import Client

from .account_data import FuturesAccountData
from .exchange_info import FuturesExchangeInfo
from .execution_utils import (
    SlippageEstimate,
    clamp_qty,
    estimate_slippage_from_orderbook,
)
from .binance_client import call_with_retry


@dataclass(frozen=True)
class PrecheckResult:
    通过: bool
    原因: list[str]
    数量: str
    标记价格: str
    预估滑点: str
    预估成交均价: str
    可用余额: str
    预估保证金: str


class OrderPrecheck:
    def __init__(self, client: Client):
        self._client = client
        self._ex = FuturesExchangeInfo(client)
        self._acct = FuturesAccountData(client)

    def _mark_price(self, symbol: str) -> Decimal:
        data = call_with_retry(lambda: self._client.futures_mark_price(symbol=symbol))
        return Decimal(str(data.get("markPrice")))

    def check_market_order_by_usdt(
        self,
        *,
        symbol: str,
        side: str,
        usdt_amount: Decimal,
        leverage: Decimal,
        slippage_limit: Decimal,
        orderbook_limit: int = 100,
    ) -> PrecheckResult:
        reasons: list[str] = []
        rules = self._ex.get_symbol_rules(symbol)

        if rules.status != "TRADING":
            reasons.append("当前不可交易")
        if rules.quote_asset != "USDT":
            reasons.append("不是USDT计价")

        mark = self._mark_price(symbol)
        if mark <= 0:
            reasons.append("标记价格异常")

        if leverage <= 0:
            reasons.append("杠杆必须大于0")
        if usdt_amount <= 0:
            reasons.append("下单金额必须大于0")

        raw_qty = (usdt_amount / mark) if mark > 0 else Decimal("0")
        qty = clamp_qty(raw_qty, rules.step_size, rules.quantity_precision)

        if rules.min_qty > 0 and qty < rules.min_qty:
            reasons.append("数量小于最小下单数量")

        notional = qty * mark
        if rules.min_notional is not None and rules.min_notional > 0 and notional < rules.min_notional:
            reasons.append("名义价值小于最小下单名义价值")

        ob = call_with_retry(lambda: self._client.futures_order_book(symbol=symbol, limit=orderbook_limit))
        slip: SlippageEstimate = estimate_slippage_from_orderbook(ob, side=side, qty=qty)
        if not slip.enough_liquidity:
            reasons.append("订单簿深度不足")
        if slip.slippage_ratio > slippage_limit:
            reasons.append("预估滑点超过上限")

        acct = self._acct.snapshot(symbol)
        avail = acct.usdt_available_balance
        margin = (notional / leverage) if leverage > 0 else Decimal("0")

        if avail is not None and margin > avail:
            reasons.append("可用余额不足")

        ok = len(reasons) == 0
        return PrecheckResult(
            通过=ok,
            原因=reasons,
            数量=str(qty),
            标记价格=str(mark),
            预估滑点=str(slip.slippage_ratio),
            预估成交均价=str(slip.avg_fill_price),
            可用余额=str(avail) if avail is not None else "",
            预估保证金=str(margin),
        )

