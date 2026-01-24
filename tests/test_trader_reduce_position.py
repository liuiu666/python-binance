import unittest

from handlers.trader import TradeExecutor


class FakeClientInner:
    def __init__(self):
        self.cancel_all_calls = 0
        self.cancel_calls = []

    def futures_cancel_all_open_orders(self, symbol=None, recvWindow=None):
        self.cancel_all_calls += 1

    def futures_cancel_order(self, symbol=None, orderId=None):
        self.cancel_calls.append((symbol, orderId))


class FakeClient:
    def __init__(
        self,
        order_types=None,
        open_orders=None,
        positions=None,
        account_balance=10000.0,
        ask_price=10.0,
        bid_price=9.9,
        ticker_price=10.0,
    ):
        self.client = FakeClientInner()
        self.place_orders = []
        self.order_types = order_types if order_types is not None else ["STOP_MARKET", "STOP", "LIMIT"]
        self.open_orders = open_orders if open_orders is not None else []
        self.positions = positions if positions is not None else []
        self.account_balance = account_balance
        self.ask_price = ask_price
        self.bid_price = bid_price
        self.ticker_price = ticker_price

    def get_symbol_filters(self, symbol):
        return {
            "step_size": 0.1,
            "min_qty": 0.1,
            "quantity_precision": 3,
            "tick_size": 0.01,
            "price_precision": 2,
            "min_notional": 5.0,
            "order_types": self.order_types,
        }

    def place_order(self, **kwargs):
        self.place_orders.append(kwargs)
        return {"orderId": len(self.place_orders)}

    def get_open_orders(self, symbol=None):
        return list(self.open_orders)

    def get_current_positions(self):
        return list(self.positions)

    def get_account_info(self):
        return {"balance": {"available_balance": self.account_balance}}

    def change_leverage(self, symbol, leverage):
        return True

    def get_book_tickers(self, symbol=None):
        return {"symbol": symbol, "askPrice": str(self.ask_price), "bidPrice": str(self.bid_price)}

    def get_symbol_ticker(self, symbol):
        return {"symbol": symbol, "price": str(self.ticker_price)}


class TradeExecutorOrderTests(unittest.TestCase):
    def test_execute_trade_places_limit_order(self):
        client = FakeClient()
        trader = TradeExecutor(client=client, state_manager=None)

        order = trader.execute_trade("SPACEUSDT", "BUY", amount_usdt=100, leverage=5, slippage=0.0)

        self.assertIsNotNone(order)
        self.assertEqual(len(client.place_orders), 1)
        first = client.place_orders[0]
        self.assertEqual(first.get("order_type"), "LIMIT")
        self.assertEqual(first.get("side"), "BUY")
        self.assertFalse(first.get("reduce_only", False))

    def test_place_stop_protection_close_position(self):
        client = FakeClient()
        trader = TradeExecutor(client=client, state_manager=None)

        ok = trader._place_stop_protection("SPACEUSDT", "BUY", stop_price=9.0, quantity=None, cancel_existing=False)

        self.assertTrue(ok)
        self.assertEqual(len(client.place_orders), 1)
        first = client.place_orders[0]
        self.assertEqual(first.get("order_type"), "STOP_MARKET")
        self.assertTrue(first.get("close_position"))

    def test_place_stop_protection_reduce_only(self):
        client = FakeClient(positions=[{"symbol": "SPACEUSDT", "amount": 10}])
        trader = TradeExecutor(client=client, state_manager=None)

        def place_order_override(**kwargs):
            client.place_orders.append(kwargs)
            if kwargs.get("close_position"):
                return None
            return {"orderId": len(client.place_orders)}

        client.place_order = place_order_override

        ok = trader._place_stop_protection("SPACEUSDT", "BUY", stop_price=9.0, quantity=None, cancel_existing=False)

        self.assertTrue(ok)
        self.assertGreaterEqual(len(client.place_orders), 2)
        reduce_orders = [o for o in client.place_orders if o.get("reduce_only")]
        self.assertTrue(len(reduce_orders) >= 1)

    def test_place_split_take_profit_places_limit_orders(self):
        client = FakeClient()
        trader = TradeExecutor(client=client, state_manager=None)
        filters = client.get_symbol_filters("SPACEUSDT")

        ok = trader._place_split_take_profit(
            symbol="SPACEUSDT",
            open_side="BUY",
            entry_price=10.0,
            take_profit=12.0,
            total_quantity=9.0,
            filters=filters,
        )

        self.assertTrue(ok)
        self.assertGreaterEqual(len(client.place_orders), 1)
        for order in client.place_orders:
            self.assertEqual(order.get("order_type"), "LIMIT")
            self.assertTrue(order.get("reduce_only"))

    def test_update_stop_loss_places_order(self):
        client = FakeClient(positions=[{"symbol": "SPACEUSDT", "amount": 10}])
        trader = TradeExecutor(client=client, state_manager=None)

        ok = trader.update_stop_loss("SPACEUSDT", "BUY", new_price=9.0)

        self.assertTrue(ok)
        self.assertGreaterEqual(len(client.place_orders), 1)

    def test_update_take_profit_places_order(self):
        client = FakeClient(positions=[{"symbol": "SPACEUSDT", "amount": 10}])
        trader = TradeExecutor(client=client, state_manager=None)

        ok = trader.update_take_profit("SPACEUSDT", "BUY", new_price=12.0)

        self.assertTrue(ok)
        self.assertEqual(len(client.place_orders), 1)
        order = client.place_orders[0]
        self.assertEqual(order.get("order_type"), "LIMIT")
        self.assertTrue(order.get("reduce_only"))

    def test_close_position_places_market_reduce_only(self):
        client = FakeClient(positions=[{"symbol": "SPACEUSDT", "amount": 10, "side": "BUY"}])
        trader = TradeExecutor(client=client, state_manager=None)

        ok = trader.close_position("SPACEUSDT")

        self.assertTrue(ok)
        self.assertEqual(len(client.place_orders), 1)
        order = client.place_orders[0]
        self.assertEqual(order.get("order_type"), "MARKET")
        self.assertTrue(order.get("reduce_only"))

    def test_increase_position_places_market_and_stop(self):
        client = FakeClient()
        trader = TradeExecutor(client=client, state_manager=None)
        position = {"symbol": "SPACEUSDT", "side": "BUY", "amount": 10}

        ok = trader.increase_position(position, amount_usdt=100, current_price=10.0, atr=0.5)

        self.assertTrue(ok)
        self.assertGreaterEqual(len(client.place_orders), 2)
        market_orders = [o for o in client.place_orders if o.get("order_type") == "MARKET"]
        stop_orders = [o for o in client.place_orders if o.get("order_type") == "STOP_MARKET"]
        self.assertEqual(len(market_orders), 1)
        self.assertEqual(len(stop_orders), 1)
        self.assertTrue(stop_orders[0].get("reduce_only"))

    def test_reduce_position_places_market_and_updates_stop(self):
        client = FakeClient(positions=[{"symbol": "SPACEUSDT", "amount": 10}])
        trader = TradeExecutor(client=client, state_manager=None)
        position = {
            "symbol": "SPACEUSDT",
            "side": "BUY",
            "amount": 10.0,
            "entry_price": 9.0,
        }

        pnl = trader.reduce_position(position, 0.3, current_price=10.0)

        self.assertIsNotNone(pnl)
        self.assertEqual(client.client.cancel_all_calls, 0)
        self.assertGreaterEqual(len(client.place_orders), 2)
        market_orders = [o for o in client.place_orders if o.get("order_type") == "MARKET"]
        self.assertTrue(len(market_orders) >= 1)


if __name__ == "__main__":
    unittest.main()
