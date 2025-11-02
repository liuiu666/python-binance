#!/usr/bin/env python3
"""
测试代理连接的简单脚本
"""
import asyncio
import aiohttp
import os

async def test_proxy_connection():
    """测试代理连接"""
    proxy_url = os.getenv('PROXY_URL', 'http://127.0.0.1:7897')
    print(f"测试代理: {proxy_url}")
    
    # 测试HTTP连接
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                'https://api.binance.com/api/v3/ping',
                proxy=proxy_url,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                print(f"HTTP代理测试成功: {response.status}")
                result = await response.json()
                print(f"响应: {result}")
                return True
    except Exception as e:
        print(f"HTTP代理测试失败: {e}")
        return False

async def test_websocket_proxy():
    """测试WebSocket代理连接"""
    import websockets
    from websockets_proxy import Proxy, proxy_connect
    
    proxy_url = os.getenv('PROXY_URL', 'http://127.0.0.1:7897')
    print(f"测试WebSocket代理: {proxy_url}")
    
    try:
        # 解析代理URL
        from urllib.parse import urlparse
        parsed = urlparse(proxy_url)
        proxy = Proxy.from_url(proxy_url)
        
        # 测试WebSocket连接
        uri = "wss://stream.binance.com:9443/ws/btcusdt@ticker"
        async with proxy_connect(uri, proxy=proxy) as websocket:
            print("WebSocket代理连接成功")
            # 接收一条消息
            message = await asyncio.wait_for(websocket.recv(), timeout=10)
            print(f"收到消息: {len(message)} 字节")
            return True
    except Exception as e:
        print(f"WebSocket代理测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False

async def main():
    """主函数"""
    print("=== 代理连接测试 ===")
    
    # 测试HTTP代理
    print("\n1. 测试HTTP代理连接...")
    http_ok = await test_proxy_connection()
    
    # 测试WebSocket代理
    print("\n2. 测试WebSocket代理连接...")
    ws_ok = await test_websocket_proxy()
    
    print(f"\n=== 测试结果 ===")
    print(f"HTTP代理: {'✓' if http_ok else '✗'}")
    print(f"WebSocket代理: {'✓' if ws_ok else '✗'}")
    
    if http_ok and ws_ok:
        print("代理配置正常，可以使用")
    else:
        print("代理配置有问题，需要检查代理服务器设置")

if __name__ == "__main__":
    asyncio.run(main())