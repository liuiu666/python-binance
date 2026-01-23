---
name: "trade-executor"
description: "执行实际的加密货币交易指令，使用限价单(Limit Order)以保护滑点。当确定要开仓、平仓或调整仓位时调用。"
---

# Trade Executor Skill

此技能负责将交易策略生成的信号转换为实际的交易所订单。为了应对小币种的高波动和低流动性，本技能强制使用限价单机制。

## 功能特性
1. **滑点保护**：自动根据盘口价格计算限价单价格 (买单=Ask+0.2%, 卖单=Bid-0.2%)，拒绝市价单。
2. **数量计算**：根据 USDT 金额自动换算币种数量。
3. **安全检查**：下单前检查盘口数据是否正常。

## 使用方法

### 代码调用

```python
from handlers.trader import TradeExecutor

executor = TradeExecutor()

# symbol: 交易对 (如 'ALPACAUSDT')
# side: 方向 ('BUY' 或 'SELL')
# amount_usdt: 交易金额 (如 100 USDT)
# slippage: 允许滑点 (默认 0.002 即 0.2%)

order = executor.execute_trade(
    symbol='ALPACAUSDT', 
    side='BUY', 
    amount_usdt=100,
    slippage=0.002
)

if order:
    print(f"下单成功: {order}")
```

## 注意事项
- 调用此技能即代表**真实资金操作**（如果配置的是实盘 Key）。
- 确保账户有足够的 USDT 余额。
- 默认滑点设置为 0.2%，对于极端波动币种可能无法成交，此时订单会挂在盘口等待成交 (GTC)。
