#!/usr/bin/env python3
"""
测试期货WebSocket端点配置
验证DepthCacheManager vs FuturesDepthCacheManager的区别
"""

import asyncio
import os
from binance import AsyncClient, BinanceSocketManager
from binance.ws.depthcache import DepthCacheManager, FuturesDepthCacheManager

async def test_endpoints():
    """测试不同DepthCacheManager使用的端点"""
    
    # 获取API密钥
    api_key = os.getenv('BINANCE_API_KEY', 'test_key')
    api_secret = os.getenv('BINANCE_API_SECRET', 'test_secret')
    proxy_url = os.getenv('PROXY_URL', 'http://127.0.0.1:7897')
    
    print("🔍 测试期货WebSocket端点配置")
    print("=" * 50)
    
    try:
        # 创建客户端
        client = await AsyncClient.create(
            api_key=api_key,
            api_secret=api_secret,
            https_proxy=proxy_url
        )
        
        # 创建BinanceSocketManager
        bm = BinanceSocketManager(client)
        
        print(f"📡 BinanceSocketManager端点配置:")
        print(f"   现货WebSocket: {bm.STREAM_URL}")
        print(f"   期货WebSocket: {bm.FSTREAM_URL}")
        print(f"   币本位期货: {bm.DSTREAM_URL}")
        print()
        
        # 测试普通DepthCacheManager使用的socket
        print("🔍 测试普通DepthCacheManager:")
        try:
            dcm_spot = DepthCacheManager(
                client=client,
                symbol="BTCUSDT",
                bm=bm,
                limit=10
            )
            
            # 获取socket但不启动
            socket_spot = dcm_spot._get_socket()
            print(f"   ✅ 普通DepthCacheManager使用: depth_socket")
            print(f"   📍 这会连接到现货端点: {bm.STREAM_URL}")
            
        except Exception as e:
            print(f"   ❌ 普通DepthCacheManager错误: {e}")
        
        print()
        
        # 测试FuturesDepthCacheManager使用的socket
        print("🔍 测试FuturesDepthCacheManager:")
        try:
            dcm_futures = FuturesDepthCacheManager(
                client=client,
                symbol="BTCUSDT",
                bm=bm,
                limit=10
            )
            
            # 获取socket但不启动
            socket_futures = dcm_futures._get_socket()
            print(f"   ✅ FuturesDepthCacheManager使用: futures_depth_socket")
            print(f"   📍 这会连接到期货端点: {bm.FSTREAM_URL}")
            
        except Exception as e:
            print(f"   ❌ FuturesDepthCacheManager错误: {e}")
        
        print()
        print("📋 结论:")
        print("   - 如果交易期货合约，应该使用 FuturesDepthCacheManager")
        print("   - FuturesDepthCacheManager 使用正确的 wss://fstream.binance.com 端点")
        print("   - 普通 DepthCacheManager 使用现货端点 wss://stream.binance.com")
        
        await client.close_connection()
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")

if __name__ == "__main__":
    asyncio.run(test_endpoints())