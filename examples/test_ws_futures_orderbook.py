#!/usr/bin/env python3
"""
测试 ws_futures_get_order_book 方法
这是一个WebSocket API方法，用于获取期货订单簿数据
"""

import asyncio
import os
from binance import AsyncClient
from binance.exceptions import BinanceAPIException, BinanceWebsocketUnableToConnect


async def test_ws_futures_get_order_book():
    """测试WebSocket期货订单簿API"""
    
    # 获取API密钥
    api_key = os.getenv('BINANCE_API_KEY', 'Pj4PyMhS6GmElbhQVi0n48WvFBEaGHEsT9njacTuBejXLYk7yWyQIDttI0tFLoIf')
    secret_key = os.getenv('BINANCE_SECRET_KEY', '8ELgLtB7IFLEbek3DAOtw9orZkXeKbSQpnAL6o4gmi8GDlnsZT1kxZINQqEYVKWb')
    proxy_url = os.getenv('PROXY_URL', 'http://127.0.0.1:7897')
    
    print("🚀 测试 ws_futures_get_order_book 方法")
    print("=" * 50)
    
    client = None
    try:
        # 创建客户端
        print(f"📡 创建AsyncClient，代理: {proxy_url}")
        client = await AsyncClient.create(
            api_key=api_key,
            api_secret=secret_key,
            https_proxy=proxy_url
        )
        
        # 测试服务器连接
        print("🔄 测试服务器连接...")
        server_time = await client.get_server_time()
        print(f"✅ 服务器时间: {server_time}")
        
        # 测试期货交易所信息
        print("🔄 获取期货交易所信息...")
        futures_info = await client.futures_exchange_info()
        print(f"✅ 期货交易所状态: {futures_info['timezone']}")
        
        # 测试WebSocket期货订单簿
        print("🔄 测试 ws_futures_get_order_book...")
        
        # 测试参数
        symbol = "BTCUSDT"
        limit = 20
        
        try:
            # 调用WebSocket期货订单簿API
            result = await client.ws_futures_get_order_book(
                symbol=symbol,
                limit=limit
            )
            
            print(f"✅ WebSocket期货订单簿获取成功!")
            print(f"📊 交易对: {result.get('symbol', 'N/A')}")
            print(f"📊 最后更新ID: {result.get('lastUpdateId', 'N/A')}")
            print(f"📊 买单数量: {len(result.get('bids', []))}")
            print(f"📊 卖单数量: {len(result.get('asks', []))}")
            
            # 显示前5个买单和卖单
            bids = result.get('bids', [])[:5]
            asks = result.get('asks', [])[:5]
            
            print("\n💰 前5个买单 (价格, 数量):")
            for i, bid in enumerate(bids, 1):
                print(f"  {i}. {bid[0]} @ {bid[1]}")
                
            print("\n💸 前5个卖单 (价格, 数量):")
            for i, ask in enumerate(asks, 1):
                print(f"  {i}. {ask[0]} @ {ask[1]}")
                
            return True
            
        except BinanceWebsocketUnableToConnect as e:
            print(f"❌ WebSocket连接失败: {e}")
            print("💡 这可能是因为代理不支持WebSocket连接")
            return False
            
        except BinanceAPIException as e:
            print(f"❌ Binance API错误: {e}")
            return False
            
        except Exception as e:
            print(f"❌ 未知错误: {e}")
            print(f"错误类型: {type(e).__name__}")
            import traceback
            traceback.print_exc()
            return False
            
    except Exception as e:
        print(f"❌ 客户端创建失败: {e}")
        return False
        
    finally:
        if client:
            await client.close_connection()
            print("🔒 客户端连接已关闭")


async def test_rest_futures_orderbook():
    """作为对比，测试REST API期货订单簿"""
    
    api_key = os.getenv('BINANCE_API_KEY', 'Pj4PyMhS6GmElbhQVi0n48WvFBEaGHEsT9njacTuBejXLYk7yWyQIDttI0tFLoIf')
    secret_key = os.getenv('BINANCE_SECRET_KEY', '8ELgLtB7IFLEbek3DAOtw9orZkXeKbSQpnAL6o4gmi8GDlnsZT1kxZINQqEYVKWb')
    proxy_url = os.getenv('PROXY_URL', 'http://127.0.0.1:7897')
    
    print("\n🔄 对比测试: REST API期货订单簿")
    print("=" * 50)
    
    client = None
    try:
        client = await AsyncClient.create(
            api_key=api_key,
            api_secret=secret_key,
            https_proxy=proxy_url
        )
        
        # 使用REST API获取期货订单簿
        result = await client.futures_order_book(symbol="BTCUSDT", limit=20)
        
        print(f"✅ REST API期货订单簿获取成功!")
        print(f"📊 最后更新ID: {result.get('lastUpdateId', 'N/A')}")
        print(f"📊 买单数量: {len(result.get('bids', []))}")
        print(f"📊 卖单数量: {len(result.get('asks', []))}")
        
        return True
        
    except Exception as e:
        print(f"❌ REST API测试失败: {e}")
        return False
        
    finally:
        if client:
            await client.close_connection()


async def main():
    """主函数"""
    print("🎯 WebSocket期货订单簿API测试")
    print("=" * 60)
    
    # 测试WebSocket方法
    ws_success = await test_ws_futures_get_order_book()
    
    # 测试REST方法作为对比
    rest_success = await test_rest_futures_orderbook()
    
    print("\n📋 测试总结:")
    print("=" * 30)
    print(f"WebSocket期货订单簿: {'✅ 成功' if ws_success else '❌ 失败'}")
    print(f"REST期货订单簿: {'✅ 成功' if rest_success else '❌ 失败'}")
    
    if not ws_success and rest_success:
        print("\n💡 结论: WebSocket方法不可用，但REST方法正常")
        print("   原因: 代理服务器不支持WebSocket连接")
    elif ws_success:
        print("\n🎉 结论: WebSocket方法完全可用!")
    else:
        print("\n⚠️  结论: 两种方法都不可用，请检查网络和API配置")


if __name__ == "__main__":
    asyncio.run(main())