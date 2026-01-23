from handlers.binance_client import BinanceClient
import pandas as pd
import time

import re

class MarketScanner:
    def __init__(self):
        self.client = BinanceClient()
        # 预编译正则，只允许大写字母和数字
        self.symbol_pattern = re.compile(r'^[A-Z0-9]+$')

    def scan_market(self, min_volume=10000000, max_spread=0.005, top_n=5):
        """
        扫描市场，筛选高波动、流动性合格的币种
        :param min_volume: 最小 24h 成交额 (USDT)
        :param max_spread: 最大盘口价差率 (Ask-Bid)/Ask
        :param top_n: 返回数量
        """
        print(">>> [Scanner] 正在获取全市场行情...")
        
        # 0. 获取允许交易的币种列表 (过滤掉停牌/下架/仅平仓的币种)
        trading_symbols = self.client.get_trading_symbols()
        if not trading_symbols:
            print(">>> [Scanner] 警告: 无法获取交易对状态列表，将跳过状态检查")
            trading_symbols = set()
        
        # 1. 获取 24h 统计数据
        tickers_24h = self.client.get_ticker_24hr()
        if not tickers_24h:
            print(">>> [Scanner] 获取 24h Ticker 失败")
            return None
            
        # 2. 获取盘口数据 (用于计算价差)
        book_tickers = self.client.get_book_tickers()
        if not book_tickers:
            print(">>> [Scanner] 获取 Book Ticker 失败")
            # 如果获取失败，降级处理，只用 24h 数据
            book_map = {}
        else:
            book_map = {t['symbol']: t for t in book_tickers}
            
        # 3. 数据处理与合并
        data_list = []
        for t in tickers_24h:
            symbol = t['symbol']
            
            # 过滤非 USDT 合约
            if not symbol.endswith('USDT'):
                continue

            # [新增] 严格过滤非法字符 (防止乱码或恶意数据)
            if not self.symbol_pattern.match(symbol):
                print(f"   [Scanner] 忽略非法交易对名称: {symbol}")
                continue

            # 过滤非交易状态的币种
            if trading_symbols and symbol not in trading_symbols:
                continue
                
            quote_vol = float(t['quoteVolume'])
            
            # 初步过滤成交额 (避免处理太多数据)
            if quote_vol < min_volume:
                continue
                
            # 计算波动率 (振幅) = (High - Low) / Low
            high = float(t['highPrice'])
            low = float(t['lowPrice'])
            if low == 0:
                continue
            amplitude = (high - low) / low
            
            # 计算价差
            spread_rate = 0.0
            book = book_map.get(symbol)
            if book:
                bid = float(book['bidPrice'])
                ask = float(book['askPrice'])
                if ask > 0:
                    spread_rate = (ask - bid) / ask
            
            # 过滤价差过大的 (流动性风险)
            if spread_rate > max_spread:
                continue
                
            data_list.append({
                'symbol': symbol,
                'price': float(t['lastPrice']),
                'change_pct': float(t['priceChangePercent']),
                'amplitude_pct': amplitude * 100,
                'volume': quote_vol,
                'spread_pct': spread_rate * 100
            })
            
        if not data_list:
            print(">>> [Scanner] 未找到符合条件的币种")
            return None
            
        # 4. 排序：按波动幅度降序
        df = pd.DataFrame(data_list)
        df = df.sort_values(by='amplitude_pct', ascending=False)
        
        # 打印 Top 榜单
        print(f"\n【市场扫描结果 (Vol>{min_volume//10000}w, Spread<{max_spread*100}%)】")
        print(df[['symbol', 'price', 'change_pct', 'amplitude_pct', 'spread_pct']].head(top_n).to_string(index=False))
        
        return df.head(top_n)

if __name__ == "__main__":
    scanner = MarketScanner()
    scanner.scan_market()
