#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Binance期货订单簿测试脚本
测试期货订单簿管理器是否能正常获取BTCUSDT数据
"""

import asyncio
import sys
import os

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from binance.ws.orderbook_manager import OrderBookManager


async def test_futures_orderbook():
    """测试期货订单簿功能"""
    print("🚀 开始测试Binance期货订单簿管理器...")
    print("=" * 50)
    
    # 创建订单簿管理器实例
    manager = OrderBookManager(
        symbol="BTCUSDT",
        # 如果需要代理，取消下面这行的注释
        proxy_url="http://127.0.0.1:7897"
    )
    
    try:
        # 1. 测试初始化
        print("📡 测试客户端初始化...")
        if not await manager.initialize():
            print("❌ 客户端初始化失败")
            return False
        
        # 2. 测试获取初始快照
        print("\n📊 测试获取1000档订单簿快照...")
        if not await manager.get_initial_snapshot():
            print("❌ 获取订单簿快照失败")
            return False
        
        # 3. 显示订单簿基本信息
        print("\n📈 订单簿基本信息:")
        best_prices = manager.get_best_prices()
        print(f"   最佳买价: {best_prices['bid']}")
        print(f"   最佳卖价: {best_prices['ask']}")
        
        if best_prices['bid'] and best_prices['ask']:
            spread = best_prices['ask'] - best_prices['bid']
            print(f"   买卖价差: {spread:.2f}")
        
        # 4. 显示订单簿深度
        depth_summary = manager.get_depth_summary(levels=5)
        print(f"\n📋 前5档订单簿:")
        print("   买单 (价格 | 数量):")
        for i, (price, qty) in enumerate(depth_summary['bids'][:5]):
            print(f"     {i+1}. {price:>10.2f} | {qty:>8.4f}")
        
        print("   卖单 (价格 | 数量):")
        for i, (price, qty) in enumerate(depth_summary['asks'][:5]):
            print(f"     {i+1}. {price:>10.2f} | {qty:>8.4f}")
        
        # 5. 测试WebSocket连接
        print("\n🔌 测试WebSocket连接...")
        if not await manager.start_websocket():
            print("❌ WebSocket连接失败")
            return False
        
        # 6. 运行5秒接收实时更新
        print("\n⏱️  运行5秒接收实时更新...")
        initial_stats = manager.get_statistics()
        initial_updates = initial_stats['update_count']
        
        await manager.run(duration=5)
        
        final_stats = manager.get_statistics()
        final_updates = final_stats['update_count']
        updates_received = final_updates - initial_updates
        
        print(f"✅ 5秒内接收到 {updates_received} 次更新")
        
        # 7. 显示最终统计信息
        stats = manager.get_statistics()
        print(f"\n📊 最终统计信息:")
        print(f"   总更新次数: {stats['update_count']}")
        print(f"   更新频率: {stats['update_frequency']:.2f} 次/秒")
        print(f"   运行时长: {stats['runtime_seconds']:.1f}秒")
        print(f"   买单档数: {stats['bid_levels']}")
        print(f"   卖单档数: {stats['ask_levels']}")
        print(f"   价差: {stats['spread']:.2f}")
        print(f"   价差百分比: {stats['spread_percentage']:.4f}%")
        
        # 8. 验证订单簿有效性
        validation = manager.validate_orderbook()
        print(f"\n✅ 订单簿验证结果:")
        print(f"   买单档数: {validation['statistics']['bids_count']}")
        print(f"   卖单档数: {validation['statistics']['asks_count']}")
        print(f"   总档数: {validation['statistics']['total_levels']}")
        print(f"   数据有效: {'是' if validation['is_valid'] else '否'}")
        
        if not validation['is_valid']:
            print(f"   验证错误: {validation.get('errors', [])}")
        
        if validation.get('warnings'):
            print(f"   警告信息: {validation['warnings']}")
        
        return True
        
    except Exception as e:
        print(f"❌ 测试过程中发生错误: {e}")
        return False
        
    finally:
        # 清理资源
        print("\n🧹 清理资源...")
        await manager.close()
        print("✅ 资源清理完成")


async def main():
    """主函数"""
    print("Binance期货订单簿测试")
    print("测试合约: BTCUSDT")
    print("=" * 50)
    
    success = await test_futures_orderbook()
    
    print("\n" + "=" * 50)
    if success:
        print("🎉 所有测试通过！期货订单簿管理器工作正常")
    else:
        print("💥 测试失败！请检查网络连接或API配置")
    
    return success


if __name__ == "__main__":
    # 运行测试
    try:
        result = asyncio.run(main())
        sys.exit(0 if result else 1)
    except KeyboardInterrupt:
        print("\n⚠️  测试被用户中断")
        sys.exit(1)
    except Exception as e:
        print(f"\n💥 程序异常退出: {e}")
        sys.exit(1)