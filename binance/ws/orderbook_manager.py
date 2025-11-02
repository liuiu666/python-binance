"""
1000档订单簿管理器 - 集成到python-binance库

提供简单易用的1000档订单簿实时数据获取功能，支持代理连接。
"""

import asyncio
import time
import warnings
from typing import Dict, List, Optional, Callable, Any, Union
from binance.async_client import AsyncClient
from binance.ws.streams import BinanceSocketManager
from binance.ws.reconnecting_websocket import ReconnectingWebsocket


class OrderBookManager:
    """
    Binance期货1000档订单簿管理器
    专门用于Binance期货合约的实时1000档订单簿数据管理，
    支持WebSocket增量更新和定期全量刷新。
    """
    
    def __init__(
        self,
        symbol: str = "BTCUSDT",
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        proxy_url: Optional[str] = None,
        update_callback: Optional[Callable] = None
    ):
        """
        初始化期货订单簿管理器
        
        Args:
            symbol: 期货交易对符号，默认BTCUSDT
            api_key: Binance API密钥
            api_secret: Binance API密钥
            proxy_url: 代理URL，如果需要的话
            update_callback: 订单簿更新回调函数
        """
        self.symbol = symbol.upper()
        self.api_key = api_key
        self.api_secret = api_secret
        self.proxy_url = proxy_url
        self.update_callback = update_callback
        
        # 客户端和连接管理
        self.client: Optional[AsyncClient] = None
        self.bm: Optional[BinanceSocketManager] = None
        self.ws_conn: Optional[ReconnectingWebsocket] = None
        
        # 订单簿数据 - 优化为字典结构以便快速更新
        self.orderbook = {
            'symbol': self.symbol,
            'bids_dict': {},  # {price: quantity} - 买单字典
            'asks_dict': {},  # {price: quantity} - 卖单字典
            'bids': [],       # [[price, quantity], ...] - 排序后的买单列表（降序）
            'asks': [],       # [[price, quantity], ...] - 排序后的卖单列表（升序）
            'last_update_id': 0,
            'timestamp': None
        }
        
        # 统计信息
        self.update_count = 0
        self.start_time = None
        self.last_refresh_time = None
        self.is_running = False
        
        # 抑制deprecation警告
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        
    async def initialize(self) -> bool:
        """
        初始化客户端连接
        
        Returns:
            bool: 初始化是否成功
        """
        try:
            # 创建异步客户端
            client_kwargs = {}
            if self.api_key and self.api_secret:
                client_kwargs['api_key'] = self.api_key
                client_kwargs['api_secret'] = self.api_secret
            if self.proxy_url:
                client_kwargs['https_proxy'] = self.proxy_url
                
            self.client = await AsyncClient.create(**client_kwargs)
            
            # 创建socket管理器
            self.bm = BinanceSocketManager(self.client)
            
            # 测试期货API连接
            server_time = await self.client.futures_time()
            print(f"✅ 期货API连接成功，服务器时间: {server_time}")
            
            return True
            
        except Exception as e:
            print(f"❌ 初始化失败: {e}")
            return False
    
    async def get_initial_snapshot(self) -> bool:
        """
        获取初始1000档订单簿快照
        
        Returns:
            bool: 获取是否成功
        """
        try:
            if not self.client:
                print("❌ 客户端未初始化")
                return False
                
            # 获取1000档订单簿 - 使用期货API
            depth = await self.client.futures_order_book(symbol=self.symbol, limit=1000)
            
            # 构建买单字典和列表（买单按价格降序排列）
            bids_dict = {}
            bids_list = []
            for bid in depth['bids']:
                price, quantity = float(bid[0]), float(bid[1])
                bids_dict[price] = quantity
                bids_list.append([price, quantity])
            
            # 构建卖单字典和列表（卖单按价格升序排列）
            asks_dict = {}
            asks_list = []
            for ask in depth['asks']:
                price, quantity = float(ask[0]), float(ask[1])
                asks_dict[price] = quantity
                asks_list.append([price, quantity])
            
            # 更新订单簿数据
            self.orderbook.update({
                'bids_dict': bids_dict,
                'asks_dict': asks_dict,
                'bids': bids_list,
                'asks': asks_list,
                'last_update_id': depth['lastUpdateId'],
                'timestamp': time.time()
            })
            
            self.last_refresh_time = time.time()
            print(f"✅ 获取初始订单簿成功: {len(self.orderbook['bids'])}档买单, {len(self.orderbook['asks'])}档卖单")
            
            return True
            
        except Exception as e:
            print(f"❌ 获取初始订单簿失败: {e}")
            return False
    
    async def start_websocket(self) -> bool:
        """
        启动WebSocket连接接收增量更新
        
        Returns:
            bool: 启动是否成功
        """
        try:
            if not self.bm:
                print("❌ Socket管理器未初始化")
                return False
                
            # 创建期货深度更新WebSocket连接
            self.ws_conn = self.bm.futures_depth_socket(symbol=self.symbol)
            
            print(f"✅ 期货WebSocket连接已建立: {self.symbol}")
            return True
            
        except Exception as e:
            print(f"❌ WebSocket连接失败: {e}")
            return False
    
    def process_depth_update(self, msg: Dict[str, Any]):
        """
        处理WebSocket深度更新消息 - 优化版本
        
        Args:
            msg: WebSocket消息
        """
        try:
            if msg.get('e') != 'depthUpdate':
                return
                
            # 检查更新ID连续性
            first_update_id = msg.get('U')
            final_update_id = msg.get('u')
            
            if first_update_id <= self.orderbook['last_update_id'] + 1 <= final_update_id:
                # 标记是否需要重建排序列表
                bids_changed = False
                asks_changed = False
                
                # 更新买单字典
                for bid in msg.get('b', []):
                    price, quantity = float(bid[0]), float(bid[1])
                    if quantity == 0:
                        # 删除订单
                        if price in self.orderbook['bids_dict']:
                            del self.orderbook['bids_dict'][price]
                            bids_changed = True
                    else:
                        # 新增或更新订单
                        self.orderbook['bids_dict'][price] = quantity
                        bids_changed = True
                
                # 更新卖单字典
                for ask in msg.get('a', []):
                    price, quantity = float(ask[0]), float(ask[1])
                    if quantity == 0:
                        # 删除订单
                        if price in self.orderbook['asks_dict']:
                            del self.orderbook['asks_dict'][price]
                            asks_changed = True
                    else:
                        # 新增或更新订单
                        self.orderbook['asks_dict'][price] = quantity
                        asks_changed = True
                
                # 重建排序列表（仅在有变化时）
                if bids_changed:
                    self._rebuild_bids_list()
                if asks_changed:
                    self._rebuild_asks_list()
                
                # 更新元数据
                self.orderbook['last_update_id'] = final_update_id
                self.orderbook['timestamp'] = time.time()
                self.update_count += 1
                
                # 调用回调函数
                if self.update_callback:
                    self.update_callback(self.orderbook)
                    
        except Exception as e:
            print(f"❌ 处理深度更新失败: {e}")
    
    def _rebuild_bids_list(self):
        """重建买单排序列表（按价格降序）"""
        self.orderbook['bids'] = sorted(
            [[price, quantity] for price, quantity in self.orderbook['bids_dict'].items()],
            key=lambda x: x[0],
            reverse=True
        )[:1000]  # 保持1000档
    
    def _rebuild_asks_list(self):
        """重建卖单排序列表（按价格升序）"""
        self.orderbook['asks'] = sorted(
            [[price, quantity] for price, quantity in self.orderbook['asks_dict'].items()],
            key=lambda x: x[0],
            reverse=False
        )[:1000]  # 保持1000档
    
    def validate_orderbook(self) -> Dict[str, Any]:
        """
        验证订单簿数据完整性
        
        Returns:
            Dict: 验证结果
        """
        validation_result = {
            'is_valid': True,
            'errors': [],
            'warnings': [],
            'statistics': {}
        }
        
        try:
            # 检查数据结构完整性
            if not self.orderbook.get('bids_dict') or not self.orderbook.get('asks_dict'):
                validation_result['errors'].append("订单簿字典数据为空")
                validation_result['is_valid'] = False
            
            if not self.orderbook.get('bids') or not self.orderbook.get('asks'):
                validation_result['errors'].append("订单簿列表数据为空")
                validation_result['is_valid'] = False
            
            # 检查字典和列表数据一致性
            bids_dict_count = len(self.orderbook['bids_dict'])
            bids_list_count = len(self.orderbook['bids'])
            asks_dict_count = len(self.orderbook['asks_dict'])
            asks_list_count = len(self.orderbook['asks'])
            
            if bids_dict_count != bids_list_count:
                validation_result['warnings'].append(
                    f"买单字典({bids_dict_count})和列表({bids_list_count})数量不一致"
                )
            
            if asks_dict_count != asks_list_count:
                validation_result['warnings'].append(
                    f"卖单字典({asks_dict_count})和列表({asks_list_count})数量不一致"
                )
            
            # 检查排序正确性
            if self.orderbook['bids']:
                for i in range(len(self.orderbook['bids']) - 1):
                    if self.orderbook['bids'][i][0] <= self.orderbook['bids'][i + 1][0]:
                        validation_result['errors'].append("买单价格排序错误（应为降序）")
                        validation_result['is_valid'] = False
                        break
            
            if self.orderbook['asks']:
                for i in range(len(self.orderbook['asks']) - 1):
                    if self.orderbook['asks'][i][0] >= self.orderbook['asks'][i + 1][0]:
                        validation_result['errors'].append("卖单价格排序错误（应为升序）")
                        validation_result['is_valid'] = False
                        break
            
            # 检查价格合理性
            if self.orderbook['bids'] and self.orderbook['asks']:
                best_bid = self.orderbook['bids'][0][0]
                best_ask = self.orderbook['asks'][0][0]
                
                if best_bid >= best_ask:
                    validation_result['errors'].append(
                        f"价格异常：最佳买价({best_bid}) >= 最佳卖价({best_ask})"
                    )
                    validation_result['is_valid'] = False
                
                spread_percentage = ((best_ask - best_bid) / best_bid) * 100
                if spread_percentage > 1.0:  # 价差超过1%
                    validation_result['warnings'].append(
                        f"价差过大：{spread_percentage:.4f}%"
                    )
            
            # 统计信息
            validation_result['statistics'] = {
                'bids_count': bids_dict_count,
                'asks_count': asks_dict_count,
                'total_levels': bids_dict_count + asks_dict_count,
                'last_update_id': self.orderbook.get('last_update_id', 0),
                'timestamp': self.orderbook.get('timestamp'),
                'update_count': self.update_count
            }
            
        except Exception as e:
            validation_result['errors'].append(f"验证过程出错: {e}")
            validation_result['is_valid'] = False
        
        return validation_result
    
    def sync_dict_and_list(self):
        """
        同步字典和列表数据（修复不一致问题）
        """
        try:
            # 重建排序列表
            self._rebuild_bids_list()
            self._rebuild_asks_list()
            
            print("✅ 订单簿数据同步完成")
            
        except Exception as e:
            print(f"❌ 数据同步失败: {e}")
    
    async def run(self, duration: Optional[int] = None) -> bool:
        """
        运行订单簿管理器
        
        Args:
            duration: 运行时长（秒），None表示无限运行
            
        Returns:
            bool: 运行是否成功
        """
        try:
            # 初始化
            if not await self.initialize():
                return False
                
            # 获取初始快照
            if not await self.get_initial_snapshot():
                return False
                
            # 启动WebSocket
            if not await self.start_websocket():
                return False
            
            if not self.ws_conn:
                print("❌ WebSocket连接未建立")
                return False
            
            self.is_running = True
            self.start_time = time.time()
            
            print(f"🚀 订单簿管理器启动成功: {self.symbol}")
            
            # 运行主循环
            async with self.ws_conn as ws:
                end_time = time.time() + duration if duration else None
                
                while self.is_running:
                    try:
                        # 接收WebSocket消息
                        msg = await ws.recv()
                        
                        # 处理深度更新
                        self.process_depth_update(msg)
                        
                        # 检查运行时长
                        if end_time and time.time() >= end_time:
                            break
                            
                    except asyncio.TimeoutError:
                        # 超时继续循环
                        continue
                    except Exception as e:
                        print(f"❌ 接收消息失败: {e}")
                        break
            
            return True
            
        except Exception as e:
            print(f"❌ 运行失败: {e}")
            return False
        finally:
            self.is_running = False
            await self.close()
    
    async def close(self):
        """关闭连接和清理资源"""
        try:
            self.is_running = False
            
            if self.ws_conn:
                await self.ws_conn.__aexit__(None, None, None)
                self.ws_conn = None
                
            if self.client:
                await self.client.close_connection()
                self.client = None
                
            print("✅ 资源清理完成")
            
        except Exception as e:
            print(f"❌ 清理资源失败: {e}")
    
    def get_best_prices(self) -> Dict[str, Optional[float]]:
        """
        获取最优买卖价格
        
        Returns:
            Dict: {'bid': 最优买价, 'ask': 最优卖价}
        """
        return {
            'bid': self.orderbook['bids'][0][0] if self.orderbook['bids'] else None,
            'ask': self.orderbook['asks'][0][0] if self.orderbook['asks'] else None
        }
    
    def get_depth_summary(self, levels: int = 10) -> Dict[str, List[List[float]]]:
        """
        获取订单簿深度摘要
        
        Args:
            levels: 返回的档位数量
            
        Returns:
            Dict: {'bids': [[price, quantity], ...], 'asks': [[price, quantity], ...]}
        """
        return {
            'bids': self.orderbook['bids'][:levels],
            'asks': self.orderbook['asks'][:levels]
        }
    
    def get_statistics(self) -> Dict[str, Any]:
        """
        获取订单簿统计信息
        
        Returns:
            Dict: 统计信息
        """
        runtime = time.time() - self.start_time if self.start_time else 0
        
        # 计算总交易量
        total_bid_volume = sum(float(qty) for _, qty in self.orderbook['bids'])
        total_ask_volume = sum(float(qty) for _, qty in self.orderbook['asks'])
        
        # 计算价差
        best_prices = self.get_best_prices()
        spread = 0
        spread_percentage = 0
        if best_prices['bid'] and best_prices['ask']:
            spread = best_prices['ask'] - best_prices['bid']
            spread_percentage = (spread / best_prices['ask']) * 100
        
        return {
            'symbol': self.symbol,
            'runtime_seconds': runtime,
            'update_count': self.update_count,
            'update_frequency': self.update_count / runtime if runtime > 0 else 0,
            'bid_levels': len(self.orderbook['bids']),
            'ask_levels': len(self.orderbook['asks']),
            'total_bid_volume': total_bid_volume,
            'total_ask_volume': total_ask_volume,
            'spread': spread,
            'spread_percentage': spread_percentage,
            'last_update_time': self.orderbook['timestamp'],
            'is_running': self.is_running
        }
    
    def get_volume_by_price_range(self, min_price: float, max_price: float) -> Dict[str, Union[float, str]]:
        """
        按价格区间统计订单量（高效算法）
        
        Args:
            min_price: 最低价格
            max_price: 最高价格
            
        Returns:
            Dict: 包含买单和卖单在指定价格区间内的总量
        """
        bid_volume = 0.0
        ask_volume = 0.0
        
        # 买单统计（按价格降序排列，从高到低遍历）
        # 当价格低于区间起点时停止，减少无效计算
        for price, quantity in self.orderbook['bids']:
            if price < min_price:
                break  # 利用排序特性提前终止
            if price <= max_price:
                bid_volume += float(quantity)
        
        # 卖单统计（按价格升序排列，从低到高遍历）
        # 当价格高于区间终点时停止
        for price, quantity in self.orderbook['asks']:
            if price > max_price:
                break  # 利用排序特性提前终止
            if price >= min_price:
                ask_volume += float(quantity)
        
        return {
            'bid_volume': bid_volume,
            'ask_volume': ask_volume,
            'total_volume': bid_volume + ask_volume,
            'price_range': f"{min_price}-{max_price}"
        }
    
    def get_volume_distribution(self, intervals: int = 10) -> Dict[str, Any]:
        """
        获取订单簿价格分布统计
        
        Args:
            intervals: 分割区间数量
            
        Returns:
            Dict: 价格分布统计
        """
        if not self.orderbook['bids'] or not self.orderbook['asks']:
            return {'error': '订单簿数据为空'}
        
        # 获取价格范围
        highest_bid = float(self.orderbook['bids'][0][0])
        lowest_ask = float(self.orderbook['asks'][0][0])
        
        # 计算合理的价格范围（以最佳价格为中心）
        mid_price = (highest_bid + lowest_ask) / 2
        price_range = abs(lowest_ask - highest_bid) * 10  # 扩大10倍范围
        
        min_price = mid_price - price_range / 2
        max_price = mid_price + price_range / 2
        
        # 计算区间大小
        interval_size = price_range / intervals
        
        distribution = []
        
        for i in range(intervals):
            interval_min = min_price + i * interval_size
            interval_max = min_price + (i + 1) * interval_size
            
            volume_data = self.get_volume_by_price_range(interval_min, interval_max)
            
            distribution.append({
                'interval': i + 1,
                'price_range': f"{interval_min:.2f}-{interval_max:.2f}",
                'bid_volume': volume_data['bid_volume'],
                'ask_volume': volume_data['ask_volume'],
                'total_volume': volume_data['total_volume']
            })
        
        return {
            'intervals': intervals,
            'mid_price': mid_price,
            'price_range': f"{min_price:.2f}-{max_price:.2f}",
            'distribution': distribution
        }

    def get_orderbook(self) -> Dict[str, Any]:
        """
        获取完整订单簿数据
        
        Returns:
            Dict: 完整订单簿
        """
        return self.orderbook.copy()


# 便捷函数
async def create_orderbook_manager(
    symbol: str = "BTCUSDT",
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
    proxy_url: Optional[str] = None,
    **kwargs
) -> OrderBookManager:
    """
    创建并初始化期货订单簿管理器的便捷函数
    
    Args:
        symbol: 期货交易对符号
        api_key: API密钥
        api_secret: API密钥
        proxy_url: 代理URL
        **kwargs: 其他参数
        
    Returns:
        OrderBookManager: 已初始化的期货管理器实例
    """
    manager = OrderBookManager(
        symbol=symbol,
        api_key=api_key,
        api_secret=api_secret,
        proxy_url=proxy_url,
        **kwargs
    )
    
    if await manager.initialize():
        return manager
    else:
        raise RuntimeError("Failed to initialize OrderBookManager")