# USDT-M期货WebSocket实时数据功能

## 概述
python-binance库为USDT-M期货提供了完整的WebSocket实时数据流功能，包括市场数据流、用户数据流、深度数据流等，支持实时获取价格变动、订单更新、持仓变化等信息。

## WebSocket管理器

### BinanceSocketManager
WebSocket功能通过`BinanceSocketManager`类实现，需要单独导入：

```python
from binance import BinanceSocketManager
from binance.client import Client
import asyncio

client = Client(api_key, api_secret)
bsm = BinanceSocketManager(client)
```

## 市场数据流

### 1. 价格行情流 (futures_ticker_socket)
- **功能**: 实时获取24小时价格统计数据
- **用法**:
```python
# 单个交易对
socket = bsm.futures_ticker_socket(symbol="BTCUSDT")

# 所有交易对
socket = bsm.futures_ticker_socket()

async def handle_socket_message(msg):
    print(f"价格更新: {msg}")

await bsm.start_socket(socket, handle_socket_message)
```

### 2. 最优挂单价格流 (futures_orderbook_ticker_socket)
- **功能**: 实时获取最优买卖价格
- **用法**:
```python
socket = bsm.futures_orderbook_ticker_socket(symbol="BTCUSDT")

async def handle_orderbook_ticker(msg):
    print(f"最优价格: 买{msg['b']} 卖{msg['a']}")

await bsm.start_socket(socket, handle_orderbook_ticker)
```

### 3. 深度数据流 (futures_depth_socket)
- **功能**: 实时获取订单簿深度数据
- **用法**:
```python
# 完整深度数据
socket = bsm.futures_depth_socket(symbol="BTCUSDT")

# 指定深度级别 (5, 10, 20)
socket = bsm.futures_depth_socket(symbol="BTCUSDT", depth=10)

# 更新频率 (100ms, 250ms, 500ms)
socket = bsm.futures_depth_socket(symbol="BTCUSDT", speed="100ms")

async def handle_depth(msg):
    print(f"深度更新: {len(msg['b'])}个买单, {len(msg['a'])}个卖单")

await bsm.start_socket(socket, handle_depth)
```

### 4. 增量深度流 (futures_diff_depth_socket)
- **功能**: 获取深度数据的增量更新
- **用法**:
```python
socket = bsm.futures_diff_depth_socket(symbol="BTCUSDT", speed="100ms")

async def handle_diff_depth(msg):
    print(f"深度增量: 最终更新ID {msg['u']}")

await bsm.start_socket(socket, handle_diff_depth)
```

### 5. K线数据流 (futures_kline_socket)
- **功能**: 实时获取K线数据
- **用法**:
```python
socket = bsm.futures_kline_socket(symbol="BTCUSDT", interval="1m")

async def handle_kline(msg):
    kline = msg['k']
    print(f"K线: {kline['c']} (收盘价)")

await bsm.start_socket(socket, handle_kline)
```

### 6. 连续合约K线流 (futures_continuous_kline_socket)
- **功能**: 获取连续合约的K线数据
- **用法**:
```python
socket = bsm.futures_continuous_kline_socket(
    pair="BTCUSDT",
    contract_type="perpetual",
    interval="1m"
)
```

### 7. 成交流 (futures_trade_socket)
- **功能**: 实时获取成交数据
- **用法**:
```python
socket = bsm.futures_trade_socket(symbol="BTCUSDT")

async def handle_trade(msg):
    print(f"成交: {msg['p']} 价格, {msg['q']} 数量")

await bsm.start_socket(socket, handle_trade)
```

### 8. 聚合成交流 (futures_aggTrade_socket)
- **功能**: 实时获取聚合成交数据
- **用法**:
```python
socket = bsm.futures_aggTrade_socket(symbol="BTCUSDT")

async def handle_agg_trade(msg):
    print(f"聚合成交: {msg['p']} 价格, {msg['q']} 数量")

await bsm.start_socket(socket, handle_agg_trade)
```

### 9. 标记价格流 (futures_mark_price_socket)
- **功能**: 实时获取标记价格
- **用法**:
```python
# 单个交易对
socket = bsm.futures_mark_price_socket(symbol="BTCUSDT", speed="1s")

# 所有交易对
socket = bsm.futures_mark_price_socket(speed="3s")

async def handle_mark_price(msg):
    print(f"标记价格: {msg['p']}")

await bsm.start_socket(socket, handle_mark_price)
```

### 10. 强平订单流 (futures_liquidation_socket)
- **功能**: 实时获取强平订单信息
- **用法**:
```python
# 单个交易对
socket = bsm.futures_liquidation_socket(symbol="BTCUSDT")

# 所有交易对
socket = bsm.futures_liquidation_socket()

async def handle_liquidation(msg):
    print(f"强平: {msg['o']['s']} {msg['o']['S']} {msg['o']['q']}")

await bsm.start_socket(socket, handle_liquidation)
```

## 用户数据流

### 11. 用户数据流 (futures_user_socket)
- **功能**: 实时获取账户更新、订单更新、持仓更新等用户相关数据
- **用法**:
```python
socket = bsm.futures_user_socket()

async def handle_user_data(msg):
    if msg['e'] == 'ACCOUNT_UPDATE':
        print("账户更新:", msg)
    elif msg['e'] == 'ORDER_TRADE_UPDATE':
        print("订单更新:", msg)
    elif msg['e'] == 'ACCOUNT_CONFIG_UPDATE':
        print("账户配置更新:", msg)

await bsm.start_socket(socket, handle_user_data)
```

用户数据流包含的事件类型：
- **ACCOUNT_UPDATE**: 账户余额和持仓更新
- **ORDER_TRADE_UPDATE**: 订单状态更新
- **ACCOUNT_CONFIG_UPDATE**: 账户配置更新（杠杆、保证金模式等）

## 组合数据流

### 12. 多路复用流 (futures_multiplex_socket)
- **功能**: 同时订阅多个数据流
- **用法**:
```python
streams = [
    "btcusdt@ticker",
    "ethusdt@ticker",
    "btcusdt@depth5@100ms",
    "ethusdt@kline_1m"
]

socket = bsm.futures_multiplex_socket(streams)

async def handle_multiplex(msg):
    stream = msg['stream']
    data = msg['data']
    print(f"流 {stream}: {data}")

await bsm.start_socket(socket, handle_multiplex)
```

## WebSocket API功能

### 13. WebSocket API请求
除了数据流，还支持通过WebSocket发送API请求：

```python
# 获取账户信息
ws_account = client.ws_futures_account()

# 获取持仓信息
ws_positions = client.ws_futures_account_position()

# 获取账户余额
ws_balance = client.ws_futures_account_balance()

# 创建订单
ws_order = client.ws_futures_create_order(
    symbol="BTCUSDT",
    side="BUY",
    positionSide="LONG",
    type="MARKET",
    quantity=0.1
)
```

## 完整示例

### 基础市场数据监控
```python
import asyncio
from binance import BinanceSocketManager
from binance.client import Client

async def main():
    client = Client()  # 市场数据不需要API密钥
    bsm = BinanceSocketManager(client)
    
    # 价格监控
    async def handle_ticker(msg):
        print(f"{msg['s']}: {msg['c']} ({msg['P']}%)")
    
    # K线监控
    async def handle_kline(msg):
        kline = msg['k']
        if kline['x']:  # K线已完成
            print(f"K线完成: {kline['s']} {kline['c']}")
    
    # 启动多个流
    ticker_socket = bsm.futures_ticker_socket(symbol="BTCUSDT")
    kline_socket = bsm.futures_kline_socket(symbol="BTCUSDT", interval="1m")
    
    await asyncio.gather(
        bsm.start_socket(ticker_socket, handle_ticker),
        bsm.start_socket(kline_socket, handle_kline)
    )

# 运行
asyncio.run(main())
```

### 用户数据监控
```python
import asyncio
from binance import BinanceSocketManager
from binance.client import Client

async def user_data_monitor():
    client = Client(api_key, api_secret)
    bsm = BinanceSocketManager(client)
    
    async def handle_user_data(msg):
        event_type = msg['e']
        
        if event_type == 'ACCOUNT_UPDATE':
            # 账户更新
            balances = msg['a']['B']
            positions = msg['a']['P']
            print("账户更新:")
            for balance in balances:
                if float(balance['wb']) > 0:
                    print(f"  余额: {balance['a']} = {balance['wb']}")
            
            for position in positions:
                if float(position['pa']) != 0:
                    print(f"  持仓: {position['s']} = {position['pa']}")
        
        elif event_type == 'ORDER_TRADE_UPDATE':
            # 订单更新
            order = msg['o']
            print(f"订单更新: {order['s']} {order['S']} {order['q']} @ {order['p']} - {order['X']}")
    
    socket = bsm.futures_user_socket()
    await bsm.start_socket(socket, handle_user_data)

# 运行
asyncio.run(user_data_monitor())
```

### 深度数据处理
```python
import asyncio
from binance import BinanceSocketManager
from binance.client import Client

class DepthManager:
    def __init__(self):
        self.bids = {}
        self.asks = {}
    
    def update_depth(self, msg):
        # 更新买单
        for bid in msg['b']:
            price, quantity = float(bid[0]), float(bid[1])
            if quantity == 0:
                self.bids.pop(price, None)
            else:
                self.bids[price] = quantity
        
        # 更新卖单
        for ask in msg['a']:
            price, quantity = float(ask[0]), float(ask[1])
            if quantity == 0:
                self.asks.pop(price, None)
            else:
                self.asks[price] = quantity
    
    def get_best_prices(self):
        if self.bids and self.asks:
            best_bid = max(self.bids.keys())
            best_ask = min(self.asks.keys())
            return best_bid, best_ask
        return None, None

async def depth_monitor():
    client = Client()
    bsm = BinanceSocketManager(client)
    depth_manager = DepthManager()
    
    async def handle_depth(msg):
        depth_manager.update_depth(msg)
        best_bid, best_ask = depth_manager.get_best_prices()
        if best_bid and best_ask:
            spread = best_ask - best_bid
            print(f"最优价格: {best_bid} / {best_ask}, 价差: {spread:.2f}")
    
    socket = bsm.futures_diff_depth_socket(symbol="BTCUSDT", speed="100ms")
    await bsm.start_socket(socket, handle_depth)

# 运行
asyncio.run(depth_monitor())
```

## 注意事项

1. **连接管理**: WebSocket连接需要适当的错误处理和重连机制
2. **数据处理**: 高频数据流需要高效的数据处理逻辑
3. **内存管理**: 长时间运行需要注意内存使用
4. **网络稳定性**: 网络不稳定时需要重连机制
5. **用户数据流**: 需要有效的API密钥和监听密钥
6. **频率限制**: 注意连接数量限制
7. **异步编程**: 使用asyncio进行异步处理
8. **错误处理**: 实现适当的异常处理机制

## 流名称参考

### 市场数据流
- `<symbol>@ticker` - 24小时价格统计
- `<symbol>@bookTicker` - 最优挂单价格
- `<symbol>@depth<levels>@<speed>` - 深度数据
- `<symbol>@depth@<speed>` - 增量深度
- `<symbol>@kline_<interval>` - K线数据
- `<symbol>@trade` - 成交数据
- `<symbol>@aggTrade` - 聚合成交
- `<symbol>@markPrice@<speed>` - 标记价格
- `<symbol>@forceOrder` - 强平订单

### 时间间隔
- 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d, 3d, 1w, 1M

### 更新频率
- 100ms, 250ms, 500ms, 1s, 3s