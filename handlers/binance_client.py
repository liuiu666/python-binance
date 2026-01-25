import os
import time
from binance.client import Client
from binance.enums import *
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
        self.debug = os.getenv('DEBUG') == '1'
        
        # 缓存交易所信息
        self.exchange_info_cache = None
        self.exchange_info_last_update = 0

        # 配置代理
        self.requests_params = {'timeout': 30}
        if self.http_proxy:
            print(f">>> [System] 检测到代理配置: {self.http_proxy}")
            self.requests_params['proxies'] = {
                'http': self.http_proxy,
                'https': self.https_proxy or self.http_proxy
            }
        else:
            print(">>> [System] 未检测到代理配置，将直连币安 API")

        # 初始化币安客户端
        try:
            self._init_client()
        except Exception as e:
            print(f">>> [System] 致命错误: 无法连接币安 API (Timeout/Proxy Error)")
            print(f"    详情: {e}")
            raise e

    def _init_client(self):
        """初始化或重置 API 客户端连接"""
        self.client = Client(
            self.api_key, 
            self.api_secret, 
            requests_params=self.requests_params
        )
        # 禁用默认的 ping
        self.client.ping = lambda: {}

    def _execute_with_retry(self, func, *args, **kwargs):
        """通用重试包装器，处理 SSL/连接错误"""
        max_retries = 3
        last_exception = None
        
        for i in range(max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_exception = e
                # 检查是否为网络连接类错误
                err_str = str(e)
                is_conn_err = 'SSLError' in err_str or 'Connection' in err_str or 'EOF' in err_str or 'Reset' in err_str
                
                if is_conn_err and i < max_retries - 1:
                    print(f">>> [System] 网络抖动 (尝试 {i+1}/{max_retries})，正在重连...")
                    try:
                        self._init_client()
                        time.sleep(1) 
                    except:
                        pass
                    continue
                # 如果不是连接错误，或者是最后一次重试，则抛出
                raise e

    def get_account_info(self):
        """
        获取账户详细信息 (余额和持仓)
        """
        try:
            # 获取账户信息
            info = self._execute_with_retry(self.client.futures_account)
            
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

    def _get_cached_exchange_info(self):
        """获取缓存的交易所信息 (缓存1小时)"""
        now = time.time()
        if self.exchange_info_cache and (now - self.exchange_info_last_update < 3600):
            return self.exchange_info_cache
        
        try:
            # 更新缓存
            info = self.client.futures_exchange_info()
            self.exchange_info_cache = info
            self.exchange_info_last_update = now
            return info
        except Exception as e:
            print(f"更新交易所信息缓存失败: {e}")
            return self.exchange_info_cache # 如果更新失败，返回旧缓存

    def get_symbol_filters(self, symbol):
        """
        获取交易对的精度过滤器信息 (Price & Quantity Precision)
        """
        try:
            info = self._get_cached_exchange_info()
            if not info:
                # 缓存为空且更新失败，尝试直接调用一次
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
                'min_notional': 5.0, # 默认值
                'order_types': target.get('orderTypes', []) # [Add] 获取支持的订单类型
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
                        'side': 'BUY' if amt > 0 else 'SELL',
                        'update_time': int(pos['updateTime'])
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
            exchange_info = self._execute_with_retry(self.client.futures_exchange_info)
            
            trading_symbols = []
            for symbol_info in exchange_info['symbols']:
                # 筛选状态为 TRADING (正在交易) 的交易对
                if symbol_info['status'] == 'TRADING':
                    trading_symbols.append(symbol_info['symbol'])
            
            return trading_symbols
        except Exception as e:
            print(f"获取交易对失败: {str(e)}")
            return []

    def get_exchange_info(self):
        try:
            return self._execute_with_retry(self.client.futures_exchange_info)
        except Exception as e:
            print(f"获取交易所信息失败: {str(e)}")
            return None

    def get_open_orders(self, symbol=None):
        """
        获取当前挂单
        """
        try:
            return self._execute_with_retry(self.client.futures_get_open_orders, symbol=symbol)
        except Exception as e:
            print(f"获取挂单失败: {str(e)}")
            return []

    def get_funding_rate(self, symbol):
        """
        获取当前资金费率
        """
        try:
            funding = self.client.futures_mark_price(symbol=symbol)
            # lastFundingRate 是最近一期的费率
            return float(funding['lastFundingRate'])
        except Exception as e:
            # 忽略 -4108 (币种不可用) 错误，避免刷屏
            if hasattr(e, 'code') and int(e.code) == -4108:
                return 0.0
            print(f"获取费率失败 {symbol}: {e}")
            return 0.0

    def get_open_interest(self, symbol):
        """
        获取未平仓合约量 (Open Interest)
        """
        try:
            oi = self.client.futures_open_interest(symbol=symbol)
            return float(oi['openInterest'])
        except Exception as e:
            # 忽略 -4108 (币种不可用) 错误，避免刷屏
            if hasattr(e, 'code') and int(e.code) == -4108:
                return 0.0
            print(f"获取持仓量失败 {symbol}: {e}")
            return 0.0

    def get_money_flow(self, symbol, period='1h', limit=5):
        """
        获取资金流向数据 (基于 K 线主动买入量估算)
        """
        try:
            # 复用 get_klines 获取数据
            df = self.get_klines(symbol, period, limit=limit)
            if df is None or df.empty:
                return None
            
            # 计算净主动买入量
            # 主动买入成交量 (Taker Buy Volume) 是买方主动吃单的量
            # 主动卖出成交量 = 总成交量 - 主动买入成交量
            # 净流入 = 主动买入 - 主动卖出 = 2 * 主动买入 - 总成交量
            
            total_vol = df['成交量'].sum()
            buy_vol = df['主动买入成交量'].sum()
            sell_vol = total_vol - buy_vol
            net_inflow = 2 * buy_vol - total_vol
            
            return {
                '净流入量': float(net_inflow),
                '主动买入量': float(buy_vol),
                '主动卖出量': float(sell_vol),
                '买卖比': float(buy_vol / sell_vol) if sell_vol > 0 else 10.0,
                '主动买入占比': float(buy_vol / total_vol) if total_vol > 0 else 0.5
            }
        except Exception as e:
            print(f"计算资金流失败 {symbol}: {e}")
            return None

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
            klines = self._execute_with_retry(self.client.futures_klines, symbol=symbol, interval=interval, limit=limit)
            
            # 转换为 DataFrame
            df = pd.DataFrame(klines, columns=[
                '开盘时间', '开盘价', '最高价', '最低价', '收盘价', '成交量',
                '收盘时间', '成交额', '成交笔数', '主动买入成交量', '主动买入成交额', '忽略'
            ])
            
            # 类型转换
            df['开盘时间'] = pd.to_datetime(df['开盘时间'], unit='ms')
            df['收盘时间'] = pd.to_datetime(df['收盘时间'], unit='ms')
            
            numeric_cols = ['开盘价', '最高价', '最低价', '收盘价', '成交量', '成交额', '成交笔数', '主动买入成交量', '主动买入成交额']
            # 使用 apply 默认 axis=0 进行列批量转换，比 axis=1 更安全且快
            df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric)
            
            return df
        except Exception as e:
            # 避免打印英文错误信息，使用通用中文提示
            print(f"获取 K 线失败: {symbol} - {str(e)}")
            return None

    def place_order(self, symbol, side, quantity=None, order_type='MARKET', price=None, stop_price=None, reduce_only=False, close_position=False, working_type='MARK_PRICE', price_protect=False, **kwargs):
        """
        下单
        """
        try:
            params = {
                'symbol': symbol,
                'side': side,
                'type': order_type,
            }
            
            # 支持额外参数 (如 timeInForce)
            if kwargs:
                # 转换参数名为驼峰式 (例如 time_in_force -> timeInForce)
                for k, v in kwargs.items():
                    # 简单处理：如果是 time_in_force，转为 timeInForce
                    if k == 'time_in_force':
                        params['timeInForce'] = v
                    else:
                        params[k] = v
            
            # [Fix -1021]
            params['recvWindow'] = 10000 

            if stop_price is not None:
                params['stopPrice'] = str(stop_price)
                if working_type:
                    params['workingType'] = working_type
                if price_protect:
                    params['priceProtect'] = 'true'

            if close_position:
                params['closePosition'] = 'true'
            else:
                if quantity is None:
                    print(f"错误: 非全平模式下必须指定数量")
                    return None
                params['quantity'] = quantity

            if order_type in ['LIMIT', 'STOP', 'TAKE_PROFIT']:
                if price is None:
                    # [关键修正] 对于 STOP 单，如果目的是触发止损单（Stop Loss），有时 price 不是必须的（如果是 STOP_MARKET）
                    # 但如果是 LIMIT 类型的 STOP，必须有 price
                    # 这里我们宽容处理：如果 type 是 STOP 且 price 为 None，打印警告但不阻断（交给 API 报错）
                    # 或者如果是 STOP 但没有 price，可能是调用方想发 STOP_MARKET 但传错了 type
                    print(f"警告: {order_type} 通常需要指定价格 (price)，当前为 None")
                    # return None # 暂时注释掉 return，让 API 决定是否报错
                else:
                    params['timeInForce'] = 'GTC'
                    params['price'] = str(price)

            if reduce_only and not close_position:
                params['reduceOnly'] = 'true'

            if self.debug:
                print(f">>> [DEBUG] 标准下单参数: {params}")
            return self.client.futures_create_order(**params)
                
        except Exception as e:
            # 忽略 -4120 (Order type not supported) 和 -4136 (Target strategy invalid) 错误
            error_code = getattr(e, 'code', 0)
            
            if int(error_code) in [-4120, -4136]:
                if self.debug:
                    print(f"   [API信息] 订单类型被拒绝 (Code {error_code})，尝试备用方案")
                return None

            print(f"下单失败: {symbol} - {str(e)}")
            return None


    def get_ticker_24hr(self):
        """
        获取所有交易对的 24小时 统计数据
        """
        try:
            # 获取所有 ticker 信息
            tickers = self._execute_with_retry(self.client.futures_ticker)
            return tickers
        except Exception as e:
            print(f"获取 24h Ticker 失败: {str(e)}")
            return []


    def get_symbol_ticker(self, symbol):
        """
        获取交易对的最新成交价 (替代盘口数据)
        """
        try:
            return self._execute_with_retry(self.client.futures_symbol_ticker, symbol=symbol)
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
            return self._execute_with_retry(self.client.futures_orderbook_ticker, symbol=symbol)
        except Exception as e:
            print(f"获取 Book Ticker 失败: {str(e)}")
            return None if symbol else []

    def get_all_open_orders(self):
        """
        获取当前账户所有挂单 (不指定 symbol)
        """
        try:
            return self._execute_with_retry(self.client.futures_get_open_orders)
        except Exception as e:
            print(f"获取所有挂单失败: {str(e)}")
            return []
