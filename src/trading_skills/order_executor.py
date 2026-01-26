"""
模块功能：底层订单执行器
主要作用：
1. 处理复杂的底层下单逻辑
2. 封装止损单（STOP_MARKET）和止盈单（TAKE_PROFIT_MARKET）的创建
3. 处理 Binance API 的兼容性问题（如 reduceOnly, closePosition, algoOrder 的不同行为）
4. 自动重试和错误处理
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from binance.client import Client
from binance.exceptions import BinanceAPIException

from .binance_client import call_with_retry
from .exchange_info import FuturesExchangeInfo
from .execution_utils import clamp_price, clamp_qty


def _d(v: Any) -> Decimal:
    return Decimal(str(v))


def _opposite_side(side: str) -> str:
    s = side.upper()
    if s == "BUY":
        return "SELL"
    if s == "SELL":
        return "BUY"
    raise ValueError("方向必须是 BUY 或 SELL")


def _position_side_for_entry(entry_side: str) -> str:
    s = entry_side.upper()
    if s == "BUY":
        return "LONG"
    if s == "SELL":
        return "SHORT"
    raise ValueError("方向必须是 BUY 或 SELL")


def _is_algo_switch_error(msg: str) -> bool:
    s = (msg or "").lower()
    if "stop_order_switch_algo" in s:
        return True
    if "algo order api endpoints instead" in s or "use the algo order api" in s:
        return True
    if "/fapi/v1/algoorder" in s or "algoorder" in s:
        return True
    return ("-4120" in s or "code=-4120" in s or "(-4120" in s) and ("algo" in s)


@dataclass(frozen=True)
class StopResult:
    symbol: str
    side: str
    stop_order_id: int
    stop_price: Decimal
    quantity: Decimal
    close_position: bool


@dataclass(frozen=True)
class TakeProfitResult:
    symbol: str
    side: str
    tp_order_id: int
    tp_price: Decimal
    quantity: Decimal
    close_position: bool


class OrderExecutor:
    def __init__(self, client: Client):
        self._client = client
        self._ex = FuturesExchangeInfo(client)

    def _request_futures_signed_raw(self, method: str, path: str, params: dict[str, Any]) -> Any:
        required = {"session", "_create_futures_api_uri", "_generate_signature", "_order_params", "_get_headers", "_handle_response"}
        if not all(hasattr(self._client, x) for x in required):
            raise RuntimeError("当前binance库不支持raw签名请求")

        # 同步服务器时间
        try:
            if not getattr(self._client, "timestamp_offset", 0):
                server_time = self._client.futures_time()
                self._client.timestamp_offset = server_time['serverTime'] - int(time.time() * 1000)
        except: pass

        uri = self._client._create_futures_api_uri(path, 1)
        data = dict(params)
        data["timestamp"] = int(time.time() * 1000 + self._client.timestamp_offset)
        if getattr(self._client, "REQUEST_RECVWINDOW", None):
            data["recvWindow"] = self._client.REQUEST_RECVWINDOW
        sig = self._client._generate_signature(data)
        qs = "&".join("%s=%s" % (k, v) for k, v in self._client._order_params({**data, "signature": sig}))

        headers = self._client._get_headers()
        m = method.upper()
        if m in {"POST", "PUT", "DELETE"}:
            headers = dict(headers)
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        resp = self._client.session.request(method, uri + "?" + qs, headers=headers)
        return self._client._handle_response(resp)

    def _create_algo_order(self, params: dict[str, Any]) -> dict[str, Any]:
        if not hasattr(self._client, "_request_futures_api"):
            raise RuntimeError("当前binance库不支持自定义futures接口调用")
        try:
            data = call_with_retry(lambda: self._request_futures_signed_raw("post", "algoOrder", params))
            return data if isinstance(data, dict) else {}
        except Exception:
            data = call_with_retry(
                lambda: self._client._request_futures_api(
                    "post",
                    "algoOrder",
                    True,
                    data=params,
                    force_params=True,
                )
            )
            return data if isinstance(data, dict) else {}

    def _create_algo_order_compat(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._create_algo_order(params)
        except BinanceAPIException as e:
            msg = str(getattr(e, "message", "")) or str(e)
            if "Mandatory parameter 'type'" in msg or "parameter 'type'" in msg:
                p2 = dict(params)
                if "type" not in p2 and "orderType" in p2:
                    p2["type"] = p2["orderType"]
                p2.pop("orderType", None)
                return self._create_algo_order(p2)
            if "Unknown parameter 'orderType'" in msg:
                p2 = dict(params)
                p2.pop("orderType", None)
                if "type" not in p2 and "orderType" in params:
                    p2["type"] = params["orderType"]
                return self._create_algo_order(p2)
            raise

    def place_stop_loss_market(
        self,
        *,
        symbol: str,
        entry_side: str,
        quantity: Decimal | None = None,
        stop_price: Decimal,
        trigger_type: str = "MARK_PRICE",
        position_side: str | None = None,
        close_position: bool = False,
    ) -> StopResult:
        rules = self._ex.get_symbol_rules(symbol)
        close_side = _opposite_side(entry_side)
        sp = clamp_price(stop_price, rules.tick_size, rules.price_precision)
        
        qty = Decimal(0)
        if not close_position:
            if quantity is None:
                raise ValueError("若不启用 close_position，必须提供 quantity")
            qty = clamp_qty(quantity, rules.step_size, rules.quantity_precision)
            if qty <= 0:
                raise RuntimeError("数量异常")

        order_params: dict[str, Any] = {
            "symbol": symbol,
            "side": close_side,
            "type": "STOP_MARKET",
            "stopPrice": str(sp),
            "workingType": trigger_type,
        }

        try:
            if close_position:
                 resp = call_with_retry(
                    lambda: self._client.futures_create_order(
                        **order_params,
                        closePosition=True,
                    )
                )
            else:
                resp = call_with_retry(
                    lambda: self._client.futures_create_order(
                        **order_params,
                        quantity=str(qty),
                        reduceOnly=True,
                    )
                )
            stop_order_id = int(resp.get("orderId"))
            return StopResult(
                symbol=symbol,
                side=close_side,
                stop_order_id=stop_order_id,
                stop_price=sp,
                quantity=qty,
                close_position=close_position,
            )
        except BinanceAPIException as e:
            msg = str(getattr(e, "message", "")) or str(e)
            if _is_algo_switch_error(msg) or "-1022" in msg:
                algo_params: dict[str, Any] = {
                    "algoType": "CONDITIONAL",
                    "type": "STOP_MARKET",
                    "orderType": "STOP_MARKET",
                    "symbol": symbol,
                    "side": close_side,
                    "positionSide": position_side or "BOTH",
                    "triggerPrice": str(sp),
                    "workingType": trigger_type,
                    "closePosition": True,
                }
                try:
                    resp = self._create_algo_order_compat(algo_params)
                    algo_id = int(resp.get("algoId"))
                    return StopResult(
                        symbol=symbol,
                        side=close_side,
                        stop_order_id=algo_id,
                        stop_price=sp,
                        quantity=qty,
                        close_position=True,
                    )
                except BinanceAPIException as e2:
                    # 某些错误可能需要 retry 或者 closePosition=True (bool)
                    msg2 = str(getattr(e2, "message", "")) or str(e2)
                    if "Signature" in msg2 or "-1022" in msg2:
                         algo_params_bool = dict(algo_params)
                         algo_params_bool["closePosition"] = True
                         try:
                             resp = self._create_algo_order_compat(algo_params_bool)
                             algo_id = int(resp.get("algoId"))
                             return StopResult(
                                 symbol=symbol,
                                 side=close_side,
                                 stop_order_id=algo_id,
                                 stop_price=sp,
                                 quantity=qty,
                                 close_position=True,
                             )
                         except: pass

                    msg2 = str(getattr(e2, "message", "")) or str(e2)
                    if "reduceOnly" in msg2 or "-4046" in msg2:
                         algo_params_noreduce = dict(algo_params)
                         # Algo order 不支持 reduceOnly，应该移除
                         algo_params_noreduce.pop("reduceOnly", None)
                         # 确保有 closePosition
                         algo_params_noreduce["closePosition"] = True
                         
                         try:
                             resp = self._create_algo_order_compat(algo_params_noreduce)
                             algo_id = int(resp.get("algoId"))
                             return StopResult(
                                 symbol=symbol,
                                 side=close_side,
                                 stop_order_id=algo_id,
                                 stop_price=sp,
                                 quantity=qty,
                                 close_position=True,
                             )
                         except: pass
                    
                    msg2 = str(getattr(e2, "message", "")) or str(e2)
                    if "positionSide" in msg2 or "Hedge" in msg2:
                        ps = position_side or _position_side_for_entry(entry_side)
                        algo_params2 = dict(algo_params)
                        algo_params2["positionSide"] = ps
                        resp = self._create_algo_order_compat(algo_params2)
                        algo_id = int(resp.get("algoId"))
                        return StopResult(
                            symbol=symbol,
                            side=close_side,
                            stop_order_id=algo_id,
                            stop_price=sp,
                            quantity=qty,
                            close_position=True,
                        )
                    raise

            if "closePosition" in msg or "ReduceOnly" in msg:
                resp = call_with_retry(
                    lambda: self._client.futures_create_order(
                        **order_params,
                        closePosition=True,
                    )
                )
                stop_order_id = int(resp.get("orderId"))
                return StopResult(
                    symbol=symbol,
                    side=close_side,
                    stop_order_id=stop_order_id,
                    stop_price=sp,
                    quantity=qty,
                    close_position=True,
                )
            raise

    def place_take_profit_market(
        self,
        *,
        symbol: str,
        entry_side: str,
        quantity: Decimal | None = None,
        take_profit_price: Decimal,
        trigger_type: str = "MARK_PRICE",
        position_side: str | None = None,
        close_position: bool = False,
    ) -> TakeProfitResult:
        rules = self._ex.get_symbol_rules(symbol)
        close_side = _opposite_side(entry_side)
        tp = clamp_price(take_profit_price, rules.tick_size, rules.price_precision)
        
        qty = Decimal(0)
        if not close_position:
            if quantity is None:
                 raise ValueError("若不启用 close_position，必须提供 quantity")
            qty = clamp_qty(quantity, rules.step_size, rules.quantity_precision)
            if qty <= 0:
                raise RuntimeError("数量异常")

        order_params: dict[str, Any] = {
            "symbol": symbol,
            "side": close_side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": str(tp),
            "workingType": trigger_type,
        }

        try:
            if close_position:
                resp = call_with_retry(
                    lambda: self._client.futures_create_order(
                        **order_params,
                        closePosition=True,
                    )
                )
            else:
                resp = call_with_retry(
                    lambda: self._client.futures_create_order(
                        **order_params,
                        quantity=str(qty),
                        reduceOnly=True,
                    )
                )
            tp_order_id = int(resp.get("orderId"))
            return TakeProfitResult(
                symbol=symbol,
                side=close_side,
                tp_order_id=tp_order_id,
                tp_price=tp,
                quantity=qty,
                close_position=close_position,
            )
        except BinanceAPIException as e:
            msg = str(getattr(e, "message", "")) or str(e)
            if _is_algo_switch_error(msg) or "-1022" in msg:
                algo_params: dict[str, Any] = {
                    "algoType": "CONDITIONAL",
                    "type": "TAKE_PROFIT_MARKET",
                    "orderType": "TAKE_PROFIT_MARKET",
                    "symbol": symbol,
                    "side": close_side,
                    "positionSide": position_side or "BOTH",
                    "triggerPrice": str(tp),
                    "workingType": trigger_type,
                    "closePosition": True,
                }
                try:
                    resp = self._create_algo_order_compat(algo_params)
                    algo_id = int(resp.get("algoId"))
                    return TakeProfitResult(
                        symbol=symbol,
                        side=close_side,
                        tp_order_id=algo_id,
                        tp_price=tp,
                        quantity=qty,
                        close_position=True,
                    )
                except BinanceAPIException as e2:
                    # 某些错误可能需要 retry 或者 closePosition=True (bool)
                    msg2 = str(getattr(e2, "message", "")) or str(e2)
                    if "Signature" in msg2 or "-1022" in msg2:
                         algo_params_bool = dict(algo_params)
                         algo_params_bool["closePosition"] = True
                         try:
                             resp = self._create_algo_order_compat(algo_params_bool)
                             algo_id = int(resp.get("algoId"))
                             return TakeProfitResult(
                                 symbol=symbol,
                                 side=close_side,
                                 tp_order_id=algo_id,
                                 tp_price=tp,
                                 quantity=qty,
                                 close_position=True,
                             )
                         except: pass

                    msg2 = str(getattr(e2, "message", "")) or str(e2)
                    if "reduceOnly" in msg2 or "-4046" in msg2:
                         algo_params_noreduce = dict(algo_params)
                         algo_params_noreduce.pop("reduceOnly", None)
                         algo_params_noreduce["closePosition"] = True
                         
                         try:
                             resp = self._create_algo_order_compat(algo_params_noreduce)
                             algo_id = int(resp.get("algoId"))
                             return TakeProfitResult(
                                 symbol=symbol,
                                 side=close_side,
                                 tp_order_id=algo_id,
                                 tp_price=tp,
                                 quantity=qty,
                                 close_position=True,
                             )
                         except: pass
                    
                    msg2 = str(getattr(e2, "message", "")) or str(e2)
                    if "positionSide" in msg2 or "Hedge" in msg2:
                        ps = position_side or _position_side_for_entry(entry_side)
                        algo_params2 = dict(algo_params)
                        algo_params2["positionSide"] = ps
                        resp = self._create_algo_order_compat(algo_params2)
                        algo_id = int(resp.get("algoId"))
                        return TakeProfitResult(
                            symbol=symbol,
                            side=close_side,
                            tp_order_id=algo_id,
                            tp_price=tp,
                            quantity=qty,
                            close_position=True,
                        )
                    raise

            if "closePosition" in msg or "ReduceOnly" in msg:
                resp = call_with_retry(
                    lambda: self._client.futures_create_order(
                        **order_params,
                        closePosition=True,
                    )
                )
                tp_order_id = int(resp.get("orderId"))
                return TakeProfitResult(
                    symbol=symbol,
                    side=close_side,
                    tp_order_id=tp_order_id,
                    tp_price=tp,
                    quantity=qty,
                    close_position=True,
                )
            raise
