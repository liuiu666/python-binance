"""
脚本功能：下单预检查
主要作用：
1. 在不实际下单的情况下，检查交易条件
2. 计算给定 USDT 金额对应的合约数量
3. 预估滑点和成交均价
4. 检查账户余额是否足够支付保证金
5. 输出详细的检查报告，用于风控判断
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_skills import OrderPrecheck, Settings, create_client


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", type=str, default="XRPUSDT")
    parser.add_argument("--side", type=str, default="BUY")
    parser.add_argument("--usdt", type=str, default="50")
    parser.add_argument("--leverage", type=str, default="10")
    parser.add_argument("--slippage", type=str, default="0.001")
    parser.add_argument("--orderbook-limit", type=int, default=100)
    args = parser.parse_args()

    settings = Settings.load(ROOT)
    client = create_client(settings)
    checker = OrderPrecheck(client)
    result = checker.check_market_order_by_usdt(
        symbol=args.symbol,
        side=args.side,
        usdt_amount=Decimal(args.usdt),
        leverage=Decimal(args.leverage),
        slippage_limit=Decimal(args.slippage),
        orderbook_limit=args.orderbook_limit,
    )

    print(f"通过：{result.通过}")
    if result.原因:
        print("原因：")
        for r in result.原因:
            print(f"- {r}")
    print(f"标记价格：{result.标记价格}")
    print(f"数量：{result.数量}")
    print(f"预估滑点：{result.预估滑点}")
    print(f"预估成交均价：{result.预估成交均价}")
    if result.可用余额:
        print(f"可用余额：{result.可用余额}")
    print(f"预估保证金：{result.预估保证金}")

    return 0 if result.通过 else 2


if __name__ == "__main__":
    raise SystemExit(main())

