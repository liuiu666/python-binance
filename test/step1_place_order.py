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
    print("=== 步骤1: 下单 (开仓) ===")
    
    # 初始化
    settings = Settings.load(ROOT)
    client = create_client(settings)
    executor = OrderExecutor(client)
    symbol = "DOGEUSDT"
    
    print(f"交易对: {symbol}")
    
    # 1. 检查当前持仓
    try:
        positions = client.futures_position_information(symbol=symbol)
        for pos in positions:
            amt = float(pos['positionAmt'])
            if amt != 0:
                print(f"警告: 当前已有持仓 {amt}，请先平仓后再测试下单。")
                return
    except Exception as e:
        print(f"检查持仓失败: {e}")
        return

    # 2. 获取价格
    try:
        ticker = client.futures_symbol_ticker(symbol=symbol)
        price = Decimal(ticker['price'])
        print(f"当前价格: {price}")
    except Exception as e:
        print(f"获取价格失败: {e}")
        return

    # 3. 计算数量 (约 6 USDT)
    qty_val = Decimal("6") / price
    
    try:
        rules = executor._ex.get_symbol_rules(symbol)
        # 按精度处理
        qty = round(qty_val, rules.quantity_precision)
        if rules.quantity_precision == 0:
            qty = int(qty)
        # 确保大于最小数量
        if qty < rules.min_qty:
            qty = rules.min_qty
            
        print(f"计划下单数量: {qty}")
        
    except Exception as e:
        print(f"获取规则或计算数量失败: {e}")
        return

    # 4. 下单
    try:
        print("正在发送市价买单...")
        order = client.futures_create_order(
            symbol=symbol,
            side="BUY",
            type="MARKET",
            quantity=str(qty)
        )
        print("下单成功!")
        print(f"订单ID: {order['orderId']}")
        
        # 验证成交
        time.sleep(2)
        positions = client.futures_position_information(symbol=symbol)
        for pos in positions:
            amt = float(pos['positionAmt'])
            if amt != 0:
                entry = pos['entryPrice']
                print(f"当前持仓: {amt}, 入场价: {entry}")
                print("开仓步骤完成。")
                return
                
        print("警告: 下单后未检测到持仓，可能未成交。")
        
    except Exception as e:
        print(f"下单失败: {e}")

if __name__ == "__main__":
    main()
