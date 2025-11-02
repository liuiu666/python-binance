# USDT-M期货WebSocket API功能

## 概述
python-binance库支持通过WebSocket API进行期货交易操作，提供了与REST API相同的功能，但具有更低的延迟和更高的效率。WebSocket API适合需要快速响应的交易场景。

## WebSocket API基础

### 连接和认证
WebSocket API需要通过签名进行认证，类似于REST API：

```python
from binance.client import Client

client = Client(api_key, api_secret)

# WebSocket API会自动处理认证
```

## 市场数据查询

### 1. 获取订单簿 (ws_futures_get_order_book)
- **功能**: 通过WebSocket获取订单簿数据
- **用法**:
```python
order_book = client.ws_futures_get_order_book(
    symbol="BTCUSDT",
    limit=100
)
```

### 2. 获取最近成交 (ws_futures_get_recent_trades)
- **功能**: 获取最近的成交记录
- **用法**:
```python
recent_trades = client.ws_futures_get_recent_trades(
    symbol="BTCUSDT",
    limit=500
)
```

### 3. 获取历史成交 (ws_futures_get_historical_trades)
- **功能**: 获取历史成交数据
- **用法**:
```python
historical_trades = client.ws_futures_get_historical_trades(
    symbol="BTCUSDT",
    limit=500,
    fromId=None
)
```

### 4. 获取聚合成交 (ws_futures_get_aggregate_trades)
- **功能**: 获取聚合成交数据
- **用法**:
```python
agg_trades = client.ws_futures_get_aggregate_trades(
    symbol="BTCUSDT",
    fromId=None,
    startTime=None,
    endTime=None,
    limit=500
)
```

### 5. 获取K线数据 (ws_futures_get_klines)
- **功能**: 获取K线数据
- **用法**:
```python
klines = client.ws_futures_get_klines(
    symbol="BTCUSDT",
    interval="1h",
    startTime=None,
    endTime=None,
    limit=500
)
```

### 6. 获取连续合约K线 (ws_futures_get_continuous_klines)
- **功能**: 获取连续合约K线数据
- **用法**:
```python
continuous_klines = client.ws_futures_get_continuous_klines(
    pair="BTCUSDT",
    contractType="PERPETUAL",
    interval="1h",
    startTime=None,
    endTime=None,
    limit=500
)
```

### 7. 获取指数价格K线 (ws_futures_get_index_price_klines)
- **功能**: 获取指数价格K线
- **用法**:
```python
index_klines = client.ws_futures_get_index_price_klines(
    pair="BTCUSDT",
    interval="1h",
    startTime=None,
    endTime=None,
    limit=500
)
```

### 8. 获取标记价格K线 (ws_futures_get_mark_price_klines)
- **功能**: 获取标记价格K线
- **用法**:
```python
mark_klines = client.ws_futures_get_mark_price_klines(
    symbol="BTCUSDT",
    interval="1h",
    startTime=None,
    endTime=None,
    limit=500
)
```

### 9. 获取溢价指数K线 (ws_futures_get_premium_index_klines)
- **功能**: 获取溢价指数K线
- **用法**:
```python
premium_klines = client.ws_futures_get_premium_index_klines(
    symbol="BTCUSDT",
    interval="1h",
    startTime=None,
    endTime=None,
    limit=500
)
```

### 10. 获取24小时价格统计 (ws_futures_get_all_tickers)
- **功能**: 获取所有交易对的24小时价格统计
- **用法**:
```python
all_tickers = client.ws_futures_get_all_tickers()
```

### 11. 获取最优挂单价格 (ws_futures_get_orderbook_tickers)
- **功能**: 获取所有交易对的最优挂单价格
- **用法**:
```python
orderbook_tickers = client.ws_futures_get_orderbook_tickers()
```

## 账户信息查询

### 12. 获取账户信息 (ws_futures_account_position)
- **功能**: 获取账户和持仓信息
- **用法**:
```python
account_info = client.ws_futures_account_position()
```

### 13. 获取账户余额 (ws_futures_account_balance)
- **功能**: 获取账户余额
- **用法**:
```python
account_balance = client.ws_futures_account_balance()
```

### 14. 获取持仓信息 (ws_futures_position_information)
- **功能**: 获取持仓信息
- **用法**:
```python
# 获取所有持仓
positions = client.ws_futures_position_information()

# 获取特定交易对持仓
positions = client.ws_futures_position_information(symbol="BTCUSDT")
```

### 15. 获取账户成交历史 (ws_futures_account_trades)
- **功能**: 获取账户成交历史
- **用法**:
```python
account_trades = client.ws_futures_account_trades(
    symbol="BTCUSDT",
    startTime=None,
    endTime=None,
    fromId=None,
    limit=500
)
```

## 订单管理

### 16. 创建订单 (ws_futures_create_order)
- **功能**: 通过WebSocket创建新订单
- **用法**:
```python
order = client.ws_futures_create_order(
    symbol="BTCUSDT",
    side="BUY",
    positionSide="LONG",
    type="LIMIT",
    timeInForce="GTC",
    quantity=0.1,
    price="50000.0",
    reduceOnly=False,
    newClientOrderId="ws_order_001"
)
```

### 17. 修改订单 (ws_futures_modify_order)
- **功能**: 修改现有订单
- **用法**:
```python
modified_order = client.ws_futures_modify_order(
    orderid=12345678,
    symbol="BTCUSDT",
    side="BUY",
    quantity=0.2,
    price="49500.0"
)
```

### 18. 取消订单 (ws_futures_cancel_order)
- **功能**: 取消特定订单
- **用法**:
```python
cancelled_order = client.ws_futures_cancel_order(
    symbol="BTCUSDT",
    orderid=12345678
)
```

### 19. 取消所有挂单 (ws_futures_cancel_all_open_orders)
- **功能**: 取消指定交易对的所有挂单
- **用法**:
```python
result = client.ws_futures_cancel_all_open_orders(symbol="BTCUSDT")
```

### 20. 批量取消订单 (ws_futures_cancel_orders)
- **功能**: 批量取消多个订单
- **用法**:
```python
cancelled_orders = client.ws_futures_cancel_orders(
    symbol="BTCUSDT",
    orderidlist=[12345678, 12345679]
)
```

### 21. 查询订单 (ws_futures_get_order)
- **功能**: 查询特定订单信息
- **用法**:
```python
order_info = client.ws_futures_get_order(
    symbol="BTCUSDT",
    orderid=12345678
)
```

### 22. 查询挂单 (ws_futures_get_open_orders)
- **功能**: 查询当前挂单
- **用法**:
```python
# 查询所有挂单
open_orders = client.ws_futures_get_open_orders()

# 查询特定交易对挂单
open_orders = client.ws_futures_get_open_orders(symbol="BTCUSDT")
```

### 23. 查询所有订单 (ws_futures_get_all_orders)
- **功能**: 查询历史订单
- **用法**:
```python
all_orders = client.ws_futures_get_all_orders(
    symbol="BTCUSDT",
    orderId=None,
    startTime=None,
    endTime=None,
    limit=500
)
```

## 杠杆和保证金管理

### 24. 调整杠杆 (ws_futures_change_leverage)
- **功能**: 调整杠杆倍数
- **用法**:
```python
leverage_result = client.ws_futures_change_leverage(
    symbol="BTCUSDT",
    leverage=10
)
```

### 25. 更改保证金模式 (ws_futures_change_margin_type)
- **功能**: 更改保证金模式
- **用法**:
```python
margin_result = client.ws_futures_change_margin_type(
    symbol="BTCUSDT",
    marginType="ISOLATED"
)
```

### 26. 调整逐仓保证金 (ws_futures_change_position_margin)
- **功能**: 调整逐仓保证金
- **用法**:
```python
margin_result = client.ws_futures_change_position_margin(
    symbol="BTCUSDT",
    positionSide="LONG",
    amount=100.0,
    type=1  # 1: 增加, 2: 减少
)
```

## 完整交易示例

### 基础交易流程
```python
from binance.client import Client
import json

async def websocket_trading_example():
    client = Client(api_key, api_secret)
    
    try:
        # 1. 获取账户信息
        account = client.ws_futures_account_position()
        print("账户信息:", json.dumps(account, indent=2))
        
        # 2. 获取当前价格
        ticker = client.ws_futures_get_all_tickers()
        btc_price = None
        for t in ticker:
            if t['symbol'] == 'BTCUSDT':
                btc_price = float(t['price'])
                break
        
        print(f"当前BTC价格: {btc_price}")
        
        # 3. 设置杠杆
        leverage_result = client.ws_futures_change_leverage(
            symbol="BTCUSDT",
            leverage=10
        )
        print(f"杠杆设置: {leverage_result}")
        
        # 4. 创建限价买单
        buy_price = btc_price * 0.99  # 低于市价1%
        buy_order = client.ws_futures_create_order(
            symbol="BTCUSDT",
            side="BUY",
            positionSide="LONG",
            type="LIMIT",
            timeInForce="GTC",
            quantity=0.001,
            price=str(round(buy_price, 2)),
            newClientOrderId="ws_buy_001"
        )
        print(f"买单创建: {buy_order}")
        
        # 5. 查询订单状态
        order_status = client.ws_futures_get_order(
            symbol="BTCUSDT",
            orderid=buy_order['orderId']
        )
        print(f"订单状态: {order_status['status']}")
        
        # 6. 如果需要，取消订单
        if order_status['status'] == 'NEW':
            cancelled = client.ws_futures_cancel_order(
                symbol="BTCUSDT",
                orderid=buy_order['orderId']
            )
            print(f"订单已取消: {cancelled}")
        
    except Exception as e:
        print(f"交易失败: {e}")

# 运行示例
import asyncio
asyncio.run(websocket_trading_example())
```

### 高频交易示例
```python
from binance.client import Client
import time

class HighFrequencyTrader:
    def __init__(self, api_key, api_secret):
        self.client = Client(api_key, api_secret)
        self.symbol = "BTCUSDT"
        self.position_size = 0.001
        
    def get_market_data(self):
        """获取市场数据"""
        orderbook = self.client.ws_futures_get_order_book(
            symbol=self.symbol,
            limit=5
        )
        
        best_bid = float(orderbook['bids'][0][0])
        best_ask = float(orderbook['asks'][0][0])
        spread = best_ask - best_bid
        
        return best_bid, best_ask, spread
    
    def place_market_making_orders(self):
        """做市策略"""
        try:
            best_bid, best_ask, spread = self.get_market_data()
            
            # 计算下单价格
            buy_price = best_bid + 0.1
            sell_price = best_ask - 0.1
            
            # 取消现有订单
            self.client.ws_futures_cancel_all_open_orders(symbol=self.symbol)
            
            # 下买单
            buy_order = self.client.ws_futures_create_order(
                symbol=self.symbol,
                side="BUY",
                positionSide="LONG",
                type="LIMIT",
                timeInForce="GTC",
                quantity=self.position_size,
                price=str(round(buy_price, 2))
            )
            
            # 下卖单
            sell_order = self.client.ws_futures_create_order(
                symbol=self.symbol,
                side="SELL",
                positionSide="SHORT",
                type="LIMIT",
                timeInForce="GTC",
                quantity=self.position_size,
                price=str(round(sell_price, 2))
            )
            
            print(f"做市订单已下: 买{buy_price} 卖{sell_price}")
            return buy_order, sell_order
            
        except Exception as e:
            print(f"做市失败: {e}")
            return None, None
    
    def run_strategy(self):
        """运行策略"""
        while True:
            try:
                self.place_market_making_orders()
                time.sleep(1)  # 每秒更新一次
            except KeyboardInterrupt:
                print("策略停止")
                break
            except Exception as e:
                print(f"策略错误: {e}")
                time.sleep(5)

# 使用示例
# trader = HighFrequencyTrader(api_key, api_secret)
# trader.run_strategy()
```

## 性能优势

### WebSocket API vs REST API
1. **延迟更低**: WebSocket连接复用，减少连接建立时间
2. **效率更高**: 二进制协议，数据传输更快
3. **实时性强**: 持久连接，适合高频交易
4. **资源节省**: 减少HTTP头部开销

### 使用场景
- 高频交易策略
- 实时风险管理
- 快速订单管理
- 低延迟套利

## 注意事项

1. **连接管理**: WebSocket连接需要维护和监控
2. **错误处理**: 实现完善的错误处理和重连机制
3. **认证安全**: 确保API密钥安全
4. **频率限制**: 遵守API调用频率限制
5. **网络稳定**: 确保网络连接稳定
6. **数据同步**: 注意数据的时序性和一致性
7. **资源管理**: 合理管理连接资源
8. **监控告警**: 实现连接状态监控和告警机制

## 错误码参考

常见WebSocket API错误码：
- `-1000`: 未知错误
- `-1001`: 连接断开
- `-1002`: 未授权
- `-1003`: 请求过多
- `-2010`: 新订单被拒绝
- `-2011`: 取消订单被拒绝
- `-2013`: 订单不存在
- `-2014`: API密钥格式无效
- `-2015`: 无效的API密钥、IP或操作权限