# USDT-M期货账户管理功能

## 概述
python-binance库为USDT-M期货提供了全面的账户管理功能，包括账户余额查询、持仓信息、杠杆设置、保证金管理、交易历史等核心账户操作。

## 账户信息查询

### 1. 账户余额 (futures_account_balance)
- **功能**: 查询期货账户的资产余额
- **用法**:
```python
balance = client.futures_account_balance()
```
- **返回**: 包含各种资产的余额信息
```python
[
    {
        "accountAlias": "SgsR",
        "asset": "USDT",
        "balance": "1000.00000000",
        "crossWalletBalance": "1000.00000000",
        "crossUnPnl": "0.00000000",
        "availableBalance": "1000.00000000",
        "maxWithdrawAmount": "1000.00000000"
    }
]
```

### 2. 账户详细信息 (futures_account)
- **功能**: 获取账户的详细信息，包括持仓、余额等
- **用法**:
```python
account_info = client.futures_account()
```
- **返回**: 完整的账户信息，包括:
  - 总钱包余额
  - 总未实现盈亏
  - 总保证金余额
  - 总初始保证金
  - 总维持保证金
  - 所有持仓信息
  - 所有资产信息

### 3. 账户配置 (futures_account_config)
- **功能**: 获取账户配置信息
- **用法**:
```python
config = client.futures_account_config()
```

### 4. 交易对配置 (futures_symbol_config)
- **功能**: 获取特定交易对的配置信息
- **用法**:
```python
symbol_config = client.futures_symbol_config(symbol="BTCUSDT")
```

## 持仓管理

### 5. 持仓信息 (futures_position_information)
- **功能**: 查询当前持仓信息
- **用法**:
```python
# 查询所有持仓
positions = client.futures_position_information()

# 查询特定交易对持仓
positions = client.futures_position_information(symbol="BTCUSDT")
```
- **返回信息包括**:
  - 持仓数量
  - 入场价格
  - 标记价格
  - 未实现盈亏
  - 持仓方向
  - 保证金类型

### 6. 持仓模式查询 (futures_get_position_mode)
- **功能**: 查询当前的持仓模式
- **用法**:
```python
position_mode = client.futures_get_position_mode()
```
- **返回**: 
  - `true`: 双向持仓模式
  - `false`: 单向持仓模式

### 7. 多资产模式查询 (futures_get_multi_assets_mode)
- **功能**: 查询多资产模式状态
- **用法**:
```python
multi_assets_mode = client.futures_get_multi_assets_mode()
```

## 杠杆和保证金管理

### 8. 调整杠杆倍数 (futures_change_leverage)
- **功能**: 调整指定交易对的杠杆倍数
- **用法**:
```python
result = client.futures_change_leverage(
    symbol="BTCUSDT",
    leverage=10  # 设置10倍杠杆
)
```
- **参数**:
  - `symbol`: 交易对符号 (必需)
  - `leverage`: 杠杆倍数 1-125 (必需)

### 9. 更改保证金模式 (futures_change_margin_type)
- **功能**: 更改交易对的保证金模式
- **用法**:
```python
result = client.futures_change_margin_type(
    symbol="BTCUSDT",
    marginType="ISOLATED"  # ISOLATED 或 CROSSED
)
```
- **参数**:
  - `symbol`: 交易对符号 (必需)
  - `marginType`: 保证金类型 ISOLATED(逐仓) 或 CROSSED(全仓) (必需)

### 10. 调整逐仓保证金 (futures_change_position_margin)
- **功能**: 调整逐仓模式下的持仓保证金
- **用法**:
```python
result = client.futures_change_position_margin(
    symbol="BTCUSDT",
    positionSide="LONG",  # LONG, SHORT, 或 BOTH
    amount=100.0,         # 调整金额
    type=1               # 1: 增加保证金, 2: 减少保证金
)
```

### 11. 保证金变动历史 (futures_position_margin_history)
- **功能**: 查询保证金变动历史记录
- **用法**:
```python
margin_history = client.futures_position_margin_history(
    symbol="BTCUSDT",
    type=None,      # 1: 增加保证金, 2: 减少保证金
    startTime=None,
    endTime=None,
    limit=500
)
```

### 12. 杠杆分层标准 (futures_leverage_bracket)
- **功能**: 查询杠杆分层标准
- **用法**:
```python
# 查询所有交易对的杠杆分层
leverage_brackets = client.futures_leverage_bracket()

# 查询特定交易对的杠杆分层
leverage_brackets = client.futures_leverage_bracket(symbol="BTCUSDT")
```

## 交易历史

### 13. 账户成交历史 (futures_account_trades)
- **功能**: 查询账户的成交历史
- **用法**:
```python
trades = client.futures_account_trades(
    symbol="BTCUSDT",  # 可选，不指定则查询所有交易对
    startTime=None,
    endTime=None,
    fromId=None,
    limit=500
)
```

### 14. 收益历史 (futures_income_history)
- **功能**: 查询收益历史记录
- **用法**:
```python
income_history = client.futures_income_history(
    symbol="BTCUSDT",  # 可选
    incomeType=None,   # 收益类型: TRANSFER, WELCOME_BONUS, REALIZED_PNL, FUNDING_FEE, COMMISSION, INSURANCE_CLEAR, REFERRAL_KICKBACK, COMMISSION_REBATE, API_REBATE, CONTEST_REWARD, CROSS_COLLATERAL_TRANSFER, OPTIONS_PREMIUM_FEE, OPTIONS_SETTLE_PROFIT, INTERNAL_TRANSFER, AUTO_EXCHANGE, DELIVERED_SETTELMENT, COIN_SWAP_DEPOSIT, COIN_SWAP_WITHDRAW, POSITION_LIMIT_INCREASE_FEE
    startTime=None,
    endTime=None,
    limit=100
)
```

## 风险管理

### 15. 手续费率 (futures_commission_rate)
- **功能**: 查询交易手续费率
- **用法**:
```python
commission_rate = client.futures_commission_rate(symbol="BTCUSDT")
```

### 16. ADL队列估算 (futures_adl_quantile_estimate)
- **功能**: 查询ADL(自动减仓)队列估算
- **用法**:
```python
adl_quantile = client.futures_adl_quantile_estimate(symbol="BTCUSDT")
```

### 17. API交易状态 (futures_api_trading_status)
- **功能**: 查询API交易状态
- **用法**:
```python
trading_status = client.futures_api_trading_status()
```

## 数据流管理

### 18. 获取监听密钥 (futures_stream_get_listen_key)
- **功能**: 获取用户数据流的监听密钥
- **用法**:
```python
listen_key = client.futures_stream_get_listen_key()
```

### 19. 延长监听密钥有效期 (futures_stream_keepalive)
- **功能**: 延长监听密钥的有效期
- **用法**:
```python
client.futures_stream_keepalive(listenKey="your_listen_key")
```

### 20. 关闭数据流 (futures_stream_close)
- **功能**: 关闭用户数据流
- **用法**:
```python
client.futures_stream_close(listenKey="your_listen_key")
```

## 完整示例

```python
from binance.client import Client
import json

# 初始化客户端
client = Client(api_key, api_secret)

try:
    # 1. 查询账户余额
    balance = client.futures_account_balance()
    print("账户余额:")
    for asset in balance:
        if float(asset['balance']) > 0:
            print(f"  {asset['asset']}: {asset['balance']}")
    
    # 2. 查询账户详细信息
    account = client.futures_account()
    print(f"\n总钱包余额: {account['totalWalletBalance']} USDT")
    print(f"总未实现盈亏: {account['totalUnrealizedProfit']} USDT")
    print(f"可用余额: {account['availableBalance']} USDT")
    
    # 3. 查询持仓信息
    positions = client.futures_position_information()
    print("\n当前持仓:")
    for pos in positions:
        if float(pos['positionAmt']) != 0:
            print(f"  {pos['symbol']}: {pos['positionAmt']} @ {pos['entryPrice']}")
            print(f"    未实现盈亏: {pos['unRealizedProfit']} USDT")
    
    # 4. 设置杠杆倍数
    leverage_result = client.futures_change_leverage(
        symbol="BTCUSDT",
        leverage=10
    )
    print(f"\nBTCUSDT杠杆设置为: {leverage_result['leverage']}倍")
    
    # 5. 查询交易历史
    trades = client.futures_account_trades(
        symbol="BTCUSDT",
        limit=10
    )
    print(f"\n最近10笔交易:")
    for trade in trades[-5:]:  # 显示最后5笔
        print(f"  {trade['time']}: {trade['side']} {trade['qty']} @ {trade['price']}")
    
    # 6. 查询收益历史
    income = client.futures_income_history(limit=10)
    print(f"\n最近收益记录:")
    for record in income[-3:]:  # 显示最后3条
        print(f"  {record['incomeType']}: {record['income']} {record['asset']}")
    
    # 7. 查询手续费率
    commission = client.futures_commission_rate(symbol="BTCUSDT")
    print(f"\nBTCUSDT手续费率:")
    print(f"  Maker: {float(commission['makerCommissionRate']) * 100:.4f}%")
    print(f"  Taker: {float(commission['takerCommissionRate']) * 100:.4f}%")

except Exception as e:
    print(f"查询失败: {e}")
```

## 注意事项

1. **权限要求**: 所有账户查询功能都需要有效的API密钥和签名
2. **杠杆风险**: 调整杠杆倍数会影响风险水平，请谨慎操作
3. **保证金模式**: 更改保证金模式前确保没有持仓
4. **数据更新**: 账户数据可能有轻微延迟
5. **频率限制**: 注意API调用频率限制
6. **风险管理**: 定期检查持仓和风险指标
7. **资金安全**: 妥善保管API密钥，建议使用只读权限进行查询操作