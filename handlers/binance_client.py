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
            requests_params = {
                'proxies': {
                    'http': self.http_proxy,
                    'https': self.https_proxy or self.http_proxy
                }
            }

        # 初始化币安客户端
        self.client = Client(
            self.api_key, 
            self.api_secret, 
            requests_params=requests_params
        )

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

    def get_funding_info(self, symbol):
        """
        获取指定交易对的资金数据（标记价格、资金费率等）
        """
        try:
            # 获取最新的资金费率信息（包含标记价格、下次资金费率收取时间等）
            # futures_mark_price 返回单个字典或列表
            funding_info = self.client.futures_mark_price(symbol=symbol)
            
            return {
                '交易对': funding_info.get('symbol'),
                '标记价格': float(funding_info.get('markPrice', 0)),
                '当前资金费率': float(funding_info.get('lastFundingRate', 0)),
                '下次资金时间': funding_info.get('nextFundingTime'), # 时间戳
                '预估资金费率': float(funding_info.get('estimatedSettlePrice', 0)) if 'estimatedSettlePrice' in funding_info else 0 # 注意：API可能不直接返回预估费率，通常是 lastFundingRate
            }
        except Exception as e:
            print(f"获取 {symbol} 资金数据失败: {str(e)}")
            return None

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
