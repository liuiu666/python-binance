from handlers.binance_client import BinanceClient
import pandas as pd
import time

import re

class MarketScanner:
    def __init__(self, client=None):
        self.client = client or BinanceClient()
        # 预编译正则，只允许大写字母和数字
        self.symbol_pattern = re.compile(r'^[A-Z0-9]+$')

    def scan_market(self, min_volume=10000000, max_spread=0.005, top_n=5, min_amplitude_pct=0.0, max_change_pct=None, prefer_cheap=False, prefer_new=False, new_days=30):
        """
        扫描市场，筛选高波动、流动性合格的币种
        优化逻辑：先取流动性前 N*4，再按波动率排序，确保选到的币种既活跃又有深度
        :param min_volume: 最小 24h 成交额 (USDT)
        :param max_spread: 最大盘口价差率 (Ask-Bid)/Ask
        :param top_n: 返回数量
        :param min_amplitude_pct: 最小 24h 振幅%
        :param max_change_pct: 最大 24h 涨跌幅% (绝对值)
        """
        print(">>> [Scanner] 正在获取全市场行情...")
        
        exchange_info = self.client.get_exchange_info()
        trading_symbols = set()
        onboard_map = {}
        if exchange_info and exchange_info.get('symbols'):
            for s in exchange_info['symbols']:
                if s.get('status') == 'TRADING':
                    trading_symbols.add(s.get('symbol'))
                onboard_ts = s.get('onboardDate')
                if onboard_ts and s.get('symbol'):
                    onboard_map[s.get('symbol')] = int(onboard_ts)
        else:
            print(">>> [Scanner] 警告: 无法获取交易对状态列表，将跳过状态检查")
        
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
            amplitude_pct = amplitude * 100
            change_pct = float(t['priceChangePercent'])
            
            # 计算价差
            spread_rate = 0.0
            book = book_map.get(symbol)
            if book:
                bid = float(book['bidPrice'])
                ask = float(book['askPrice'])
                if ask > 0:
                    spread_rate = (ask - bid) / ask
            
            if min_amplitude_pct and amplitude_pct < min_amplitude_pct:
                continue

            if max_change_pct is not None and abs(change_pct) > max_change_pct:
                continue

            # 过滤价差过大的 (流动性风险)
            if spread_rate > max_spread:
                continue
                
            data_list.append({
                'symbol': symbol,
                'price': float(t['lastPrice']),
                'change_pct': change_pct,
                'amplitude_pct': amplitude_pct,
                'volume': quote_vol,
                'spread_pct': spread_rate * 100,
                'onboard_ts': onboard_map.get(symbol, 0)
            })
            
        if not data_list:
            print(">>> [Scanner] 未找到符合条件的币种")
            return None
            
        df = pd.DataFrame(data_list)
        if prefer_new:
            now_ms = int(time.time() * 1000)
            df['days_since'] = df['onboard_ts'].apply(lambda ts: (now_ms - ts) / 86400000 if ts and ts > 0 else None)
            df['is_new'] = df['days_since'].apply(lambda d: d is not None and d <= new_days)
            
        # --- 优化后的排序逻辑 ---
        # 1. 优先按成交额降序，筛选出流动性最好的头部币种 (例如前 N*5)
        # 这样可以避免选到成交额刚过线但深度很差的小币
        pool_size = top_n * 5
        df_liquid = df.sort_values(by='volume', ascending=False).head(pool_size)
        
        # 2. 在高流动性池中，按振幅/波动率降序排列
        # 如果开启了 prefer_new，则新币优先
        sort_cols = []
        sort_orders = []
        
        if prefer_new:
            sort_cols.append('is_new')
            sort_orders.append(False)
            
        if prefer_cheap:
            sort_cols.append('price')
            sort_orders.append(True)
            
        sort_cols.append('amplitude_pct')
        sort_orders.append(False)
        
        df_sorted = df_liquid.sort_values(by=sort_cols, ascending=sort_orders)
        
        # 打印 Top 榜单
        print(f"\n【市场扫描结果 (Vol>{min_volume//10000}w, Spread<{max_spread*100}%, Amp>{min_amplitude_pct}%, Pool={pool_size})】")
        print(df_sorted[['symbol', 'price', 'change_pct', 'amplitude_pct', 'spread_pct', 'volume']].head(top_n).to_string(index=False))
        
        return df_sorted.head(top_n)

if __name__ == "__main__":
    scanner = MarketScanner()
    scanner.scan_market()
