# 交易执行模块
from binance.client import Client
from binance.enums import *
from loguru import logger
import config

class TradeExecutor:
    def __init__(self):
        self.client = None
    
    def connect(self):
        try:
            self.client = Client(
                config.API_KEY, 
                config.API_SECRET,
                requests_params={'proxies': config.PROXY} if config.PROXY['http'] else None
            )
            # 测试连接
            self.client.get_server_time()
            logger.info("币安 API 连接成功")
        except Exception as e:
            logger.error(f"连接失败: {e}")

    def get_position(self, symbol=config.SYMBOL):
        try:
            info = self.client.futures_position_information(symbol=symbol)
            if info:
                # 返回字典包含更多信息
                return {
                    "amt": float(info[0]['positionAmt']),
                    "entryPrice": float(info[0]['entryPrice']),
                    "unRealizedProfit": float(info[0]['unRealizedProfit'])
                }
            return {"amt": 0.0, "entryPrice": 0.0, "unRealizedProfit": 0.0}
        except Exception as e:
            logger.error(f"获取持仓失败: {e}")
            return {"amt": 0.0, "entryPrice": 0.0, "unRealizedProfit": 0.0}

    def get_balance(self):
        try:
            account = self.client.futures_account_balance()
            for asset in account:
                if asset['asset'] == 'USDT':
                    return float(asset['balance'])
            return 0.0
        except Exception as e:
            logger.error(f"获取余额失败: {e}")
            return 0.0

    def get_symbol_info(self, symbol=config.SYMBOL):
        try:
            info = self.client.futures_exchange_info()
            for s in info['symbols']:
                if s['symbol'] == symbol:
                    return s
            return None
        except Exception as e:
            logger.error(f"获取交易对信息失败: {e}")
            return None

    def place_order(self, side, quantity, symbol=config.SYMBOL):
        try:
            # 确定买卖方向
            order_side = SIDE_BUY if side == 1 else SIDE_SELL
            
            order = self.client.futures_create_order(
                symbol=symbol,
                side=order_side,
                type=ORDER_TYPE_MARKET,
                quantity=quantity
            )
            logger.info(f"下单成功: {order_side} {quantity} {symbol}")
            return order
        except Exception as e:
            logger.error(f"下单失败: {e}")
            return None

    def get_klines(self, symbol=config.SYMBOL, interval=config.TIMEFRAME, limit=100):
        try:
            klines = self.client.futures_klines(symbol=symbol, interval=interval, limit=limit)
            return klines
        except Exception as e:
            logger.error(f"获取 K 线数据失败: {e}")
            return []

    def get_orderbook(self, symbol=config.SYMBOL, limit=config.DEPTH_LIMIT):
        try:
            depth = self.client.futures_order_book(symbol=symbol, limit=limit)
            return depth
        except Exception as e:
            logger.error(f"获取深度数据失败: {e}")
            return None
