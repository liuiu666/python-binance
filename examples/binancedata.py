import asyncio
import json
import os
import time
from typing import Dict, Optional
from binance import AsyncClient, BinanceSocketManager
from binance.ws.depthcache import FuturesDepthCacheManager


class OrderbookCollector:
    def __init__(self, symbol: str = "BTCUSDT", proxy_url: Optional[str] = None):
        """
        初始化订单簿收集器
        
        Args:
            symbol: 交易对符号，默认BTCUSDT
            proxy_url: 代理URL，如果为None则从环境变量PROXY_URL获取，默认http://127.0.0.1:7897
        """
        self.symbol = symbol.upper()
        
        # 获取API密钥配置
        self.api_key = os.getenv('BINANCE_API_KEY', 'Pj4PyMhS6GmElbhQVi0n48WvFBEaGHEsT9njacTuBejXLYk7yWyQIDttI0tFLoIf')
        self.secret_key = os.getenv('BINANCE_SECRET_KEY', '8ELgLtB7IFLEbek3DAOtw9orZkXeKbSQpnAL6o4gmi8GDlnsZT1kxZINQqEYVKWb')
        
        print(f"API Key: {self.api_key[:10]}...{self.api_key[-10:]}")  # 只显示部分密钥用于确认
        
        # 获取代理配置
        self.proxy_url = proxy_url or os.getenv('PROXY_URL','http://127.0.0.1:7897')
        if self.proxy_url:
            print(f"使用代理: {self.proxy_url}")
        else:
            print("错误：必须设置代理才能访问Binance API")
            print("请设置环境变量 PROXY_URL 或在初始化时传入proxy_url参数")
            raise ValueError("代理配置缺失，无法访问Binance API")
        
        # 初始化客户端和socket管理器
        self.client = None
        self.bm = None
        self.depth_cache_manager = None
        
        # 订单簿数据
        self.orderbook = {
            'bids': [],
            'asks': [],
            'timestamp': None,
            'last_update_id': None
        }
        
        # 统计信息
        self.update_count = 0
        self.start_time = time.time()
        
    async def initialize(self):
        """初始化异步客户端和socket管理器"""
        try:
            # 创建异步客户端，配置代理和API密钥
            self.client = await AsyncClient.create(
                api_key=self.api_key,
                api_secret=self.secret_key,
                https_proxy=self.proxy_url
            )
            print(f"AsyncClient创建成功，代理配置: {self.proxy_url}")
            
            # 创建socket管理器（代理配置通过client传递）
            self.bm = BinanceSocketManager(self.client)
            print(f"BinanceSocketManager创建成功，使用client的代理配置")
            
            # 测试连接 - 获取服务器时间
            server_time = await self.client.get_server_time()
            print(f"服务器时间: {server_time}")
            print("代理连接测试成功！")
            
            return True
            
        except Exception as e:
            print(f"初始化失败: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    async def start_depth_cache(self):
        """启动深度缓存管理器 - 增强版重试机制"""
        max_retries = 10  # 增加到10次重试
        base_retry_delay = 3  # 基础延迟3秒
        max_retry_delay = 60  # 最大延迟60秒
        
        for attempt in range(max_retries):
            retry_delay = min(base_retry_delay * (2 ** attempt), max_retry_delay)
            
            try:
                print(f"🔄 尝试启动深度缓存 (第{attempt + 1}/{max_retries}次)...")
                
                # 创建期货深度缓存管理器
                self.depth_cache_manager = FuturesDepthCacheManager(
                    client=self.client,
                    symbol=self.symbol,
                    bm=self.bm,
                    limit=1000,  # 获取1000档深度
                    refresh_interval=30*60  # 30分钟刷新一次全量数据
                )
                
                print(f"📡 开始监听 {self.symbol} 订单簿数据...")
                print("📥 正在获取初始订单簿快照...")
                
                # 启动深度缓存
                async with self.depth_cache_manager as dcm:
                    print("✅ 深度缓存管理器启动成功")
                    print("✅ 初始订单簿快照已获取")
                    print("✅ WebSocket增量更新已建立")
                    print("🎯 开始实时数据流...")
                    
                    # 重置重试计数器，因为连接成功了
                    connection_errors = 0
                    max_connection_errors = 5
                    
                    while True:
                        try:
                            # 接收深度更新
                            depth_cache = await dcm.recv()
                            
                            # 检查是否收到错误消息
                            if isinstance(depth_cache, dict) and depth_cache.get('e') == 'error':
                                error_type = depth_cache.get('type')
                                print(f"❌ 收到WebSocket错误: {depth_cache}")
                                
                                if error_type == 'BinanceWebsocketClosed':
                                    print("🔄 WebSocket连接已关闭，正在尝试重连...")
                                    break  # 退出内层循环，触发重试
                                else:
                                    print(f"⚠️  未知错误类型: {error_type}")
                                    continue
                            
                            # 更新订单簿数据
                            self.update_orderbook(depth_cache)
                            
                            # 重置连接错误计数器
                            connection_errors = 0
                            
                        except (ConnectionResetError, ConnectionError, OSError) as inner_e:
                            connection_errors += 1
                            print(f"⚠️  连接错误 ({connection_errors}/{max_connection_errors}): {inner_e}")
                            
                            if connection_errors >= max_connection_errors:
                                print("❌ 连接错误过多，退出当前会话重试")
                                break
                            
                            # 短暂等待后继续
                            await asyncio.sleep(2)
                            continue
                            
                        except Exception as inner_e:
                            print(f"⚠️  处理深度数据时出错: {inner_e}")
                            await asyncio.sleep(1)
                            continue
                    
                    # 如果到达这里，说明WebSocket连接断开，需要重试
                    print("🔄 WebSocket连接断开，准备重试...")
                    
            except ConnectionResetError as e:
                print(f"❌ 连接被重置 (第{attempt + 1}/{max_retries}次尝试): {e}")
                if attempt < max_retries - 1:
                    print(f"⏳ 网络可能不稳定，等待 {retry_delay} 秒后重试...")
                    await asyncio.sleep(retry_delay)
                else:
                    print("❌ 达到最大重试次数，WebSocket连接失败")
                    print("💡 建议检查网络连接或代理服务器的WebSocket支持")
                    raise
                    
            except (ConnectionError, OSError, TimeoutError) as e:
                print(f"❌ 网络连接错误 (第{attempt + 1}/{max_retries}次尝试): {e}")
                if attempt < max_retries - 1:
                    print(f"⏳ 网络连接不稳定，等待 {retry_delay} 秒后重试...")
                    await asyncio.sleep(retry_delay)
                else:
                    print("❌ 达到最大重试次数，网络连接失败")
                    raise
                    
            except Exception as e:
                print(f"❌ 启动深度缓存失败 (第{attempt + 1}/{max_retries}次尝试): {e}")
                print(f"   错误类型: {type(e).__name__}")
                import traceback
                traceback.print_exc()
                if attempt < max_retries - 1:
                    print(f"⏳ 等待 {retry_delay} 秒后重试...")
                    await asyncio.sleep(retry_delay)
                else:
                    print("❌ 达到最大重试次数，无法启动深度缓存")
                    raise
    
    def update_orderbook(self, depth_cache):
        """更新订单簿数据"""
        try:
            # 获取买单和卖单
            bids = depth_cache.get_bids()[:20]  # 取前20档
            asks = depth_cache.get_asks()[:20]  # 取前20档
            
            # 更新订单簿
            self.orderbook.update({
                'bids': bids,
                'asks': asks,
                'timestamp': time.time() * 1000,  # 毫秒时间戳
                'last_update_id': depth_cache.update_time,
                'symbol': self.symbol
            })
            
            self.update_count += 1
            
        except Exception as e:
            print(f"更新订单簿数据时出错: {e}")
    
    def print_stats(self):
        """打印统计信息"""
        if self.update_count % 10 == 0:  # 每10次更新打印一次
            elapsed_time = time.time() - self.start_time
            updates_per_second = self.update_count / elapsed_time if elapsed_time > 0 else 0
            
            print(f"\n📊 === {self.symbol} 订单簿统计 ===")
            print(f"📈 更新次数: {self.update_count}")
            print(f"⏱️  运行时间: {elapsed_time:.1f}秒")
            print(f"🚀 更新频率: {updates_per_second:.2f}次/秒")
            
            if self.orderbook['bids'] and self.orderbook['asks']:
                best_bid = self.orderbook['bids'][0]
                best_ask = self.orderbook['asks'][0]
                spread = float(best_ask[0]) - float(best_bid[0])
                spread_pct = (spread / float(best_bid[0])) * 100
                
                print(f"💰 最佳买价: {best_bid[0]} (数量: {best_bid[1]})")
                print(f"💸 最佳卖价: {best_ask[0]} (数量: {best_ask[1]})")
                print(f"📏 价差: {spread:.8f} ({spread_pct:.4f}%)")
                print(f"🕐 最后更新: {self.orderbook['last_update_id']}")
            
            print("=" * 40)
    
    def get_orderbook_snapshot(self) -> Dict:
        """获取当前订单簿快照"""
        return self.orderbook.copy()
    
    async def close(self):
        """关闭连接"""
        try:
            if self.depth_cache_manager:
                # DepthCacheManager会在async with中自动关闭
                pass
            if self.client:
                await self.client.close_connection()
            print("连接已关闭")
        except Exception as e:
            print(f"关闭连接时出错: {e}")


async def main():
    """主函数"""
    # 创建订单簿收集器
    collector = OrderbookCollector(symbol="BTCUSDT")
    
    try:
        # 初始化
        if not await collector.initialize():
            print("初始化失败，退出程序")
            return
        
        print("初始化成功，开始收集订单簿数据...")
        
        # 启动深度缓存
        await collector.start_depth_cache()
        
    except KeyboardInterrupt:
        print("\n收到中断信号，正在关闭...")
    except Exception as e:
        print(f"程序运行出错: {e}")
    finally:
        # 清理资源
        await collector.close()


if __name__ == "__main__":
    # 运行主程序
    asyncio.run(main())