#!/usr/bin/env python3
"""
简化的WebSocket代理测试
专门测试代理配置问题
"""
import asyncio
import os
from binance import AsyncClient, BinanceSocketManager

async def test_simple_websocket():
    """简单的WebSocket代理测试"""
    
    proxy_url = os.getenv('PROXY_URL', 'http://127.0.0.1:7897')
    api_key = os.getenv('BINANCE_API_KEY', 'Pj4PyMhS6GmElbhQVi0n48WvFBEaGHEsT9njacTuBejXLYk7yWyQIDttI0tFLoIf')
    secret_key = os.getenv('BINANCE_SECRET_KEY', '8ELgLtB7IFLEbek3DAOtw9orZkXeKbSQpnAL6o4gmi8GDlnsZT1kxZINQqEYVKWb')
    
    print(f"🔧 测试代理: {proxy_url}")
    print("=" * 50)
    
    # 测试1: 不使用代理的WebSocket连接
    print("\n📡 测试1: 不使用代理的WebSocket连接")
    try:
        client = await AsyncClient.create(api_key=api_key, api_secret=secret_key)
        bm = BinanceSocketManager(client, user_timeout=10)
        
        print("🔄 尝试连接ticker stream (无代理)...")
        ts = bm.symbol_ticker_socket('BTCUSDT')
        
        async with ts as tscm:
            print("✅ 无代理WebSocket连接成功")
            msg = await asyncio.wait_for(tscm.recv(), timeout=5)
            if msg:
                print(f"📊 收到数据: 价格={msg.get('c', 'N/A')}")
            else:
                print("⚠️  未收到数据")
        
        await client.close_connection()
        print("✅ 测试1完成: 无代理连接正常")
        
    except asyncio.TimeoutError:
        print("⏰ 测试1超时: 无代理连接超时")
        if 'client' in locals():
            await client.close_connection()
    except Exception as e:
        print(f"❌ 测试1失败: {e}")
        if 'client' in locals():
            await client.close_connection()
    
    # 测试2: 使用代理的WebSocket连接
    print(f"\n🔧 测试2: 使用代理的WebSocket连接")
    try:
        client = await AsyncClient.create(
            api_key=api_key,
            api_secret=secret_key,
            https_proxy=proxy_url
        )
        bm = BinanceSocketManager(client, user_timeout=10)
        
        print("🔄 尝试连接ticker stream (使用代理)...")
        ts = bm.symbol_ticker_socket('BTCUSDT')
        
        async with ts as tscm:
            print("✅ 代理WebSocket连接成功")
            msg = await asyncio.wait_for(tscm.recv(), timeout=10)
            if msg:
                print(f"📊 收到数据: 价格={msg.get('c', 'N/A')}")
            else:
                print("⚠️  未收到数据")
        
        await client.close_connection()
        print("✅ 测试2完成: 代理连接正常")
        
    except asyncio.TimeoutError:
        print("⏰ 测试2超时: 代理连接超时")
        if 'client' in locals():
            await client.close_connection()
    except Exception as e:
        print(f"❌ 测试2失败: {e}")
        print(f"   错误类型: {type(e).__name__}")
        if 'client' in locals():
            await client.close_connection()
    
    # 测试3: 测试不同的WebSocket URL
    print(f"\n🌐 测试3: 测试WebSocket连接详情")
    try:
        import websockets
        import ssl
        
        # 测试直连Binance WebSocket
        print("🔄 测试直连Binance WebSocket...")
        try:
            uri = "wss://stream.binance.com:9443/ws/btcusdt@ticker"
            async with websockets.connect(uri, timeout=5) as websocket:
                print("✅ 直连Binance WebSocket成功")
                # 不等待消息，只测试连接
        except Exception as e:
            print(f"❌ 直连失败: {e}")
        
        # 测试通过代理连接
        print("🔄 测试通过代理连接WebSocket...")
        try:
            # 这里需要特殊的代理WebSocket库
            print("⚠️  需要专门的WebSocket代理库来测试")
        except Exception as e:
            print(f"❌ 代理WebSocket测试失败: {e}")
            
    except ImportError:
        print("⚠️  websockets库未安装，跳过底层测试")
    
    print(f"\n🎉 简化WebSocket测试完成!")
    print("=" * 50)
    print("📋 结论:")
    print("   如果测试1成功，测试2失败 → 代理不支持WebSocket")
    print("   如果测试1和2都失败 → 网络连接问题")
    print("   如果测试1和2都成功 → 代理配置正确")

if __name__ == "__main__":
    asyncio.run(test_simple_websocket())