
import sys
import unittest
from pathlib import Path
import pandas as pd

# 将 src 目录添加到 sys.path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_skills import Settings, create_client
from trading_skills.data_fetcher import FuturesDataFetcher

class TestDataFetcher(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        print("正在初始化测试环境...")
        cls.settings = Settings.load(ROOT)
        cls.client = create_client(cls.settings)
        cls.fetcher = FuturesDataFetcher(cls.client)
        cls.symbol = "NOMUSDT"
        print(f"测试交易对: {cls.symbol}")

    def test_01_fetch_klines(self):
        """测试获取K线数据"""
        print(f"\n[测试] 获取K线数据 ({self.symbol})...")
        df = self.fetcher.fetch_klines(self.symbol, "1m", limit=10)
        
        self.assertIsInstance(df, pd.DataFrame, "返回类型应该是 DataFrame")
        self.assertFalse(df.empty, "K线数据不应为空")
        self.assertTrue(len(df) <= 10, "返回行数不应超过limit")
        
        required_cols = [
            "open_time", "open", "high", "low", "close", "volume", 
            "close_time", "quote_volume", "trade_count", 
            "taker_buy_base_volume", "taker_buy_quote_volume", 
            "taker_sell_quote_volume", "资金净流入_估算"
        ]
        for col in required_cols:
            self.assertIn(col, df.columns, f"K线数据缺少列: {col}")
        
        print(f"成功获取 {len(df)} 条K线数据")
        print("\n[资金流入情况 (最近 5 根 K 线)]")
        print(df[["open_time", "close", "资金净流入_估算"]].tail(5).to_string(index=False))

    def test_02_fetch_order_book(self):
        """测试获取订单簿"""
        print(f"\n[测试] 获取订单簿 ({self.symbol})...")
        ob = self.fetcher.fetch_order_book(self.symbol, limit=5)
        
        self.assertIsInstance(ob, dict, "返回类型应该是 dict")
        self.assertIn("bids", ob, "订单簿应包含 bids")
        self.assertIn("asks", ob, "订单簿应包含 asks")
        
        bids = ob["bids"]
        asks = ob["asks"]
        self.assertTrue(len(bids) > 0, "买单深度不应为空")
        self.assertTrue(len(asks) > 0, "卖单深度不应为空")
        print("订单簿获取成功")

    def test_03_fetch_agg_trades(self):
        """测试获取近期成交"""
        print(f"\n[测试] 获取近期成交 ({self.symbol})...")
        df = self.fetcher.fetch_agg_trades(self.symbol, limit=10)
        
        self.assertIsInstance(df, pd.DataFrame, "返回类型应该是 DataFrame")
        self.assertFalse(df.empty, "成交数据不应为空")
        
        # 检查特定列
        if "p" in df.columns:
            self.assertIn("q", df.columns)
            self.assertIn("交易额", df.columns)
        
        print(f"成功获取 {len(df)} 条成交记录")

    def test_04_fetch_funding_rate(self):
        """测试获取资金费率"""
        print(f"\n[测试] 获取资金费率 ({self.symbol})...")
        df = self.fetcher.fetch_funding_rate(self.symbol, limit=5)
        
        self.assertIsInstance(df, pd.DataFrame, "返回类型应该是 DataFrame")
        # 资金费率可能为空（如果刚上线的币），但 BTCUSDT 肯定有
        self.assertFalse(df.empty, "资金费率数据不应为空")
        self.assertIn("fundingRate", df.columns)
        print("资金费率获取成功")

    def test_05_fetch_open_interest(self):
        """测试获取持仓量"""
        print(f"\n[测试] 获取持仓量 ({self.symbol})...")
        oi = self.fetcher.fetch_open_interest(self.symbol)
        
        self.assertIsNotNone(oi, "持仓量不应为 None")
        self.assertIsInstance(oi, float, "持仓量应该是 float")
        self.assertTrue(oi > 0, "持仓量应大于 0")
        print(f"当前持仓量: {oi}")

    def test_06_fetch_mark_price(self):
        """测试获取标记价格"""
        print(f"\n[测试] 获取标记价格 ({self.symbol})...")
        mp = self.fetcher.fetch_mark_price(self.symbol)
        
        self.assertIsInstance(mp, dict)
        self.assertIn("markPrice", mp)
        print(f"标记价格: {mp.get('markPrice')}")

    def test_07_fetch_long_short_ratio(self):
        """测试获取多空比"""
        print(f"\n[测试] 获取多空比 ({self.symbol})...")
        df = self.fetcher.fetch_global_long_short_ratio(self.symbol, limit=5)
        
        self.assertIsInstance(df, pd.DataFrame, "返回类型应该是 DataFrame")
        # 多空比数据可能为空（如果币种不支持），但如果有数据则必须包含特定列
        if not df.empty:
            self.assertIn("longShortRatio", df.columns)
            self.assertIn("longAccount", df.columns)
            self.assertIn("shortAccount", df.columns)
            print("多空比数据获取成功")
            print(df[["timestamp", "longShortRatio"]].tail(3).to_string(index=False))
        else:
            print("该币种暂无多空比数据")

    def test_08_fetch_snapshot(self):
        """测试获取全量快照"""
        print(f"\n[测试] 获取全量快照 ({self.symbol})...")
        snapshot = self.fetcher.fetch_snapshot(
            self.symbol, 
            "1m", 
            kline_limit=10,
            orderbook_limit=5,
            agg_trade_limit=10,
            funding_limit=5,
            ratio_limit=5
        )
        
        self.assertEqual(snapshot.symbol, self.symbol)
        self.assertEqual(snapshot.interval, "1m")
        self.assertFalse(snapshot.klines.empty, "快照K线不应为空")
        self.assertTrue(len(snapshot.order_book["bids"]) > 0, "快照订单簿买单不应为空")
        self.assertFalse(snapshot.agg_trades.empty, "快照成交不应为空")
        self.assertIsNotNone(snapshot.open_interest, "快照持仓量不应为空")
        # 资金费率和多空比可能视情况而定，但对象必须存在
        self.assertIsInstance(snapshot.funding_rate, pd.DataFrame)
        self.assertIsInstance(snapshot.long_short_ratio, pd.DataFrame)
        
        print("全量快照获取成功")

if __name__ == "__main__":
    unittest.main()
