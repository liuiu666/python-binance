import sys
import unittest
import time
from decimal import Decimal
from pathlib import Path

# 将 src 目录添加到 sys.path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_skills import Settings, create_client
from trading_skills.order_executor import OrderExecutor

class TestOrderExecutorReal(unittest.TestCase):
    # 类变量用于在不同测试方法间传递数据
    settings = None
    client = None
    executor = None
    symbol = "DOGEUSDT" # 使用真实存在的交易对进行测试
    position_amt = Decimal("0")
    entry_price = Decimal("0")
    tp_order_id = None
    sl_order_id = None

    @classmethod
    def setUpClass(cls):
        print("正在初始化真实交易环境...")
        cls.settings = Settings.load(ROOT)
        cls.client = create_client(cls.settings)
        cls.executor = OrderExecutor(cls.client)
        print(f"测试交易对: {cls.symbol}")
        
        # 确保开始前没有挂单和持仓 (为了安全，这里只打印警告，不自动强平，除非确认是测试用的)
        # 建议用户在一个干净的子账户或测试网运行
        try:
            positions = cls.client.futures_position_information(symbol=cls.symbol)
            for pos in positions:
                if float(pos['positionAmt']) != 0:
                    print(f"警告: 检测到 {cls.symbol} 已有持仓: {pos['positionAmt']}，测试可能会受影响！")
        except Exception as e:
            print(f"初始化检查失败: {e}")

    def test_01_place_order(self):
        """1. 测试下单 (开仓)"""
        print("\n=== 步骤1: 测试下单 (开仓) ===")
        # 获取当前价格
        try:
            ticker = self.client.futures_symbol_ticker(symbol=self.symbol)
            price = Decimal(ticker['price'])
            print(f"当前价格: {price}")
        except Exception as e:
            self.fail(f"获取价格失败: {e}")
        
        # 计算数量 (至少 6 USDT 以满足 5 USDT 限制)
        qty_val = Decimal("6") / price
        # 获取精度规则
        try:
            rules = self.executor._ex.get_symbol_rules(self.symbol)
        except Exception as e:
            self.fail(f"获取规则失败: {e}")

        # 向上取整或按精度处理，确保满足最小数量
        qty = round(qty_val, rules.quantity_precision)
        if rules.quantity_precision == 0:
            qty = int(qty)
        
        # 确保大于 min_qty
        if qty < rules.min_qty:
            qty = rules.min_qty
            
        print(f"计划下单数量: {qty}")
        
        # 市价开多
        try:
            order = self.client.futures_create_order(
                symbol=self.symbol,
                side="BUY",
                type="MARKET",
                quantity=str(qty)
            )
            print("下单成功!")
            print(f"订单ID: {order['orderId']}")
        except Exception as e:
            self.fail(f"下单失败: {e}")
        
        # 等待成交
        time.sleep(2)
        
        # 查询持仓
        positions = self.client.futures_position_information(symbol=self.symbol)
        found = False
        for pos in positions:
            if float(pos['positionAmt']) != 0:
                TestOrderExecutorReal.position_amt = Decimal(pos['positionAmt'])
                TestOrderExecutorReal.entry_price = Decimal(pos['entryPrice'])
                print(f"当前持仓: {TestOrderExecutorReal.position_amt}, 入场价: {TestOrderExecutorReal.entry_price}")
                found = True
                break
        
        if not found:
            self.fail("未检测到持仓，可能下单未成交或延迟")

    def test_02_place_tp_sl(self):
        """2. 测试止盈止损"""
        print("\n=== 步骤2: 测试止盈止损 ===")
        if TestOrderExecutorReal.position_amt == 0:
            self.skipTest("无持仓，跳过")
            
        entry_price = TestOrderExecutorReal.entry_price
        quantity = abs(TestOrderExecutorReal.position_amt)
        
        # 止损价: -1%
        sl_price = entry_price * Decimal("0.99")
        # 止盈价: +1%
        tp_price = entry_price * Decimal("1.01")
        
        print(f"设置止损价: {sl_price}, 止盈价: {tp_price}")
        
        try:
            # 下止损单
            sl_res = self.executor.place_stop_loss_market(
                symbol=self.symbol,
                entry_side="BUY", # 我们是开多，所以 entry_side 是 BUY
                quantity=quantity,
                stop_price=sl_price
            )
            TestOrderExecutorReal.sl_order_id = sl_res.stop_order_id
            print(f"止损单下单成功: {sl_res.stop_order_id}")
            
            # 下止盈单
            tp_res = self.executor.place_take_profit_market(
                symbol=self.symbol,
                entry_side="BUY",
                quantity=quantity,
                take_profit_price=tp_price
            )
            TestOrderExecutorReal.tp_order_id = tp_res.tp_order_id
            print(f"止盈单下单成功: {tp_res.tp_order_id}")
        except Exception as e:
            self.fail(f"设置止盈止损失败: {e}")

    def test_03_modify_tp_sl(self):
        """3. 修改止盈止损"""
        print("\n=== 步骤3: 修改止盈止损 ===")
        if not TestOrderExecutorReal.sl_order_id:
            self.skipTest("无止损单，跳过")
            
        # 撤销旧单
        try:
            print(f"撤销旧止损单: {TestOrderExecutorReal.sl_order_id}")
            self.client.futures_cancel_order(symbol=self.symbol, orderId=TestOrderExecutorReal.sl_order_id)
            print(f"撤销旧止盈单: {TestOrderExecutorReal.tp_order_id}")
            self.client.futures_cancel_order(symbol=self.symbol, orderId=TestOrderExecutorReal.tp_order_id)
        except Exception as e:
            print(f"撤单警告 (可能已触发?): {e}")
        
        # 下新单 (价格稍微变动)
        entry_price = TestOrderExecutorReal.entry_price
        quantity = abs(TestOrderExecutorReal.position_amt)
        
        new_sl_price = entry_price * Decimal("0.98") # 扩大止损
        new_tp_price = entry_price * Decimal("1.02") # 扩大止盈
        
        print(f"设置新止损价: {new_sl_price}, 新止盈价: {new_tp_price}")
        
        try:
            sl_res = self.executor.place_stop_loss_market(
                symbol=self.symbol,
                entry_side="BUY",
                quantity=quantity,
                stop_price=new_sl_price
            )
            TestOrderExecutorReal.sl_order_id = sl_res.stop_order_id
            print(f"新止损单下单成功: {sl_res.stop_order_id}")
            
            tp_res = self.executor.place_take_profit_market(
                symbol=self.symbol,
                entry_side="BUY",
                quantity=quantity,
                take_profit_price=new_tp_price
            )
            TestOrderExecutorReal.tp_order_id = tp_res.tp_order_id
            print(f"新止盈单下单成功: {tp_res.tp_order_id}")
        except Exception as e:
            self.fail(f"修改止盈止损失败: {e}")

    def test_04_close_position(self):
        """4. 平仓"""
        print("\n=== 步骤4: 平仓 ===")
        if TestOrderExecutorReal.position_amt == 0:
            self.skipTest("无持仓，跳过")
            
        # 撤销所有挂单 (TP/SL)
        print("撤销所有挂单...")
        try:
            self.client.futures_cancel_all_open_orders(symbol=self.symbol)
        except Exception as e:
            print(f"撤销挂单失败: {e}")
        
        # 市价全平
        print("正在市价平仓...")
        quantity = abs(TestOrderExecutorReal.position_amt)
        # 如果是多单，就卖出平仓
        side = "SELL" if TestOrderExecutorReal.position_amt > 0 else "BUY"
        
        try:
            self.client.futures_create_order(
                symbol=self.symbol,
                side=side,
                type="MARKET",
                quantity=str(quantity),
                reduceOnly=True
            )
            print("平仓成功!")
        except Exception as e:
            self.fail(f"平仓失败: {e}")
        
        # 验证持仓归零
        time.sleep(2)
        positions = self.client.futures_position_information(symbol=self.symbol)
        current_amt = 0
        for pos in positions:
            if float(pos['positionAmt']) != 0:
                current_amt = float(pos['positionAmt'])
                break
        
        if current_amt == 0:
            print("持仓已确认归零。")
        else:
            print(f"警告: 持仓未归零，剩余: {current_amt}")

if __name__ == "__main__":
    unittest.main()
