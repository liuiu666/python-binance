# USDT-M期货市场数据功能

## 概述
python-binance库为USDT-M期货提供了全面的市场数据获取功能，包括实时价格、历史数据、K线图表、资金费率等多种市场信息。

## 基础连接功能

### 1. 连接测试 (futures_ping)
- **功能**: 测试与币安USDT-M期货API的连接状态
- **用法**: `client.futures_ping()`
- **返回**: 空响应，用于验证连接

### 2. 服务器时间 (futures_time)
- **功能**: 获取币安期货服务器的当前时间
- **用法**: `client.futures_time()`
- **返回**: 服务器时间戳

## 交易所信息

### 3. 交易所信息 (futures_exchange_info)
- **功能**: 获取USDT-M期货交易所的基本信息
- **用法**: `client.futures_exchange_info()`
- **返回**: 包含交易对信息、交易规则、费率限制等详细信息

## 订单簿和交易数据

### 4. 订单簿 (futures_order_book)
- **功能**: 获取指定交易对的订单簿数据
- **用法**: `client.futures_order_book(symbol="BTCUSDT", limit=100)`
- **参数**:
  - `symbol`: 交易对符号 (必需)
  - `limit`: 返回的订单数量 (可选，默认100)

### 5. 最近成交 (futures_recent_trades)
- **功能**: 获取最近的成交记录
- **用法**: `client.futures_recent_trades(symbol="BTCUSDT", limit=500)`
- **参数**:
  - `symbol`: 交易对符号 (必需)
  - `limit`: 返回的成交数量 (可选，默认500)

### 6. 历史成交 (futures_historical_trades)
- **功能**: 获取历史成交记录
- **用法**: `client.futures_historical_trades(symbol="BTCUSDT", limit=500, fromId=None)`
- **参数**:
  - `symbol`: 交易对符号 (必需)
  - `limit`: 返回的成交数量 (可选)
  - `fromId`: 从指定ID开始获取 (可选)

### 7. 聚合成交 (futures_aggregate_trades)
- **功能**: 获取聚合成交数据
- **用法**: `client.futures_aggregate_trades(symbol="BTCUSDT", fromId=None, startTime=None, endTime=None, limit=500)`

## K线数据

### 8. K线数据 (futures_klines)
- **功能**: 获取K线/蜡烛图数据
- **用法**: `client.futures_klines(symbol="BTCUSDT", interval="1h", startTime=None, endTime=None, limit=500)`
- **参数**:
  - `symbol`: 交易对符号 (必需)
  - `interval`: 时间间隔 (1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d, 3d, 1w, 1M)

### 9. 标记价格K线 (futures_mark_price_klines)
- **功能**: 获取标记价格的K线数据
- **用法**: `client.futures_mark_price_klines(symbol="BTCUSDT", interval="1h", startTime=None, endTime=None, limit=500)`

### 10. 指数价格K线 (futures_index_price_klines)
- **功能**: 获取指数价格的K线数据
- **用法**: `client.futures_index_price_klines(pair="BTCUSDT", interval="1h", startTime=None, endTime=None, limit=500)`

### 11. 溢价指数K线 (futures_premium_index_klines)
- **功能**: 获取溢价指数的K线数据
- **用法**: `client.futures_premium_index_klines(symbol="BTCUSDT", interval="1h", startTime=None, endTime=None, limit=500)`

### 12. 连续合约K线 (futures_continuous_klines)
- **功能**: 获取连续合约的K线数据
- **用法**: `client.futures_continuous_klines(pair="BTCUSDT", contractType="PERPETUAL", interval="1h", startTime=None, endTime=None, limit=500)`

### 13. 历史K线 (futures_historical_klines)
- **功能**: 获取历史K线数据
- **用法**: `client.futures_historical_klines(symbol="BTCUSDT", interval="1h", start_str="2023-01-01", end_str="2023-12-31")`

### 14. 历史K线生成器 (futures_historical_klines_generator)
- **功能**: 以生成器方式获取历史K线数据，适合大量数据处理
- **用法**: `client.futures_historical_klines_generator(symbol="BTCUSDT", interval="1h", start_str="2023-01-01")`

## 价格和费率信息

### 15. 标记价格 (futures_mark_price)
- **功能**: 获取标记价格信息
- **用法**: `client.futures_mark_price(symbol="BTCUSDT")`
- **返回**: 标记价格、指数价格、预估结算价格等

### 16. 资金费率 (futures_funding_rate)
- **功能**: 获取资金费率历史
- **用法**: `client.futures_funding_rate(symbol="BTCUSDT", startTime=None, endTime=None, limit=100)`

## 市场统计

### 17. 24小时价格统计 (futures_ticker)
- **功能**: 获取24小时价格变动统计
- **用法**: `client.futures_ticker(symbol="BTCUSDT")`
- **返回**: 开盘价、最高价、最低价、收盘价、成交量等

### 18. 单一交易对价格 (futures_symbol_ticker)
- **功能**: 获取单一交易对的最新价格
- **用法**: `client.futures_symbol_ticker(symbol="BTCUSDT")`

### 19. 订单簿价格 (futures_orderbook_ticker)
- **功能**: 获取最优买卖价格
- **用法**: `client.futures_orderbook_ticker(symbol="BTCUSDT")`

## 高级市场数据

### 20. 指数价格成分 (futures_index_price_constituents)
- **功能**: 获取指数价格的成分信息
- **用法**: `client.futures_index_price_constituents(symbol="BTCUSDT")`

### 21. 强平订单 (futures_liquidation_orders)
- **功能**: 获取强制平仓订单信息
- **用法**: `client.futures_liquidation_orders(symbol="BTCUSDT", startTime=None, endTime=None, limit=100)`

### 22. 持仓量 (futures_open_interest)
- **功能**: 获取持仓量信息
- **用法**: `client.futures_open_interest(symbol="BTCUSDT")`

### 23. 持仓量历史 (futures_open_interest_hist)
- **功能**: 获取持仓量历史数据
- **用法**: `client.futures_open_interest_hist(symbol="BTCUSDT", period="5m", limit=30, startTime=None, endTime=None)`

### 24. 指数信息 (futures_index_info)
- **功能**: 获取指数相关信息
- **用法**: `client.futures_index_info(symbol="BTCUSDT")`

## 大户持仓数据 (需要特殊权限)

### 25. 大户账户多空比 (futures_top_longshort_account_ratio)
- **功能**: 获取大户账户的多空持仓比例
- **用法**: `client.futures_top_longshort_account_ratio(symbol="BTCUSDT", period="5m", limit=30)`

### 26. 大户持仓多空比 (futures_top_longshort_position_ratio)
- **功能**: 获取大户持仓的多空比例
- **用法**: `client.futures_top_longshort_position_ratio(symbol="BTCUSDT", period="5m", limit=30)`

### 27. 全市场多空比 (futures_global_longshort_ratio)
- **功能**: 获取全市场的多空持仓比例
- **用法**: `client.futures_global_longshort_ratio(symbol="BTCUSDT", period="5m", limit=30)`

### 28. 合约主动买卖量 (futures_taker_longshort_ratio)
- **功能**: 获取主动买卖的多空比例
- **用法**: `client.futures_taker_longshort_ratio(symbol="BTCUSDT", period="5m", limit=30)`

## 使用示例

```python
from binance.client import Client

# 初始化客户端
client = Client(api_key, api_secret)

# 获取BTC/USDT的24小时统计
ticker = client.futures_ticker(symbol="BTCUSDT")
print(f"当前价格: {ticker['lastPrice']}")

# 获取K线数据
klines = client.futures_klines(symbol="BTCUSDT", interval="1h", limit=100)

# 获取资金费率
funding_rate = client.futures_funding_rate(symbol="BTCUSDT", limit=10)
```

## 注意事项

1. 部分功能需要API密钥权限
2. 某些高级数据功能可能需要特殊权限或在沙盒环境中不可用
3. 请注意API调用频率限制
4. 时间参数通常以毫秒为单位的Unix时间戳格式