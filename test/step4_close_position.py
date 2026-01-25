import sys
import time
from decimal import Decimal
from pathlib import Path

# 设置路径
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_skills import Settings, create_client
from trading_skills.order_executor import OrderExecutor

def main():
    print("=== 步骤4: 平仓 (强力模式) ===")
    
    # 初始化
    settings = Settings.load(ROOT)
    client = create_client(settings)
    symbol = "DOGEUSDT"
    
    max_retries = 3
    
    for i in range(max_retries):
        print(f"\n--- 第 {i+1} 次尝试检查持仓 ---")
        
        # 1. 获取当前持仓
        position_amt = Decimal("0")
        try:
            positions = client.futures_position_information(symbol=symbol)
            for pos in positions:
                amt = Decimal(pos['positionAmt'])
                if amt != 0:
                    position_amt = amt
                    print(f"当前持仓: {position_amt}")
                    break
        except Exception as e:
            print(f"获取持仓失败: {e}")
            return

        if position_amt == 0:
            print("当前无持仓，平仓完成。")
            
            # 最后清理一次挂单
            try:
                orders = client.futures_get_open_orders(symbol=symbol)
                if orders:
                    print(f"清理剩余挂单 ({len(orders)} 个)...")
                    client.futures_cancel_all_open_orders(symbol=symbol)
            except Exception as e:
                print(f"清理挂单失败: {e}")
                
            return

        # 2. 撤销所有挂单 (防止平仓后被触发)
        if i == 0: # 只在第一次尝试时强制撤单，后续如果是补单可以不需要
            print("正在撤销所有挂单...")
            try:
                client.futures_cancel_all_open_orders(symbol=symbol)
                print("挂单撤销成功。")
            except Exception as e:
                print(f"撤单失败: {e}")

        # 3. 市价平仓
        print("正在执行市价平仓...")
        quantity = abs(position_amt)
        side = "SELL" if position_amt > 0 else "BUY"
        
        try:
            client.futures_create_order(
                symbol=symbol,
                side=side,
                type="MARKET",
                quantity=str(quantity),
                reduceOnly=True
            )
            print("平仓指令已发送。")
        except Exception as e:
            print(f"平仓失败: {e}")
            
        # 等待成交
        time.sleep(2)
    
    print("\n警告: 达到最大重试次数，可能仍有持仓，请人工检查！")

if __name__ == "__main__":
    main()
