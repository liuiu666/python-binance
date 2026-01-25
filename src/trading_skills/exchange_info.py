"""
模块功能：交易所规则信息
主要作用：
1. 获取交易对的详细规则（Symbol Rules）
2. 解析价格精度（tick_size）、数量精度（step_size）
3. 解析最小下单数量、最小名义价值等限制
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from binance.client import Client

from .binance_client import call_with_retry


def _d(v: Any) -> Decimal:
    return Decimal(str(v))


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    status: str
    contract_type: str
    quote_asset: str
    base_asset: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal | None
    price_precision: int | None
    quantity_precision: int | None


class FuturesExchangeInfo:
    def __init__(self, client: Client):
        self._client = client

    def fetch_exchange_info(self) -> dict[str, Any]:
        return call_with_retry(lambda: self._client.futures_exchange_info())

    def get_symbol_rules(self, symbol: str) -> SymbolRules:
        info = self.fetch_exchange_info()
        items = info.get("symbols", [])
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("symbol") != symbol:
                continue

            filters = item.get("filters", [])
            tick_size = Decimal("0")
            step_size = Decimal("0")
            min_qty = Decimal("0")
            min_notional: Decimal | None = None

            for f in filters:
                if not isinstance(f, dict):
                    continue
                if f.get("filterType") == "PRICE_FILTER":
                    tick_size = _d(f.get("tickSize"))
                if f.get("filterType") == "LOT_SIZE":
                    step_size = _d(f.get("stepSize"))
                    min_qty = _d(f.get("minQty"))
                if f.get("filterType") in {"MIN_NOTIONAL", "NOTIONAL"}:
                    raw = f.get("notional") if f.get("filterType") == "NOTIONAL" else f.get("minNotional")
                    if raw is not None:
                        min_notional = _d(raw)

            price_precision = item.get("pricePrecision")
            quantity_precision = item.get("quantityPrecision")
            pp = int(price_precision) if isinstance(price_precision, int) else None
            qp = int(quantity_precision) if isinstance(quantity_precision, int) else None

            return SymbolRules(
                symbol=symbol,
                status=str(item.get("status", "")),
                contract_type=str(item.get("contractType", "")),
                quote_asset=str(item.get("quoteAsset", "")),
                base_asset=str(item.get("baseAsset", "")),
                tick_size=tick_size,
                step_size=step_size,
                min_qty=min_qty,
                min_notional=min_notional,
                price_precision=pp,
                quantity_precision=qp,
            )
        raise ValueError("找不到交易对规则")

