#!/usr/bin/env python3
"""
测试WebSocket代理连接
验证代理配置是否正确
"""
import asyncio
import os
import json
from binance import AsyncClient, BinanceSocketManager
from binance.ws.depthcache import DepthCacheManager

async def test_websocket_proxy():
    """测试WebSocket代理连接的各种方式"""
    
    # 代理配置
    proxy_url = os.getenv('PROXY_URL', 'http://127.0.0.1:7897')
    api_key = os.getenv('BINANCE_API_KEY', 'Pj4PyMhS6GmElbhQVi0n48WvFBEaGHEsT9njacTuBejXLYk7yWyQIDttI0tFLoIf')
    secret_key = os.getenv('BINANCE_SECRET_KEY', '8ELgLtB7IFLEbek3DAOtw9orZkXeKbSQpnAL6o4gmi8GDlnsZT1kxZINQqEYVKWb')
    
    print(f"🔧 测试代理: {proxy_url}")
    print(f"🔑 API Key: {api_key[:10]}...{api_key[-10:]}")
    print("=" * 60)
    
    # 测试1: 基础WebSocket连接
    print("\n📡 测试1: 基础WebSocket连接")
    try:
        client = await AsyncClient.create(
            api_key=api_key,
            api_secret=secret_key,
            https_proxy=proxy_url
        )
        
        bm = BinanceSocketManager(client, user_timeout=60)
        print("✅ BinanceSocketManager创建成功")
        
        # 测试简单的ticker stream
        print("🔄 尝试连接ticker stream...")
        ts = bm.symbol_ticker_socket('BTCUSDT')
        
        async with ts as tscm:
            print("✅ Ticker WebSocket连接成功")
            
            # 接收几条消息
            for i in range(3):
                msg = await tscm.recv()
                if msg:
                    print(f"📊 收到ticker数据: 价格={msg.get('c', 'N/A')}, 时间={msg.get('E', 'N/A')}")
                else:
                    print(f"⚠️  第{i+1}次未收到数据")
                
                if i < 2:  # 不在最后一次等待
                    await asyncio.sleep(1)
        
        await client.close_connection()
        print("✅ 测试1完成: 基础WebSocket连接正常")
        
    except Exception as e:
        print(f"❌ 测试1失败: {e}")
        print(f"   错误类型: {type(e).__name__}")
        if 'client' in locals():
            await client.close_connection()
    
    # 测试2: Depth Stream连接
    print(f"\n📖 测试2: Depth Stream连接")
    try:
        client = await AsyncClient.create(
            api_key=api_key,
            api_secret=secret_key,
            https_proxy=proxy_url
        )
        
        bm = BinanceSocketManager(client, user_timeout=60)
        
        # 测试depth stream
        print("🔄 尝试连接depth stream...")
        ds = bm.depth_socket('BTCUSDT')
        
        async with ds as dscm:
            print("✅ Depth WebSocket连接成功")
            
            # 接收几条消息
            for i in range(3):
                msg = await dscm.recv()
                if msg and isinstance(msg, dict):
                    bids_count = len(msg.get('b', []))
                    asks_count = len(msg.get('a', []))
                    print(f"📊 收到depth数据: bids={bids_count}, asks={asks_count}, updateId={msg.get('u', 'N/A')}")
                else:
                    print(f"⚠️  第{i+1}次未收到有效数据: {type(msg)}")
                
                if i < 2:
                    await asyncio.sleep(1)
        
        await client.close_connection()
        print("✅ 测试2完成: Depth Stream连接正常")
        
    except Exception as e:
        print(f"❌ 测试2失败: {e}")
        print(f"   错误类型: {type(e).__name__}")
        if 'client' in locals():
            await client.close_connection()
    
    # 测试3: DepthCacheManager连接
    print(f"\n🏗️  测试3: DepthCacheManager连接")
    try:
        client = await AsyncClient.create(
            api_key=api_key,
            api_secret=secret_key,
            https_proxy=proxy_url
        )
        
        bm = BinanceSocketManager(client, user_timeout=60)
        
        print("🔄 尝试创建DepthCacheManager...")
        dcm = DepthCacheManager(
            client=client,
            symbol='BTCUSDT',
            bm=bm,
            limit=100,  # 较小的limit，减少初始化时间
            refresh_interval=10*60,  # 10分钟刷新
            ws_interval=1000  # 1秒更新间隔，降低频率
        )
        
        print("🔄 尝试启动DepthCacheManager...")
        async with dcm as depth_cache_manager:
            print("✅ DepthCacheManager启动成功")
            print("✅ 初始订单簿快照已获取")
            
            # 接收几次更新
            for i in range(3):
                print(f"🔄 等待第{i+1}次深度更新...")
                depth_cache = await depth_cache_manager.recv()
                
                if depth_cache:
                    bids = depth_cache.get_bids()[:3]  # 前3档买单
                    asks = depth_cache.get_asks()[:3]  # 前3档卖单
                    
                    print(f"📊 深度缓存更新成功:")
                    print(f"   买单前3档: {[(float(price), float(qty)) for price, qty in bids]}")
                    print(f"   卖单前3档: {[(float(price), float(qty)) for price, qty in asks]}")
                else:
                    print(f"⚠️  第{i+1}次未收到深度缓存数据")
                
                if i < 2:
                    await asyncio.sleep(2)
        
        await client.close_connection()
        print("✅ 测试3完成: DepthCacheManager连接正常")
        
    except Exception as e:
        print(f"❌ 测试3失败: {e}")
        print(f"   错误类型: {type(e).__name__}")
        import traceback
        traceback.print_exc()
        if 'client' in locals():
            await client.close_connection()
    
    # 测试4: 不同代理格式测试
    print(f"\n🔧 测试4: 不同代理格式测试")
    
    proxy_formats = [
        proxy_url,  # 原始格式
        proxy_url.replace('http://', ''),  # 无协议
        f"http://127.0.0.1:7897",  # 明确HTTP
    ]
    
    for i, proxy in enumerate(proxy_formats, 1):
        print(f"\n🔄 测试代理格式 {i}: {proxy}")
        try:
            client = await AsyncClient.create(
                api_key=api_key,
                api_secret=secret_key,
                https_proxy=proxy
            )
            
            # 简单测试服务器时间
            server_time = await client.get_server_time()
            print(f"✅ 代理格式 {i} 工作正常: 服务器时间={server_time['serverTime']}")
            
            await client.close_connection()
            
        except Exception as e:
            print(f"❌ 代理格式 {i} 失败: {e}")
    
    print(f"\n🎉 WebSocket代理测试完成!")
    print("=" * 60)
    print("📋 测试总结:")
    print("   如果所有测试都成功，说明代理配置正确")
    print("   如果部分测试失败，可能是:")
    print("   1. 代理服务器不支持WebSocket协议")
    print("   2. 代理服务器SSL/TLS配置问题")
    print("   3. 网络连接不稳定")
    print("   4. 防火墙阻止WebSocket连接")

if __name__ == "__main__":
    asyncio.run(test_websocket_proxy())