from handlers.binance_client import BinanceClient
import pandas as pd

class SentimentAnalyzer:
    def __init__(self, client=None):
        self.client = client or BinanceClient()

    def check_market_sentiment(self, symbol='BTCUSDT', crash_threshold=-0.01):
        """
        检查大盘情绪 (是否暴跌)
        :param symbol: 风向标币种 (默认 BTCUSDT)
        :param crash_threshold: 暴跌阈值 (默认 -1% / 1h)
        :return: (is_safe, reason)
        """
        print(f">>> [Sentiment] 正在检查大盘 ({symbol}) 情绪...")
        
        # 获取最近 1h 的 K 线
        # 取最近 2 根：当前这根(未走完) 和 上一根
        df = self.client.get_klines(symbol, '1h', limit=5)
        
        if df is None or len(df) < 2:
            return True, "无法获取大盘数据，默认通行"
            
        # 计算最近 1 小时的涨跌幅
        current = df.iloc[-1]
        close_price = current['收盘价']
        open_price = current['开盘价']
        
        change_pct = (close_price - open_price) / open_price
        
        # 如果当前这根跌幅很大，或者连续两根都在跌
        is_safe = True
        reason = f"大盘平稳 (1h涨跌幅 {change_pct*100:.2f}%)"
        
        if change_pct < crash_threshold:
            is_safe = False
            reason = f"危险: 大盘正在暴跌 (1h跌幅 {change_pct*100:.2f}%)"
            
        # 也可以加更复杂的逻辑，比如 4h 趋势等
        print(f"   {reason}")
        return is_safe, reason

if __name__ == "__main__":
    analyzer = SentimentAnalyzer()
    analyzer.check_market_sentiment()
