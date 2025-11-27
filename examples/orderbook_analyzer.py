import pandas as pd
import numpy as np
from typing import List, Dict, Tuple
import matplotlib.pyplot as plt

class OrderBookAnalyzer:
    def __init__(self, symbol: str = "MINAUSDT"):
        self.symbol = symbol
        self.bids = []  # 格式: [[price, quantity], ...] 价格降序
        self.asks = []  # 格式: [[price, quantity], ...] 价格升序
        self.price_history = []
        self.analysis_results = {}
        
    def update_orderbook(self, bids: List, asks: List):
        """更新订单簿数据"""
        self.bids = bids[:1000]  # 确保不超过1000个深度
        self.asks = asks[:1000]
        
        # 记录当前中间价
        if bids and asks:
            best_bid = float(bids[0][0])
            best_ask = float(asks[0][0])
            mid_price = (best_bid + best_ask) / 2
            self.price_history.append(mid_price)
            
            # 保持价格历史长度
            if len(self.price_history) > 1000:
                self.price_history.pop(0)
    
    def get_basic_metrics(self) -> Dict:
        """获取基础指标"""
        if not self.bids or not self.asks:
            return {}
            
        best_bid = float(self.bids[0][0])
        best_bid_qty = float(self.bids[0][1])
        best_ask = float(self.asks[0][0])
        best_ask_qty = float(self.asks[0][1])
        
        spread = best_ask - best_bid
        spread_percentage = (spread / best_bid) * 100
        
        return {
            'best_bid': best_bid,
            'best_bid_qty': best_bid_qty,
            'best_ask': best_ask,
            'best_ask_qty': best_ask_qty,
            'spread': spread,
            'spread_percentage': spread_percentage,
            'mid_price': (best_bid + best_ask) / 2
        }
    
    def calculate_market_imbalance(self, depth_levels: int = 20) -> float:
        """计算市场不平衡度"""
        if not self.bids or not self.asks:
            return 0
            
        # 计算指定深度内的总买卖量
        bid_volume = sum(float(bid[1]) for bid in self.bids[:depth_levels])
        ask_volume = sum(float(ask[1]) for ask in self.asks[:depth_levels])
        
        if bid_volume + ask_volume == 0:
            return 0
            
        imbalance = (bid_volume - ask_volume) / (bid_volume + ask_volume)
        return imbalance
    
    def find_key_levels(self, top_n: int = 5) -> Dict:
        """找出关键支撑阻力位"""
        if not self.bids or not self.asks:
            return {}
            
        # 找出买单量最大的价格水平（支撑位）
        bid_levels = sorted(self.bids, key=lambda x: float(x[1]), reverse=True)[:top_n]
        support_levels = [(float(level[0]), float(level[1])) for level in bid_levels]
        
        # 找出卖单量最大的价格水平（阻力位）
        ask_levels = sorted(self.asks, key=lambda x: float(x[1]), reverse=True)[:top_n]
        resistance_levels = [(float(level[0]), float(level[1])) for level in ask_levels]
        
        return {
            'support_levels': support_levels,
            'resistance_levels': resistance_levels
        }
    
    def calculate_market_depth(self, price_range_percentage: float = 1.0) -> Dict:
        """计算市场深度"""
        if not self.bids or not self.asks:
            return {}
            
        mid_price = self.get_basic_metrics()['mid_price']
        price_range = mid_price * price_range_percentage / 100
        
        # 计算买卖盘在价格区间内的总量
        bid_depth = sum(float(bid[1]) for bid in self.bids 
                       if mid_price - price_range <= float(bid[0]) <= mid_price)
        ask_depth = sum(float(ask[1]) for ask in self.asks 
                       if mid_price <= float(ask[0]) <= mid_price + price_range)
        
        return {
            'bid_depth': bid_depth,
            'ask_depth': ask_depth,
            'depth_imbalance': (bid_depth - ask_depth) / (bid_depth + ask_depth) if (bid_depth + ask_depth) > 0 else 0
        }
    
    def detect_large_orders(self, threshold: float = 10000) -> Dict:
        """检测大额订单（墙壁单）"""
        large_bids = [(float(bid[0]), float(bid[1])) for bid in self.bids if float(bid[1]) >= threshold]
        large_asks = [(float(ask[0]), float(ask[1])) for ask in self.asks if float(ask[1]) >= threshold]
        
        return {
            'large_bids': large_bids,
            'large_asks': large_asks,
            'large_order_ratio': len(large_bids + large_asks) / (len(self.bids) + len(self.asks))
        }
    
    def analyze_liquidity_gaps(self, gap_threshold: float = 0.001) -> List:
        """分析流动性缺口"""
        gaps = []
        
        # 分析卖盘缺口
        for i in range(len(self.asks) - 1):
            price_diff = float(self.asks[i+1][0]) - float(self.asks[i][0])
            if price_diff > gap_threshold:
                gaps.append({
                    'type': 'ask_gap',
                    'start_price': float(self.asks[i][0]),
                    'end_price': float(self.asks[i+1][0]),
                    'gap_size': price_diff
                })
        
        # 分析买盘缺口
        for i in range(len(self.bids) - 1):
            price_diff = float(self.bids[i][0]) - float(self.bids[i+1][0])
            if price_diff > gap_threshold:
                gaps.append({
                    'type': 'bid_gap',
                    'start_price': float(self.bids[i][0]),
                    'end_price': float(self.bids[i+1][0]),
                    'gap_size': price_diff
                })
        
        return gaps
    
    def calculate_volatility(self, period: int = 20) -> float:
        """计算价格波动率"""
        if len(self.price_history) < period:
            return 0
            
        recent_prices = self.price_history[-period:]
        returns = np.diff(np.log(recent_prices))
        volatility = np.std(returns) * np.sqrt(365 * 24 * 60)  # 年化波动率
        
        return volatility
    
    def generate_trading_signals(self) -> Dict:
        """生成交易信号"""
        basic_metrics = self.get_basic_metrics()
        if not basic_metrics:
            return {'signal': 'HOLD', 'confidence': 0}
        
        # 获取各项指标
        imbalance = self.calculate_market_imbalance()
        key_levels = self.find_key_levels()
        market_depth = self.calculate_market_depth()
        large_orders = self.detect_large_orders()
        volatility = self.calculate_volatility()
        
        current_price = basic_metrics['mid_price']
        
        # 信号评分系统
        bullish_signals = 0
        bearish_signals = 0
        total_signals = 0
        
        # 1. 市场不平衡度信号
        if imbalance > 0.1:
            bullish_signals += 1
        elif imbalance < -0.1:
            bearish_signals += 1
        total_signals += 1
        
        # 2. 关键价位信号
        if key_levels['support_levels']:
            nearest_support = key_levels['support_levels'][0][0]
            support_distance = (current_price - nearest_support) / current_price
            if support_distance < 0.01:  # 接近强支撑
                bullish_signals += 1
        
        if key_levels['resistance_levels']:
            nearest_resistance = key_levels['resistance_levels'][0][0]
            resistance_distance = (nearest_resistance - current_price) / current_price
            if resistance_distance < 0.01:  # 接近强阻力
                bearish_signals += 1
        total_signals += 1
        
        # 3. 市场深度信号
        if market_depth['depth_imbalance'] > 0.1:
            bullish_signals += 1
        elif market_depth['depth_imbalance'] < -0.1:
            bearish_signals += 1
        total_signals += 1
        
        # 4. 大单信号
        if len(large_orders['large_bids']) > len(large_orders['large_asks']):
            bullish_signals += 1
        elif len(large_orders['large_asks']) > len(large_orders['large_bids']):
            bearish_signals += 1
        total_signals += 1
        
        # 生成最终信号
        bullish_score = bullish_signals / total_signals
        bearish_score = bearish_signals / total_signals
        
        if bullish_score > 0.6 and bullish_score > bearish_score:
            signal = 'BUY'
            confidence = bullish_score
        elif bearish_score > 0.6 and bearish_score > bullish_score:
            signal = 'SELL'
            confidence = bearish_score
        else:
            signal = 'HOLD'
            confidence = max(bullish_score, bearish_score)
        
        return {
            'signal': signal,
            'confidence': confidence,
            'bullish_score': bullish_score,
            'bearish_score': bearish_score,
            'details': {
                'imbalance': imbalance,
                'key_levels': key_levels,
                'market_depth': market_depth,
                'large_orders': large_orders,
                'volatility': volatility
            }
        }
    
    def comprehensive_analysis(self) -> Dict:
        """执行全面分析"""
        return {
            'basic_metrics': self.get_basic_metrics(),
            'market_imbalance': self.calculate_market_imbalance(),
            'key_levels': self.find_key_levels(),
            'market_depth': self.calculate_market_depth(),
            'large_orders': self.detect_large_orders(),
            'liquidity_gaps': self.analyze_liquidity_gaps(),
            'volatility': self.calculate_volatility(),
            'trading_signals': self.generate_trading_signals()
        }
    
    def plot_orderbook(self, levels: int = 50):
        """绘制订单簿图表"""
        if not self.bids or not self.asks:
            print("没有数据可绘制")
            return
            
        plt.figure(figsize=(12, 8))
        
        # 提取数据
        bid_prices = [float(bid[0]) for bid in self.bids[:levels]]
        bid_volumes = [float(bid[1]) for bid in self.bids[:levels]]
        ask_prices = [float(ask[0]) for ask in self.asks[:levels]]
        ask_volumes = [float(ask[1]) for ask in self.asks[:levels]]
        
        # 绘制买盘
        plt.barh(bid_prices, bid_volumes, height=0.0001, color='green', alpha=0.7, label='Bids')
        # 绘制卖盘
        plt.barh(ask_prices, ask_volumes, height=0.0001, color='red', alpha=0.7, label='Asks')
        
        plt.xlabel('Volume')
        plt.ylabel('Price')
        plt.title(f'Order Book - {self.symbol}')
        plt.legend()
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.show()
    
    def print_analysis_report(self):
        """打印分析报告"""
        analysis = self.comprehensive_analysis()
        
        print("=" * 50)
        print(f"订单簿分析报告 - {self.symbol}")
        print("=" * 50)
        
        # 基础指标
        basic = analysis['basic_metrics']
        print(f"\n📊 基础指标:")
        print(f"   最佳买价: {basic.get('best_bid', 0):.6f}")
        print(f"   最佳卖价: {basic.get('best_ask', 0):.6f}")
        print(f"   价差: {basic.get('spread', 0):.6f} ({basic.get('spread_percentage', 0):.2f}%)")
        
        # 市场不平衡
        print(f"\n⚖️  市场不平衡度: {analysis['market_imbalance']:.4f}")
        
        # 关键价位
        key_levels = analysis['key_levels']
        print(f"\n🎯 关键支撑位:")
        for price, volume in key_levels.get('support_levels', [])[:3]:
            print(f"   {price:.6f} - 挂单量: {volume:,.0f}")
        
        print(f"\n🎯 关键阻力位:")
        for price, volume in key_levels.get('resistance_levels', [])[:3]:
            print(f"   {price:.6f} - 挂单量: {volume:,.0f}")
        
        # 交易信号
        signals = analysis['trading_signals']
        print(f"\n🚦 交易信号: {signals['signal']}")
        print(f"   置信度: {signals['confidence']:.2%}")
        print(f"   多头分数: {signals['bullish_score']:.2f}")
        print(f"   空头分数: {signals['bearish_score']:.2f}")
        
        # 大额订单
        large_orders = analysis['large_orders']
        print(f"\n🏦 大额订单:")
        print(f"   买单数量: {len(large_orders['large_bids'])}")
        print(f"   卖单数量: {len(large_orders['large_asks'])}")
        
        print("=" * 50)

# 使用示例
if __name__ == "__main__":
    # 模拟数据示例
    analyzer = OrderBookAnalyzer("MINAUSDT")
    
    # 模拟订单簿数据
    sample_bids = [
        [0.11770, 119], [0.11769, 642], [0.11768, 75], [0.11766, 754],
        [0.11765, 849], [0.11764, 325], [0.11763, 152], [0.11762, 499]
    ]
    
    sample_asks = [
        [0.11771, 298], [0.11772, 770], [0.11773, 901], [0.11774, 1592],
        [0.11775, 1082], [0.11776, 199], [0.11777, 4308], [0.11778, 140]
    ]
    
    # 更新数据并分析
    analyzer.update_orderbook(sample_bids, sample_asks)
    
    # 生成报告
    analyzer.print_analysis_report()
    
    # 获取详细分析
    full_analysis = analyzer.comprehensive_analysis()
    print("\n详细分析:", full_analysis)
    
    # 绘制订单簿
    # analyzer.plot_orderbook()