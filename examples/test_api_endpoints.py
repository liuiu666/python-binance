#!/usr/bin/env python3
"""
测试不同Binance API端点的连接情况
- 现货API: api.binance.com
- 期货API: fapi.binance.com
- WebSocket API测试
"""

import asyncio
import os
from binance import AsyncClient
from binance.exceptions import BinanceAPIException


async def test_spot_api():
    """测试现货API (api.binance.com)"""
    print("🔄 测试现货API (api.binance.com)")
    print("-" * 40)
    
    api_key = os.getenv('BINANCE_API_KEY', 'Pj4PyMhS6GmElbhQVi0n48WvFBEaGHEsT9njacTuBejXLYk7yWyQIDttI0tFLoIf')
    secret_key = os.getenv('BINANCE_SECRET_KEY', '8ELgLtB7IFLEbek3DAOtw9orZkXeKbSQpnAL6o4gmi8GDlnsZT1kxZINQqEYVKWb')
    proxy_url = os.getenv('PROXY_URL', 'http://127.0.0.1:7897')
    
    client = None
    try:
        client = await AsyncClient.create(
            api_key=api_key,
            api_secret=secret_key,
            https_proxy=proxy_url
        )
        
        # 测试现货API
        server_time = await client.get_server_time()
        print(f"✅ 现货服务器时间: {server_time['serverTime']}")
        
        # 测试现货订单簿
        orderbook = await client.get_order_book(symbol="BTCUSDT", limit=5)
        print(f"✅ 现货订单簿: {len(orderbook['bids'])} 买单, {len(orderbook['asks'])} 卖单")
        
        return True
        
    except Exception as e:
        print(f"❌ 现货API失败: {e}")
        return False
        
    finally:
        if client:
            await client.close_connection()


async def test_futures_api():
    """测试期货API (fapi.binance.com)"""
    print("\n🔄 测试期货API (fapi.binance.com)")
    print("-" * 40)
    
    api_key = os.getenv('BINANCE_API_KEY', 'Pj4PyMhS6GmElbhQVi0n48WvFBEaGHEsT9njacTuBejXLYk7yWyQIDttI0tFLoIf')
    secret_key = os.getenv('BINANCE_SECRET_KEY', '8ELgLtB7IFLEbek3DAOtw9orZkXeKbSQpnAL6o4gmi8GDlnsZT1kxZINQqEYVKWb')
    proxy_url = os.getenv('PROXY_URL', 'http://127.0.0.1:7897')
    
    client = None
    try:
        client = await AsyncClient.create(
            api_key=api_key,
            api_secret=secret_key,
            https_proxy=proxy_url
        )
        
        # 测试期货交易所信息
        futures_info = await client.futures_exchange_info()
        print(f"✅ 期货交易所状态: {futures_info['timezone']}")
        
        # 测试期货订单簿
        orderbook = await client.futures_order_book(symbol="BTCUSDT", limit=5)
        print(f"✅ 期货订单簿: {len(orderbook['bids'])} 买单, {len(orderbook['asks'])} 卖单")
        
        return True
        
    except Exception as e:
        print(f"❌ 期货API失败: {e}")
        print(f"   错误类型: {type(e).__name__}")
        return False
        
    finally:
        if client:
            await client.close_connection()


async def test_websocket_api():
    """测试WebSocket API方法"""
    print("\n🔄 测试WebSocket API方法")
    print("-" * 40)
    
    api_key = os.getenv('BINANCE_API_KEY', 'Pj4PyMhS6GmElbhQVi0n48WvFBEaGHEsT9njacTuBejXLYk7yWyQIDttI0tFLoIf')
    secret_key = os.getenv('BINANCE_SECRET_KEY', '8ELgLtB7IFLEbek3DAOtw9orZkXeKbSQpnAL6o4gmi8GDlnsZT1kxZINQqEYVKWb')
    proxy_url = os.getenv('PROXY_URL', 'http://127.0.0.1:7897')
    
    client = None
    try:
        client = await AsyncClient.create(
            api_key=api_key,
            api_secret=secret_key,
            https_proxy=proxy_url
        )
        
        # 测试现货WebSocket订单簿
        print("🔄 测试现货WebSocket订单簿...")
        try:
            result = await client.ws_get_order_book(symbol="BTCUSDT", limit=5)
            print(f"✅ 现货WebSocket订单簿成功: {len(result.get('bids', []))} 买单")
        except Exception as e:
            print(f"❌ 现货WebSocket订单簿失败: {e}")
        
        # 测试期货WebSocket订单簿
        print("🔄 测试期货WebSocket订单簿...")
        try:
            result = await client.ws_futures_get_order_book(symbol="BTCUSDT", limit=5)
            print(f"✅ 期货WebSocket订单簿成功: {len(result.get('bids', []))} 买单")
            return True
        except Exception as e:
            print(f"❌ 期货WebSocket订单簿失败: {e}")
            return False
        
    except Exception as e:
        print(f"❌ WebSocket API测试失败: {e}")
        return False
        
    finally:
        if client:
            await client.close_connection()


async def test_different_proxy_formats():
    """测试不同的代理格式"""
    print("\n🔄 测试不同代理格式对期货API的影响")
    print("-" * 50)
    
    api_key = os.getenv('BINANCE_API_KEY', 'Pj4PyMhS6GmElbhQVi0n48WvFBEaGHEsT9njacTuBejXLYk7yWyQIDttI0tFLoIf')
    secret_key = os.getenv('BINANCE_SECRET_KEY', '8ELgLtB7IFLEbek3DAOtw9orZkXeKbSQpnAL6o4gmi8GDlnsZT1kxZINQqEYVKWb')
    
    proxy_formats = [
        "http://127.0.0.1:7897",
        "https://127.0.0.1:7897",
        "127.0.0.1:7897"
    ]
    
    for i, proxy_url in enumerate(proxy_formats, 1):
        print(f"\n📡 测试代理格式 {i}: {proxy_url}")
        
        client = None
        try:
            client = await AsyncClient.create(
                api_key=api_key,
                api_secret=secret_key,
                https_proxy=proxy_url
            )
            
            # 快速测试期货API
            futures_info = await client.futures_exchange_info()
            print(f"✅ 代理格式 {i} 成功: {futures_info['timezone']}")
            
        except Exception as e:
            print(f"❌ 代理格式 {i} 失败: {e}")
            
        finally:
            if client:
                await client.close_connection()


async def main():
    """主函数"""
    print("🎯 Binance API端点连接测试")
    print("=" * 60)
    
    # 测试现货API
    spot_success = await test_spot_api()
    
    # 测试期货API
    futures_success = await test_futures_api()
    
    # 测试WebSocket API
    ws_success = await test_websocket_api()
    
    # 测试不同代理格式
    await test_different_proxy_formats()
    
    print("\n📋 测试总结:")
    print("=" * 30)
    print(f"现货API (api.binance.com): {'✅ 成功' if spot_success else '❌ 失败'}")
    print(f"期货API (fapi.binance.com): {'✅ 成功' if futures_success else '❌ 失败'}")
    print(f"WebSocket API: {'✅ 成功' if ws_success else '❌ 失败'}")
    
    print("\n💡 分析:")
    if spot_success and not futures_success:
        print("   - 现货API正常，期货API失败")
        print("   - 可能是代理配置对fapi.binance.com域名的处理问题")
        print("   - 建议检查代理软件的域名规则设置")
    elif not spot_success and not futures_success:
        print("   - 所有API都失败，可能是代理连接问题")
    elif spot_success and futures_success and not ws_success:
        print("   - REST API正常，WebSocket失败")
        print("   - 代理不支持WebSocket协议")
    else:
        print("   - 请根据具体结果分析问题")


if __name__ == "__main__":
    asyncio.run(main())