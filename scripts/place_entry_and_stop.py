"""
脚本功能：开仓并自动设置止损止盈
主要作用：
1. 执行下单预检（资金、滑点等）
2. 市价开仓（支持按 USDT 金额计算数量）
3. 开仓成功后，自动挂出止损单（可按价格或比例）
4. 开仓成功后，自动挂出止盈单（可按价格或比例）
5. 如果止损/止盈下单失败，会提示手工处理
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_skills import FuturesTrader, Settings, create_client
from trading_skills.order_precheck import OrderPrecheck


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", type=str, default="XRPUSDT")
    parser.add_argument("--side", type=str, default="BUY")
    parser.add_argument("--usdt", type=str, default="20")
    parser.add_argument("--leverage", type=int, default=10)
    parser.add_argument("--slippage", type=str, default="0.002")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--no-stop", action="store_true")
    parser.add_argument("--stop-price", type=str, default="")
    parser.add_argument("--stop-ratio", type=str, default="0.01")
    parser.add_argument("--no-tp", action="store_true")
    parser.add_argument("--tp-price", type=str, default="")
    parser.add_argument("--tp-ratio", type=str, default="0.02")
    parser.add_argument("--trigger", type=str, default="MARK_PRICE")
    args = parser.parse_args()

    settings = Settings.load(ROOT)
    client = create_client(settings)
    trader = FuturesTrader(client)
    checker = OrderPrecheck(client)

    pre = checker.check_market_order_by_usdt(
        symbol=args.symbol,
        side=args.side,
        usdt_amount=Decimal(args.usdt),
        leverage=Decimal(str(args.leverage)),
        slippage_limit=Decimal(args.slippage),
    )
    print(f"预检通过：{pre.通过}")
    if pre.原因:
        print("预检原因：")
        for r in pre.原因:
            print(f"- {r}")
    print(f"标记价格：{pre.标记价格}")
    print(f"预估数量：{pre.数量}")
    print(f"预估滑点：{pre.预估滑点}")
    if pre.可用余额:
        print(f"可用余额：{pre.可用余额}")
    print(f"预估保证金：{pre.预估保证金}")

    if not pre.通过:
        return 2

    if not args.confirm:
        print("未执行真实下单：加 --confirm 才会提交订单")
        return 0

    trader.set_leverage(args.symbol, args.leverage)
    entry = trader.place_market_entry_by_usdt(
        symbol=args.symbol,
        side=args.side,
        usdt_amount=Decimal(args.usdt),
    )

    if entry.executed_qty <= 0:
        print("开仓未成交")
        return 2

    stop = None
    take_profit = None

    mark = client.futures_mark_price(symbol=args.symbol)
    mark_price = Decimal(str(mark.get("markPrice")))

    if not args.no_stop:
        if args.stop_price:
            stop_price = Decimal(args.stop_price)
        else:
            ratio = Decimal(args.stop_ratio)
            if entry.side == "BUY":
                stop_price = mark_price * (Decimal("1") - ratio)
            else:
                stop_price = mark_price * (Decimal("1") + ratio)

        try:
            stop = trader.place_stop_loss_market(
                symbol=args.symbol,
                entry_side=entry.side,
                quantity=entry.executed_qty,
                stop_price=stop_price,
                trigger_type=args.trigger,
            )
        except Exception as e:
            print(f"止损单下单失败：{e}")
            print(f"请立刻检查并手工处理仓位，或用脚本重设：python scripts/manage_protection_orders.py --symbol {args.symbol} --set-stop --stop-price {stop_price} --confirm")
            return 3

    if not args.no_tp:
        if args.tp_price:
            tp_price = Decimal(args.tp_price)
        else:
            ratio = Decimal(args.tp_ratio)
            if entry.side == "BUY":
                tp_price = mark_price * (Decimal("1") + ratio)
            else:
                tp_price = mark_price * (Decimal("1") - ratio)

        try:
            take_profit = trader.place_take_profit_market(
                symbol=args.symbol,
                entry_side=entry.side,
                quantity=entry.executed_qty,
                take_profit_price=tp_price,
                trigger_type=args.trigger,
            )
        except Exception as e:
            print(f"止盈单下单失败：{e}")
            print(f"请立刻检查并手工处理仓位，或用脚本重设：python scripts/manage_protection_orders.py --symbol {args.symbol} --set-tp --tp-price {tp_price} --confirm")
            return 3

    print(f"开仓：{entry.symbol} {entry.side} 订单号={entry.order_id} 成交数量={entry.executed_qty} 均价={entry.avg_price}")
    if stop:
        print(
            f"止损：{stop.symbol} {stop.side} 订单号={stop.stop_order_id} 止损价={stop.stop_price} 数量={stop.quantity} closePosition={stop.close_position}"
        )
    if take_profit:
        print(
            f"止盈：{take_profit.symbol} {take_profit.side} 订单号={take_profit.tp_order_id} 止盈价={take_profit.tp_price} 数量={take_profit.quantity} closePosition={take_profit.close_position}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
