# USDT-M期货订单管理功能

## 概述
python-binance库为USDT-M期货提供了完整的订单管理功能，包括创建订单、修改订单、取消订单、查询订单状态等核心交易操作。

## 订单创建

### 1. 创建订单 (futures_create_order)
- **功能**: 创建新的期货订单
- **用法**: 
```python
order = client.futures_create_order(
    symbol="BTCUSDT",
    side="BUY",  # BUY 或 SELL
    positionSide="LONG",  # LONG, SHORT, 或 BOTH
    type="LIMIT",  # MARKET, LIMIT, STOP, TAKE_PROFIT 等
    timeInForce="GTC",  # GTC, IOC, FOK
    quantity=0.1,
    price="50000.0",
    reduceOnly=False,
    newClientOrderId="my_order_001"
)
```
- **参数说明**:
  - `symbol`: 交易对符号 (必需)
  - `side`: 买卖方向 BUY/SELL (必需)
  - `positionSide`: 持仓方向 LONG/SHORT/BOTH (必需)
  - `type`: 订单类型 (必需)
  - `timeInForce`: 订单有效期类型
  - `quantity`: 订单数量 (必需)
  - `price`: 订单价格 (限价单必需)
  - `reduceOnly`: 是否为只减仓订单
  - `newClientOrderId`: 客户端订单ID

### 2. 测试订单 (futures_create_test_order)
- **功能**: 创建测试订单，不会实际执行
- **用法**: 
```python
client.futures_create_test_order(
    symbol="BTCUSDT",
    side="BUY",
    positionSide="LONG",
    type="LIMIT",
    timeInForce="GTC",
    quantity=0.1,
    price="50000.0"
)
```

### 3. 批量下单 (futures_place_batch_order)
- **功能**: 一次性创建多个订单
- **用法**:
```python
orders = client.futures_place_batch_order(
    batchOrders=[
        {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "positionSide": "LONG",
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": "0.1",
            "price": "49000.0"
        },
        {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "positionSide": "SHORT",
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": "0.1",
            "price": "51000.0"
        }
    ]
)
```

## 订单修改

### 4. 修改订单 (futures_modify_order)
- **功能**: 修改现有订单的价格和数量
- **用法**:
```python
modified_order = client.futures_modify_order(
    orderid=12345678,  # 或使用 origClientOrderId
    symbol="BTCUSDT",
    side="BUY",
    quantity=0.2,  # 新数量
    price="49500.0"  # 新价格
)
```

## 订单查询

### 5. 查询订单 (futures_get_order)
- **功能**: 查询特定订单的详细信息
- **用法**:
```python
order = client.futures_get_order(
    symbol="BTCUSDT",
    orderid=12345678  # 或使用 origClientOrderId="my_order_001"
)
```

### 6. 查询当前挂单 (futures_get_open_orders)
- **功能**: 查询当前所有未成交的订单
- **用法**:
```python
# 查询所有交易对的挂单
open_orders = client.futures_get_open_orders()

# 查询特定交易对的挂单
open_orders = client.futures_get_open_orders(symbol="BTCUSDT")
```

### 7. 查询所有订单 (futures_get_all_orders)
- **功能**: 查询历史订单记录
- **用法**:
```python
all_orders = client.futures_get_all_orders(
    symbol="BTCUSDT",
    orderId=None,  # 从指定订单ID开始查询
    startTime=None,  # 开始时间
    endTime=None,    # 结束时间
    limit=500        # 返回数量限制
)
```

## 订单取消

### 8. 取消订单 (futures_cancel_order)
- **功能**: 取消特定的订单
- **用法**:
```python
cancelled_order = client.futures_cancel_order(
    symbol="BTCUSDT",
    orderid=12345678  # 或使用 origClientOrderId="my_order_001"
)
```

### 9. 批量取消订单 (futures_cancel_orders)
- **功能**: 批量取消多个订单
- **用法**:
```python
# 使用订单ID列表取消
cancelled_orders = client.futures_cancel_orders(
    symbol="BTCUSDT",
    orderidlist=[12345678, 12345679, 12345680]
)

# 使用客户端订单ID列表取消
cancelled_orders = client.futures_cancel_orders(
    symbol="BTCUSDT",
    origclientorderidlist=["order_001", "order_002", "order_003"]
)
```

### 10. 取消所有挂单 (futures_cancel_all_open_orders)
- **功能**: 取消指定交易对的所有挂单
- **用法**:
```python
client.futures_cancel_all_open_orders(symbol="BTCUSDT")
```

### 11. 倒计时取消所有订单 (futures_countdown_cancel_all)
- **功能**: 设置倒计时，到时自动取消所有订单
- **用法**:
```python
client.futures_countdown_cancel_all(
    symbol="BTCUSDT",
    countdownTime=60000  # 60秒后取消所有订单
)
```

## 订单类型详解

### 市价单 (MARKET)
```python
client.futures_create_order(
    symbol="BTCUSDT",
    side="BUY",
    positionSide="LONG",
    type="MARKET",
    quantity=0.1
)
```

### 限价单 (LIMIT)
```python
client.futures_create_order(
    symbol="BTCUSDT",
    side="BUY",
    positionSide="LONG",
    type="LIMIT",
    timeInForce="GTC",
    quantity=0.1,
    price="50000.0"
)
```

### 止损单 (STOP/STOP_MARKET)
```python
client.futures_create_order(
    symbol="BTCUSDT",
    side="SELL",
    positionSide="LONG",
    type="STOP_MARKET",
    quantity=0.1,
    stopPrice="49000.0"
)
```

### 止盈单 (TAKE_PROFIT/TAKE_PROFIT_MARKET)
```python
client.futures_create_order(
    symbol="BTCUSDT",
    side="SELL",
    positionSide="LONG",
    type="TAKE_PROFIT_MARKET",
    quantity=0.1,
    stopPrice="51000.0"
)
```

### 跟踪止损单 (TRAILING_STOP_MARKET)
```python
client.futures_create_order(
    symbol="BTCUSDT",
    side="SELL",
    positionSide="LONG",
    type="TRAILING_STOP_MARKET",
    quantity=0.1,
    callbackRate=1.0  # 回调比率1%
)
```

## 订单状态说明

- **NEW**: 新建订单
- **PARTIALLY_FILLED**: 部分成交
- **FILLED**: 完全成交
- **CANCELED**: 已取消
- **REJECTED**: 被拒绝
- **EXPIRED**: 已过期

## 时间有效期类型 (TimeInForce)

- **GTC** (Good Till Cancel): 订单一直有效直到被取消
- **IOC** (Immediate Or Cancel): 立即成交或取消
- **FOK** (Fill Or Kill): 全部成交或全部取消

## 完整示例

```python
from binance.client import Client

# 初始化客户端
client = Client(api_key, api_secret)

try:
    # 1. 创建限价买单
    buy_order = client.futures_create_order(
        symbol="BTCUSDT",
        side="BUY",
        positionSide="LONG",
        type="LIMIT",
        timeInForce="GTC",
        quantity=0.1,
        price="49000.0",
        newClientOrderId="buy_order_001"
    )
    print(f"买单创建成功: {buy_order['orderId']}")
    
    # 2. 查询订单状态
    order_status = client.futures_get_order(
        symbol="BTCUSDT",
        orderid=buy_order['orderId']
    )
    print(f"订单状态: {order_status['status']}")
    
    # 3. 如果需要，修改订单
    if order_status['status'] == 'NEW':
        modified_order = client.futures_modify_order(
            orderid=buy_order['orderId'],
            symbol="BTCUSDT",
            side="BUY",
            quantity=0.15,  # 修改数量
            price="48500.0"  # 修改价格
        )
        print(f"订单修改成功: {modified_order['orderId']}")
    
    # 4. 查询所有挂单
    open_orders = client.futures_get_open_orders(symbol="BTCUSDT")
    print(f"当前挂单数量: {len(open_orders)}")
    
    # 5. 取消订单（如果需要）
    # cancelled = client.futures_cancel_order(
    #     symbol="BTCUSDT",
    #     orderid=buy_order['orderId']
    # )
    
except Exception as e:
    print(f"订单操作失败: {e}")
```

## 注意事项

1. **权限要求**: 所有订单操作都需要有效的API密钥和签名
2. **资金要求**: 确保账户有足够的保证金
3. **风险管理**: 建议设置止损和止盈订单
4. **频率限制**: 注意API调用频率限制
5. **订单ID**: 保存好订单ID用于后续查询和管理
6. **错误处理**: 实现适当的错误处理机制
7. **测试环境**: 建议先在测试网环境进行测试