import requests
from binance.client import Client
from binance.enums import *
import os
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

class BinanceClient:
    def __init__(self):
        # 获取环境变量配置
        self.api_key = os.getenv('BINANCE_API_KEY')
        self.api_secret = os.getenv('BINANCE_API_SECRET')
        self.http_proxy = os.getenv('HTTP_PROXY')
        self.https_proxy = os.getenv('HTTPS_PROXY')

        # 配置代理
        requests_params = None
        if self.http_proxy:
            print(f">>> [System] 检测到代理配置: {self.http_proxy}")
            requests_params = {
                'proxies': {
                    'http': self.http_proxy,
                    'https': self.https_proxy or self.http_proxy
                }
            }
        else:
            print(">>> [System] 未检测到代理配置，将直连币安 API")

        # 初始化币安客户端
        self.client = Client(
            self.api_key, 
            self.api_secret, 
            requests_params=requests_params
        )

    def get_account_info(self):
        """
        获取账户详细信息 (余额和持仓)
        """
        try:
            # 获取账户信息
            info = self.client.futures_account()
            
            # 整理余额
            usdt_balance = next((item for item in info['assets'] if item['asset'] == 'USDT'), None)
            balance_data = {
                'total_wallet_balance': float(usdt_balance['walletBalance']) if usdt_balance else 0.0,
                'total_margin_balance': float(usdt_balance['marginBalance']) if usdt_balance else 0.0,
                'available_balance': float(usdt_balance['availableBalance']) if usdt_balance else 0.0,
                'unrealized_pnl': float(usdt_balance['unrealizedProfit']) if usdt_balance else 0.0
            }

            # 整理持仓 (只保留有持仓的)
            positions = []
            for pos in info['positions']:
                amt = float(pos['positionAmt'])
                if amt != 0:
                    positions.append({
                        'symbol': pos['symbol'],
                        'amount': amt,
                        'entry_price': float(pos['entryPrice']),
                        'unrealized_pnl': float(pos['unrealizedProfit']),
                        'leverage': int(pos['leverage']),
                        'side': 'LONG' if amt > 0 else 'SHORT'
                    })
            
            return {
                'balance': balance_data,
                'positions': positions
            }
        except Exception as e:
            print(f"获取账户信息失败: {str(e)}")
            return None

    def change_leverage(self, symbol, leverage):
        """
        调整合约杠杆
        :param symbol: 交易对
        :param leverage: 杠杆倍数 (int)
        """
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=leverage)
            print(f"已将 {symbol} 杠杆调整为 {leverage}x")
            return True
        except Exception as e:
            print(f"调整杠杆失败: {symbol} -> {leverage}x, 错误: {str(e)}")
            return False

    def get_symbol_filters(self, symbol):
        """
        获取交易对的精度过滤器信息 (Price & Quantity Precision)
        """
        try:
            info = self.client.futures_exchange_info()
            target = next((s for s in info['symbols'] if s['symbol'] == symbol), None)
            
            if not target:
                return None
                
            filters = {
                'price_precision': target['pricePrecision'],
                'quantity_precision': target['quantityPrecision'],
                'min_qty': 0.0,
                'max_qty': 0.0,
                'step_size': 0.0,
                'tick_size': 0.0,
                'min_notional': 5.0 # 默认值
            }
            
            for f in target['filters']:
                if f['filterType'] == 'LOT_SIZE':
                    filters['min_qty'] = float(f['minQty'])
                    filters['max_qty'] = float(f['maxQty'])
                    filters['step_size'] = float(f['stepSize'])
                elif f['filterType'] == 'PRICE_FILTER':
                    filters['tick_size'] = float(f['tickSize'])
                elif f['filterType'] == 'MIN_NOTIONAL':
                    # 注意：binance futures 的 MIN_NOTIONAL 字段通常叫 'notional'
                    # 但在 filters 中可能叫 'notional' 或 'minNotional'
                    # 具体取决于 API 版本，这里做兼容处理
                    if 'notional' in f:
                        filters['min_notional'] = float(f['notional'])
                    elif 'minNotional' in f:
                        filters['min_notional'] = float(f['minNotional'])
                    
            return filters
        except Exception as e:
            print(f"获取交易规则失败: {str(e)}")
            return None

    def get_balance(self):
        """
        获取当前账户合约余额
        """
        try:
            # 获取合约账户余额
            # futures_account_balance 返回的是一个列表
            balances = self.client.futures_account_balance()
            
            # 查找 USDT 余额
            for asset in balances:
                if asset['asset'] == 'USDT':
                    # balance: 总余额
                    # availableBalance: 可用余额 (注意：这里取 withdrawAvailable 可能更准确，或者 availableBalance)
                    # python-binance 返回的字段通常是 string
                    total_balance = float(asset.get('balance', 0))
                    available_balance = float(asset.get('availableBalance', 0))
                    # 已用余额 = 总余额 - 可用余额
                    used_balance = total_balance - available_balance
                    
                    return {
                        '总权益': total_balance,
                        '可用余额': available_balance,
                        '已用余额': used_balance
                    }
            
            # 如果没找到 USDT
            return {
                '总权益': 0.0,
                '可用余额': 0.0,
                '已用余额': 0.0
            }
            
        except Exception as e:
            print(f"获取余额失败: {str(e)}")
            return None

    def get_current_positions(self):
        """
        获取当前持有的仓位 (只返回非零持仓)
        :return: list of dict
        """
        try:
            account = self.client.futures_account()
            positions = []
            for pos in account['positions']:
                amt = float(pos['positionAmt'])
                if amt != 0:
                    positions.append({
                        'symbol': pos['symbol'],
                        'amount': amt,
                        'entry_price': float(pos['entryPrice']),
                        'unrealized_pnl': float(pos['unrealizedProfit']),
                        'leverage': int(pos['leverage']),
                        'side': 'BUY' if amt > 0 else 'SELL'
                    })
            return positions
        except Exception as e:
            print(f"获取持仓失败: {str(e)}")
            return []

    def get_trading_symbols(self):
        """
        获取所有正在交易的合约交易对
        """
        try:
            # 获取交易所信息
            exchange_info = self.client.futures_exchange_info()
            
            trading_symbols = []
            for symbol_info in exchange_info['symbols']:
                # 筛选状态为 TRADING (正在交易) 的交易对
                if symbol_info['status'] == 'TRADING':
                    trading_symbols.append(symbol_info['symbol'])
            
            return trading_symbols
        except Exception as e:
            print(f"获取交易对失败: {str(e)}")
            return []

    def get_klines(self, symbol, interval, limit=100):
        """
        获取 K 线数据并转换为 DataFrame
        :param symbol: 交易对
        :param interval: 时间间隔 (例如 '1h', '4h', '1d')
        :param limit: 获取数量
        """
        try:
            import pandas as pd
            # 获取 K 线数据
            klines = self.client.futures_klines(symbol=symbol, interval=interval, limit=limit)
            
            # 转换为 DataFrame
            df = pd.DataFrame(klines, columns=[
                '开盘时间', '开盘价', '最高价', '最低价', '收盘价', '成交量',
                '收盘时间', '成交额', '成交笔数', '主动买入成交量', '主动买入成交额', '忽略'
            ])
            
            # 类型转换
            df['开盘时间'] = pd.to_datetime(df['开盘时间'], unit='ms')
            df['收盘时间'] = pd.to_datetime(df['收盘时间'], unit='ms')
            
            numeric_cols = ['开盘价', '最高价', '最低价', '收盘价', '成交量']
            # 使用 apply 默认 axis=0 进行列批量转换，比 axis=1 更安全且快
            df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric)
            
            return df
        except Exception as e:
            # 避免打印英文错误信息，使用通用中文提示
            print(f"获取 K 线失败: {symbol}")
            return None

    def place_order(self, symbol, side, quantity, order_type='MARKET', price=None, stop_price=None, reduce_only=False, close_position=False):
        """
        下单
        :param symbol: 交易对
        :param side: 方向 ('BUY' 或 'SELL')
        :param quantity: 数量 (如果 close_position=True 则忽略)
        :param order_type: 订单类型
        :param price: 价格
        :param stop_price: 触发价格
        :param reduce_only: 是否只减仓
        :param close_position: 是否全平 (仅用于 STOP_MARKET/TAKE_PROFIT_MARKET)
        """
        try:
            params = {
                'symbol': symbol,
                'side': side,
                'type': order_type,
            }
            
            if not close_position:
                params['quantity'] = quantity
            
            if order_type == 'LIMIT':
                if price is None:
                    print("限价单必须指定价格")
                    return None
                params['timeInForce'] = 'GTC'
                params['price'] = price
                
            elif order_type in ['STOP_MARKET', 'TAKE_PROFIT_MARKET']:
                if stop_price is None:
                    print(f"{order_type} 必须指定触发价格")
                    return None
                params['stopPrice'] = stop_price
                params['workingType'] = 'MARK_PRICE' # 显式指定触发类型
                
                if close_position:
                    params['closePosition'] = True
                    # closePosition=True 时不能传 quantity，也不能传 reduceOnly (隐含为 True)
                    if 'quantity' in params:
                        del params['quantity']
                elif reduce_only:
                     params['reduceOnly'] = True
            
            elif reduce_only:
                # MARKET 或 LIMIT 单的 reduceOnly
                params['reduceOnly'] = True
                
            print(f">>> [DEBUG] 下单参数: {params}")
            order = self.client.futures_create_order(**params)
            print(f"下单成功: {side} {symbol} {quantity if not close_position else 'ALL'} Type={order_type}")
            return order
        except Exception as e:
            print(f"下单失败: {symbol} - {str(e)}")
            # 尝试打印更详细的错误信息
            if hasattr(e, 'message'):
                print(f"   Error Message: {e.message}")
            if hasattr(e, 'code'):
                print(f"   Error Code: {e.code}")
            return None


    def get_trading_symbols(self):
        """
        获取所有状态为 TRADING 的交易对
        """
        try:
            info = self.client.futures_exchange_info()
            return {s['symbol'] for s in info['symbols'] if s['status'] == 'TRADING'}
        except Exception as e:
            print(f"获取交易规则失败: {str(e)}")
            return set()

    def get_ticker_24hr(self):
        """
        获取所有交易对的 24小时 统计数据
        """
        try:
            # 获取所有 ticker 信息
            tickers = self.client.futures_ticker()
            return tickers
        except Exception as e:
            print(f"获取 24h Ticker 失败: {str(e)}")
            return []

    def get_symbol_ticker(self, symbol):
        """
        获取交易对的最新成交价 (替代盘口数据)
        """
        try:
            return self.client.futures_symbol_ticker(symbol=symbol)
        except Exception as e:
            print(f"获取 Ticker 失败: {str(e)}")
            return None

    def get_book_tickers(self, symbol=None):
        """
        获取交易对的最新买一卖一价 (用于计算滑点和价差)
        :param symbol: 指定交易对 (可选)
        """
        try:
            # futures_orderbook_ticker 不传 symbol 默认返回所有 (list)
            # 传 symbol 返回单个 (dict)
            return self.client.futures_orderbook_ticker(symbol=symbol)
        except Exception as e:
            print(f"获取 Book Ticker 失败: {str(e)}")
            return None if symbol else []


    def get_money_flow(self, symbol, period='1h', limit=1):
        """
        获取资金流向数据（基于合约主动买卖量）
        period: 时间周期，如 '5m', '15m', '1h', '4h', '1d'
        """
        try:
            # 使用 futures_taker_longshort_ratio 获取买卖量数据
            # 虽然名字叫 Ratio，但返回数据包含 buyVol 和 sellVol
            taker_stats = self.client.futures_taker_longshort_ratio(
                symbol=symbol,
                period=period,
                limit=limit
            )
            
            if not taker_stats:
                return None
                
            # 取最近的一个周期数据
            latest_stat = taker_stats[-1]
            
            # 注意：这里的 buyVol 和 sellVol 通常是数量（Cont），不是金额？
            # 或者是基础货币数量？文档说是 "Volume"
            # 如果是数量，需要乘以价格估算金额，或者直接展示数量
            # 通常 takerlongshortRatio 返回的是 "buyVol": "xxxx", "sellVol": "xxxx"
            
            buy_vol = float(latest_stat['buyVol'])
            sell_vol = float(latest_stat['sellVol'])
            buy_sell_ratio = float(latest_stat['buySellRatio'])
            
            # 估算净流入（这里直接用 Volume 差值）
            net_inflow = buy_vol - sell_vol
            
            return {
                '周期': period,
                '主动买入量': buy_vol,
                '主动卖出量': sell_vol,
                '净流入量': net_inflow,
                '买卖比': buy_sell_ratio
            }
        except Exception as e:
            print(f"获取 {symbol} 资金流向失败: {str(e)}")
            return None
