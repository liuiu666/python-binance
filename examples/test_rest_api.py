#!/usr/bin/env python3
"""
测试Binance REST API连接
"""
import asyncio
import os
from binance import AsyncClient

async def test_rest_api():
    """测试REST API各项功能"""
    
    # API密钥配置
    api_key = os.getenv('BINANCE_API_KEY', 'Pj4PyMhS6GmElbhQVi0n48WvFBEaGHEsT9njacTuBejXLYk7yWyQIDttI0tFLoIf')
    secret_key = os.getenv('BINANCE_SECRET_KEY', '8ELgLtB7IFLEbek3DAOtw9orZkXeKbSQpnAL6o4gmi8GDlnsZT1kxZINQqEYVKWb')
    proxy_url = os.getenv('PROXY_URL', 'http://127.0.0.1:7897')
    
    print(f"🔧 使用代理: {proxy_url}")
    print(f"🔑 API Key: {api_key[:10]}...{api_key[-10:]}")
    
    try:
        # 创建客户端
        client = await AsyncClient.create(
            api_key=api_key,
            api_secret=secret_key,
            https_proxy=proxy_url
        )
        print("✅ AsyncClient创建成功")
        
        # 测试1: 获取服务器时间
        print("\n📡 测试1: 获取服务器时间")
        server_time = await client.get_server_time()
        print(f"✅ 服务器时间: {server_time}")
        
        # 测试2: 获取交易规则
        print("\n📋 测试2: 获取交易规则")
        exchange_info = await client.get_exchange_info()
        print(f"✅ 交易所信息获取成功，共有 {len(exchange_info['symbols'])} 个交易对")
        
        # 测试3: 获取BTCUSDT价格
        print("\n💰 测试3: 获取BTCUSDT价格")
        ticker = await client.get_symbol_ticker(symbol="BTCUSDT")
        print(f"✅ BTCUSDT当前价格: {ticker['price']}")
        
        # 测试4: 获取24小时价格变化
        print("\n📊 测试4: 获取24小时价格统计")
        stats = await client.get_ticker(symbol="BTCUSDT")
        print(f"✅ 24小时价格变化: {stats['priceChangePercent']}%")
        print(f"✅ 24小时成交量: {stats['volume']} BTC")
        
        # 测试5: 获取订单簿
        print("\n📖 测试5: 获取订单簿")
        orderbook = await client.get_order_book(symbol="BTCUSDT", limit=10)
        print(f"✅ 订单簿获取成功")
        print(f"   最佳买价: {orderbook['bids'][0][0]}")
        print(f"   最佳卖价: {orderbook['asks'][0][0]}")
        print(f"   最后更新ID: {orderbook['lastUpdateId']}")
        
        # 测试6: 获取K线数据
        print("\n📈 测试6: 获取K线数据")
        klines = await client.get_klines(symbol="BTCUSDT", interval="1m", limit=5)
        print(f"✅ 获取到 {len(klines)} 条K线数据")
        latest_kline = klines[-1]
        print(f"   最新K线开盘价: {latest_kline[1]}")
        print(f"   最新K线收盘价: {latest_kline[4]}")
        
        # 测试7: 获取账户信息（需要有效API密钥）
        print("\n👤 测试7: 获取账户信息")
        try:
            account = await client.get_account()
            print(f"✅ 账户信息获取成功")
            print(f"   账户类型: {account.get('accountType', 'SPOT')}")
            print(f"   可交易: {account.get('canTrade', False)}")
            print(f"   余额数量: {len(account.get('balances', []))}")
        except Exception as e:
            print(f"⚠️  账户信息获取失败: {e}")
            print("   这可能是因为API密钥权限不足或无效")
        
        print(f"\n🎉 REST API测试完成！")
        
    except Exception as e:
        print(f"❌ REST API测试失败: {e}")
        import traceback
        traceback.print_exc()
        
    finally:
        # 关闭客户端连接
        if 'client' in locals():
            await client.close_connection()
            print("🔒 客户端连接已关闭")

if __name__ == "__main__":
    asyncio.run(test_rest_api())