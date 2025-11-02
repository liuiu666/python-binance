#!/usr/bin/env python3
"""
代理配置测试脚本
测试不同的代理配置方式
"""
import asyncio
import os
from binance import AsyncClient, BinanceSocketManager

async def test_proxy_configurations():
    """测试不同的代理配置"""
    
    api_key = os.getenv('BINANCE_API_KEY', 'Pj4PyMhS6GmElbhQVi0n48WvFBEaGHEsT9njacTuBejXLYk7yWyQIDttI0tFLoIf')
    secret_key = os.getenv('BINANCE_SECRET_KEY', '8ELgLtB7IFLEbek3DAOtw9orZkXeKbSQpnAL6o4gmi8GDlnsZT1kxZINQqEYVKWb')
    
    print("🔧 代理配置测试")
    print("=" * 50)
    
    # 测试不同的代理配置方式
    proxy_configs = [
        {
            'name': '无代理',
            'config': {}
        },
        {
            'name': 'https_proxy参数',
            'config': {'https_proxy': 'http://127.0.0.1:7897'}
        },
        {
            'name': 'proxies参数 (字典格式)',
            'config': {'proxies': {'https': 'http://127.0.0.1:7897'}}
        },
        {
            'name': 'proxies参数 (完整格式)',
            'config': {'proxies': {
                'http': 'http://127.0.0.1:7897',
                'https': 'http://127.0.0.1:7897'
            }}
        }
    ]
    
    for i, proxy_config in enumerate(proxy_configs, 1):
        print(f"\n📡 测试 {i}: {proxy_config['name']}")
        print(f"   配置: {proxy_config['config']}")
        
        try:
            # 创建客户端
            client = await AsyncClient.create(
                api_key=api_key,
                api_secret=secret_key,
                **proxy_config['config']
            )
            
            # 测试REST API
            print("🔄 测试REST API...")
            server_time = await client.get_server_time()
            print(f"✅ REST API成功: {server_time['serverTime']}")
            
            # 测试WebSocket
            print("🔄 测试WebSocket...")
            bm = BinanceSocketManager(client, user_timeout=5)
            
            try:
                ts = bm.symbol_ticker_socket('BTCUSDT')
                async with ts as tscm:
                    msg = await asyncio.wait_for(tscm.recv(), timeout=3)
                    if msg:
                        print(f"✅ WebSocket成功: 价格={msg.get('c', 'N/A')}")
                    else:
                        print("⚠️  WebSocket连接成功但未收到数据")
            except asyncio.TimeoutError:
                print("⏰ WebSocket超时")
            except Exception as ws_e:
                print(f"❌ WebSocket失败: {ws_e}")
            
            await client.close_connection()
            
        except Exception as e:
            print(f"❌ 配置 {i} 失败: {e}")
            print(f"   错误类型: {type(e).__name__}")
            if 'client' in locals():
                try:
                    await client.close_connection()
                except:
                    pass
    
    # 测试环境变量代理
    print(f"\n🌍 测试环境变量代理")
    
    # 设置环境变量
    original_http_proxy = os.environ.get('HTTP_PROXY')
    original_https_proxy = os.environ.get('HTTPS_PROXY')
    
    try:
        os.environ['HTTP_PROXY'] = 'http://127.0.0.1:7897'
        os.environ['HTTPS_PROXY'] = 'http://127.0.0.1:7897'
        
        print("🔄 设置环境变量代理...")
        client = await AsyncClient.create(api_key=api_key, api_secret=secret_key)
        
        # 测试REST API
        server_time = await client.get_server_time()
        print(f"✅ 环境变量代理REST成功: {server_time['serverTime']}")
        
        await client.close_connection()
        
    except Exception as e:
        print(f"❌ 环境变量代理失败: {e}")
        if 'client' in locals():
            try:
                await client.close_connection()
            except:
                pass
    finally:
        # 恢复环境变量
        if original_http_proxy:
            os.environ['HTTP_PROXY'] = original_http_proxy
        else:
            os.environ.pop('HTTP_PROXY', None)
            
        if original_https_proxy:
            os.environ['HTTPS_PROXY'] = original_https_proxy
        else:
            os.environ.pop('HTTPS_PROXY', None)
    
    print(f"\n🎉 代理配置测试完成!")
    print("=" * 50)
    print("📋 总结:")
    print("   1. 如果REST API都成功，说明HTTP代理工作正常")
    print("   2. 如果WebSocket都失败，说明代理不支持WebSocket")
    print("   3. 如果某些配置成功，说明代理配置方式有问题")
    print("   4. 推荐使用 https_proxy 参数配置代理")

if __name__ == "__main__":
    asyncio.run(test_proxy_configurations())