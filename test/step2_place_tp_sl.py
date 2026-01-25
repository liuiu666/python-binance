import sys
from decimal import Decimal
from pathlib import Path

# 设置路径
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_skills import Settings, create_client
from trading_skills.order_executor import OrderExecutor

def main():
    print("=== 步骤2: 设置止盈止损 (仓位条件单) ===")
    
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
                print(f"检测到持仓: {position_amt}, 入场价: {entry_price}")
                break
                
        if position_amt == 0:
            print("错误: 未检测到持仓，请先执行步骤1 (step1_place_order.py)。")
            return
            
    except Exception as e:
        print(f"获取持仓失败: {e}")
        return

    # 2. 计算价格
    # quantity = abs(position_amt) # 仓位止损不需要指定数量
    
    # 多单: 止损在下方，止盈在上方
    if position_amt > 0:
        entry_side = "BUY"
        sl_price = entry_price * Decimal("0.99") # -1%
        tp_price = entry_price * Decimal("1.01") # +1%
    else:
        entry_side = "SELL"
        sl_price = entry_price * Decimal("1.01") # -1%
        tp_price = entry_price * Decimal("0.99") # +1%
        
    print(f"计划止损价: {sl_price}")
    print(f"计划止盈价: {tp_price}")

    # 3. 下单 (使用 close_position=True)
    try:
        # 止损 (条件单仓位止损)
        print("正在设置止损 (仓位模式)...")
        sl_res = executor.place_stop_loss_market(
            symbol=symbol,
            entry_side=entry_side,
            stop_price=sl_price,
            close_position=True # 明确指定使用仓位止损
        )
        print(f"止损单成功，订单ID: {sl_res.stop_order_id}")
        
        # 止盈 (条件单仓位止盈)
        print("正在设置止盈 (仓位模式)...")
        tp_res = executor.place_take_profit_market(
            symbol=symbol,
            entry_side=entry_side,
            take_profit_price=tp_price,
            close_position=True # 明确指定使用仓位止盈
        )
        print(f"止盈单成功，订单ID: {tp_res.tp_order_id}")
        
        print("步骤2完成。")
        
    except Exception as e:
        print(f"设置止盈止损失败: {e}")

if __name__ == "__main__":
    main()
