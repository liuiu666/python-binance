"""
模块功能：高级交易接口
主要作用：
1. 整合 OrderExecutor, ExchangeInfo 等模块
2. 提供语义化的高级下单接口（如按 USDT 金额开仓）
3. 提供查询当前挂单、撤单等快捷方法
4. 作为上层策略调用的主要入口
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from binance.client import Client

from .binance_client import call_with_retry
from .exchange_info import FuturesExchangeInfo
from .execution_utils import clamp_qty
from .order_executor import OrderExecutor, StopResult, TakeProfitResult


def _d(v: Any) -> Decimal:
    return Decimal(str(v))


@dataclass(frozen=True)
class EntryResult:
    symbol: str
    side: str
    order_id: int
    executed_qty: Decimal
    avg_price: Decimal | None


class FuturesTrader:
    def __init__(self, client: Client):
        self._client = client
        self._ex = FuturesExchangeInfo(client)
        self.executor = OrderExecutor(client)

    def _request_futures_algo(self, method: str, path: str, params: dict[str, Any]) -> Any:
        # 使用 request_futures_api 发送请求，如果方法不存在则抛出异常
        if hasattr(self._client, "_request_futures_api"):
            return call_with_retry(
                lambda: self._client._request_futures_api(
                    method, path, True, data=params
                )
            )
        raise RuntimeError("当前 binance 库版本过低，不支持 futures algo 接口")

    def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        return call_with_retry(lambda: self._client.futures_change_leverage(symbol=symbol, leverage=leverage))

    def list_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        raw = call_with_retry(lambda: self._client.futures_get_open_orders(symbol=symbol))
        return raw if isinstance(raw, list) else []

    def list_open_algo_orders(self, symbol: str) -> list[dict[str, Any]]:
        raw = self._request_futures_algo("get", "openAlgoOrders", {"symbol": symbol})
        return raw if isinstance(raw, list) else []

    def list_all_algo_orders(self, symbol: str) -> list[dict[str, Any]]:
        # allAlgoOrders 通常也需要签名请求
        raw = self._request_futures_algo("get", "allAlgoOrders", {"symbol": symbol})
        return raw if isinstance(raw, list) else []

    def cancel_order(self, symbol: str, order_id: int) -> dict[str, Any]:
        data = call_with_retry(lambda: self._client.futures_cancel_order(symbol=symbol, orderId=order_id))
        return data if isinstance(data, dict) else {}

    def cancel_algo_order(self, symbol: str, algo_id: int) -> dict[str, Any]:
        data = self._request_futures_algo("delete", "algoOrder", {"symbol": symbol, "algoId": algo_id})
        return data if isinstance(data, dict) else {}

    def cancel_all_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        raw = call_with_retry(lambda: self._client.futures_cancel_all_open_orders(symbol=symbol))
        return raw if isinstance(raw, list) else []

    def cancel_all_open_algo_orders(self, symbol: str) -> list[dict[str, Any]]:
        raw = self._request_futures_algo("delete", "algoOpenOrders", {"symbol": symbol})
        return raw if isinstance(raw, list) else []

    def place_market_entry_by_usdt(
        self,
        *,
        symbol: str,
        side: str,
        usdt_amount: Decimal,
    ) -> EntryResult:
        rules = self._ex.get_symbol_rules(symbol)
        mark = call_with_retry(lambda: self._client.futures_mark_price(symbol=symbol))
        mark_price = _d(mark.get("markPrice"))
        if mark_price <= 0:
            raise RuntimeError("标记价格异常")

        raw_qty = usdt_amount / mark_price
        qty = clamp_qty(raw_qty, rules.step_size, rules.quantity_precision)
        if qty <= 0:
            raise RuntimeError("数量异常")

        resp = call_with_retry(
            lambda: self._client.futures_create_order(
                symbol=symbol,
                side=side.upper(),
                type="MARKET",
                quantity=str(qty),
            )
        )
        order_id = int(resp.get("orderId"))

        o = call_with_retry(lambda: self._client.futures_get_order(symbol=symbol, orderId=order_id))
        executed_qty = _d(o.get("executedQty"))
        avg_price: Decimal | None = None
        ap = o.get("avgPrice")
        if ap is not None and str(ap).strip():
            try:
                avg_price = _d(ap)
            except Exception:
                avg_price = None

        return EntryResult(
            symbol=symbol,
            side=side.upper(),
            order_id=order_id,
            executed_qty=executed_qty,
            avg_price=avg_price,
        )

    def place_stop_loss_market(
        self,
        *,
        symbol: str,
        entry_side: str,
        quantity: Decimal,
        stop_price: Decimal,
        trigger_type: str = "MARK_PRICE",
        position_side: str | None = None,
    ) -> StopResult:
        return self.executor.place_stop_loss_market(
            symbol=symbol,
            entry_side=entry_side,
            quantity=quantity,
            stop_price=stop_price,
            trigger_type=trigger_type,
            position_side=position_side,
        )

    def place_take_profit_market(
        self,
        *,
        symbol: str,
        entry_side: str,
        quantity: Decimal,
        take_profit_price: Decimal,
        trigger_type: str = "MARK_PRICE",
        position_side: str | None = None,
    ) -> TakeProfitResult:
        return self.executor.place_take_profit_market(
            symbol=symbol,
            entry_side=entry_side,
            quantity=quantity,
            take_profit_price=take_profit_price,
            trigger_type=trigger_type,
            position_side=position_side,
        )
