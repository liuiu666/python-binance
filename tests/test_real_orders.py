import os
import time
import unittest

from handlers.binance_client import BinanceClient
from handlers.trader import TradeExecutor


class RealOrderTests(unittest.TestCase):
    @unittest.skipUnless(os.getenv("RUN_REAL_ORDERS") == "1", "未开启真实下单测试")
    def test_real_order_flow(self):
        symbol = os.getenv("REAL_TEST_SYMBOL")
        side = os.getenv("REAL_TEST_SIDE")
        amount_text = os.getenv("REAL_TEST_AMOUNT_USDT")
        leverage_text = os.getenv("REAL_TEST_LEVERAGE")

        if not symbol:
            self.skipTest("未设置 REAL_TEST_SYMBOL")
        if side not in ["BUY", "SELL"]:
            self.skipTest("未设置 REAL_TEST_SIDE")
        if not amount_text:
            self.skipTest("未设置 REAL_TEST_AMOUNT_USDT")
        if not leverage_text:
            self.skipTest("未设置 REAL_TEST_LEVERAGE")

        try:
            amount_usdt = float(amount_text)
            leverage = int(leverage_text)
        except Exception:
            self.skipTest("金额或杠杆参数不合法")

        client = BinanceClient()
        trader = TradeExecutor(client=client, state_manager=None)

        ticker = client.get_symbol_ticker(symbol)
        if not ticker or float(ticker.get("price", 0)) <= 0:
            self.skipTest("无法获取行情价格")

        price = float(ticker["price"])
        if side == "BUY":
            stop_loss = price * 0.99
            take_profit = price * 1.01
        else:
            stop_loss = price * 1.01
            take_profit = price * 0.99

        order = trader.execute_trade(
            symbol=symbol,
            side=side,
            amount_usdt=amount_usdt,
            leverage=leverage,
            slippage=0.001,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

        self.assertIsNotNone(order)
        time.sleep(2)
        closed = trader.close_position(symbol)
        self.assertTrue(closed)


if __name__ == "__main__":
    unittest.main()
