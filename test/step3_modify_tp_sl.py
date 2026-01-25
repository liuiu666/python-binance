import sys
from decimal import Decimal
from pathlib import Path

# 设置路径
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_skills import Settings, create_client
from trading_skills.order_executor import OrderExecutor

def main():
    print("=== 步骤3: 修改止盈止损 (仓位条件单) ===")
    
    # 初始化
    settings = Settings.load(ROOT)
    client = create_client(settings)
    executor = OrderExecutor(client)
    symbol = "DOGEUSDT"
    
    # 1. 获取当前持仓
    position_amt = Decimal("0")
    entry_price = Decimal("0")
    
    try:
        positions = client.futures_position_information(symbol=symbol)
        for pos in positions:
            amt = Decimal(pos['positionAmt'])
            if amt != 0:
                position_amt = amt
                entry_price = Decimal(pos['entryPrice'])
                print(f"当前持仓: {position_amt}, 入场价: {entry_price}")
                break
                
        if position_amt == 0:
            print("错误: 未检测到持仓，无法修改止盈止损。")
            return
            
    except Exception as e:
        print(f"获取持仓失败: {e}")
        return

    # 2. 撤销旧挂单
    print("正在撤销旧的挂单...")
    try:
        # 获取所有挂单
        orders = client.futures_get_open_orders(symbol=symbol)
        count = 0
        for order in orders:
            # 简单起见，撤销该交易对所有挂单
            client.futures_cancel_order(symbol=symbol, orderId=order['orderId'])
            print(f"已撤销订单: {order['orderId']} ({order['type']})")
            count += 1
            
        if count == 0:
            print("未发现旧挂单，直接设置新单。")
        else:
            print(f"共撤销 {count} 个订单。")
            
    except Exception as e:
        print(f"撤单失败: {e}")
        # 继续尝试下新单

    # 3. 计算新价格 (扩大范围)
    if position_amt > 0:
        entry_side = "BUY"
        sl_price = entry_price * Decimal("0.98") # -2%
        tp_price = entry_price * Decimal("1.02") # +2%
    else:
        entry_side = "SELL"
        sl_price = entry_price * Decimal("1.02") # -2%
        tp_price = entry_price * Decimal("0.98") # +2%
        
    print(f"新止损价: {sl_price}")
    print(f"新止盈价: {tp_price}")

    # 4. 下新单
    try:
        # 止损
        sl_res = executor.place_stop_loss_market(
            symbol=symbol,
            entry_side=entry_side,
            stop_price=sl_price,
            close_position=True # 明确指定使用仓位止损
        )
        print(f"新止损单成功，订单ID: {sl_res.stop_order_id}")
        
        # 止盈
        tp_res = executor.place_take_profit_market(
            symbol=symbol,
            entry_side=entry_side,
            take_profit_price=tp_price,
            close_position=True # 明确指定使用仓位止盈
        )
        print(f"新止盈单成功，订单ID: {tp_res.tp_order_id}")
        
        print("步骤3完成。")
        
    except Exception as e:
        print(f"设置新止盈止损失败: {e}")

if __name__ == "__main__":
    main()
