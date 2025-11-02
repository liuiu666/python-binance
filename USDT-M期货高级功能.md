# USDT-M期货高级功能

## 概述
python-binance库为USDT-M期货提供了多种高级功能，包括资金转账、历史数据下载、风险管理工具、API状态监控等专业交易功能。

## 资金管理

### 1. 期货账户转账 (futures_account_transfer)
- **功能**: 在现货账户和期货账户之间转账
- **用法**:
```python
transfer_result = client.futures_account_transfer(
    asset="USDT",
    amount=100.0,
    type=1  # 1: 现货转期货, 2: 期货转现货
)
```
- **参数说明**:
  - `asset`: 转账资产 (必需)
  - `amount`: 转账金额 (必需)
  - `type`: 转账类型 1(现货→期货) 或 2(期货→现货) (必需)

### 2. 转账历史查询 (transfer_history)
- **功能**: 查询资金转账历史记录
- **用法**:
```python
transfer_history = client.transfer_history(
    type="MAIN_UMFUTURE",  # 转账类型
    startTime=None,
    endTime=None,
    current=1,    # 页码
    size=10       # 每页数量
)
```

### 3. 跨抵押品借贷历史 (futures_cross_collateral_adjust_history)
- **功能**: 查询跨抵押品调整历史
- **用法**:
```python
adjust_history = client.futures_cross_collateral_adjust_history(
    loanCoin="USDT",
    collateralCoin="BTC",
    startTime=None,
    endTime=None,
    limit=500
)
```

### 4. 跨抵押品清算历史 (futures_cross_collateral_liquidation_history)
- **功能**: 查询跨抵押品清算历史
- **用法**:
```python
liquidation_history = client.futures_cross_collateral_liquidation_history(
    collateralCoin="BTC",
    startTime=None,
    endTime=None,
    limit=100
)
```

## 借贷功能

### 5. 期货借贷历史 (futures_loan_borrow_history)
- **功能**: 查询期货借贷历史
- **用法**:
```python
borrow_history = client.futures_loan_borrow_history(
    coin="USDT",
    startTime=None,
    endTime=None,
    limit=100
)
```

### 6. 期货还款历史 (futures_loan_repay_history)
- **功能**: 查询期货还款历史
- **用法**:
```python
repay_history = client.futures_loan_repay_history(
    coin="USDT",
    startTime=None,
    endTime=None,
    limit=100
)
```

### 7. 期货借贷钱包 (futures_loan_wallet)
- **功能**: 查询期货借贷钱包信息
- **用法**:
```python
loan_wallet = client.futures_loan_wallet()
```

### 8. 借贷利息历史 (futures_loan_interest_history)
- **功能**: 查询借贷利息历史
- **用法**:
```python
interest_history = client.futures_loan_interest_history(
    coin="USDT",
    startTime=None,
    endTime=None,
    limit=100
)
```

## 数据下载功能

### 9. 订单历史下载 (futures_account_order_history_download)
- **功能**: 下载账户订单历史数据
- **用法**:
```python
download_id = client.futures_account_order_history_download(
    startTime=1609459200000,  # 2021-01-01
    endTime=1640995200000     # 2022-01-01
)
```

### 10. 获取下载ID (futures_account_order_download_id)
- **功能**: 获取订单历史下载的ID
- **用法**:
```python
download_info = client.futures_account_order_download_id(downloadId="download_id_here")
```

### 11. 交易历史下载 (futures_account_trade_history_download)
- **功能**: 下载账户交易历史数据
- **用法**:
```python
download_id = client.futures_account_trade_history_download(
    startTime=1609459200000,
    endTime=1640995200000
)
```

### 12. 获取交易下载ID (futures_account_trade_download_id)
- **功能**: 获取交易历史下载的ID
- **用法**:
```python
download_info = client.futures_account_trade_download_id(downloadId="download_id_here")
```

## 持仓模式管理

### 13. 更改持仓模式 (futures_change_position_mode)
- **功能**: 更改持仓模式（单向/双向）
- **用法**:
```python
result = client.futures_change_position_mode(dualSidePosition=True)  # True: 双向, False: 单向
```

### 14. 查询持仓模式 (futures_get_position_mode)
- **功能**: 查询当前持仓模式
- **用法**:
```python
position_mode = client.futures_get_position_mode()
```

### 15. 更改多资产模式 (futures_change_multi_assets_mode)
- **功能**: 更改多资产模式
- **用法**:
```python
result = client.futures_change_multi_assets_mode(multiAssetsMargin=True)
```

## API状态和监控

### 16. API交易状态 (futures_api_trading_status)
- **功能**: 查询API交易状态和限制
- **用法**:
```python
trading_status = client.futures_api_trading_status()
```
- **返回信息包括**:
  - API交易状态
  - 违规次数
  - 解锁时间
  - 更新时间

### 17. 手续费率查询 (futures_commission_rate)
- **功能**: 查询交易手续费率
- **用法**:
```python
commission_rate = client.futures_commission_rate(symbol="BTCUSDT")
```

### 18. ADL队列估算 (futures_adl_quantile_estimate)
- **功能**: 查询自动减仓(ADL)队列估算
- **用法**:
```python
adl_quantile = client.futures_adl_quantile_estimate(symbol="BTCUSDT")
```

## 完整高级功能示例

### 资金管理示例
```python
from binance.client import Client
import time

class FundsManager:
    def __init__(self, api_key, api_secret):
        self.client = Client(api_key, api_secret)
    
    def transfer_to_futures(self, amount):
        """转账到期货账户"""
        try:
            result = self.client.futures_account_transfer(
                asset="USDT",
                amount=amount,
                type=1  # 现货转期货
            )
            print(f"转账成功: {amount} USDT 已转入期货账户")
            return result
        except Exception as e:
            print(f"转账失败: {e}")
            return None
    
    def transfer_to_spot(self, amount):
        """转账到现货账户"""
        try:
            result = self.client.futures_account_transfer(
                asset="USDT",
                amount=amount,
                type=2  # 期货转现货
            )
            print(f"转账成功: {amount} USDT 已转入现货账户")
            return result
        except Exception as e:
            print(f"转账失败: {e}")
            return None
    
    def get_transfer_history(self, days=30):
        """获取转账历史"""
        try:
            end_time = int(time.time() * 1000)
            start_time = end_time - (days * 24 * 60 * 60 * 1000)
            
            history = self.client.transfer_history(
                type="MAIN_UMFUTURE",
                startTime=start_time,
                endTime=end_time,
                size=100
            )
            
            print(f"最近{days}天转账记录:")
            for record in history.get('rows', []):
                print(f"  {record['timestamp']}: {record['amount']} {record['asset']} - {record['status']}")
            
            return history
        except Exception as e:
            print(f"查询转账历史失败: {e}")
            return None

# 使用示例
funds_manager = FundsManager(api_key, api_secret)
funds_manager.transfer_to_futures(100)
funds_manager.get_transfer_history(7)
```

### 数据下载示例
```python
from binance.client import Client
import time
import json

class DataDownloader:
    def __init__(self, api_key, api_secret):
        self.client = Client(api_key, api_secret)
    
    def download_order_history(self, start_date, end_date):
        """下载订单历史"""
        try:
            # 转换日期为时间戳
            start_time = int(time.mktime(time.strptime(start_date, "%Y-%m-%d")) * 1000)
            end_time = int(time.mktime(time.strptime(end_date, "%Y-%m-%d")) * 1000)
            
            # 请求下载
            download_result = self.client.futures_account_order_history_download(
                startTime=start_time,
                endTime=end_time
            )
            
            download_id = download_result['downloadId']
            print(f"订单历史下载请求已提交，下载ID: {download_id}")
            
            # 等待下载完成
            while True:
                status = self.client.futures_account_order_download_id(downloadId=download_id)
                
                if status['status'] == 'completed':
                    print(f"下载完成，下载链接: {status['url']}")
                    return status['url']
                elif status['status'] == 'failed':
                    print("下载失败")
                    return None
                else:
                    print(f"下载状态: {status['status']}")
                    time.sleep(10)  # 等待10秒后再次检查
                    
        except Exception as e:
            print(f"下载订单历史失败: {e}")
            return None
    
    def download_trade_history(self, start_date, end_date):
        """下载交易历史"""
        try:
            start_time = int(time.mktime(time.strptime(start_date, "%Y-%m-%d")) * 1000)
            end_time = int(time.mktime(time.strptime(end_date, "%Y-%m-%d")) * 1000)
            
            download_result = self.client.futures_account_trade_history_download(
                startTime=start_time,
                endTime=end_time
            )
            
            download_id = download_result['downloadId']
            print(f"交易历史下载请求已提交，下载ID: {download_id}")
            
            while True:
                status = self.client.futures_account_trade_download_id(downloadId=download_id)
                
                if status['status'] == 'completed':
                    print(f"下载完成，下载链接: {status['url']}")
                    return status['url']
                elif status['status'] == 'failed':
                    print("下载失败")
                    return None
                else:
                    print(f"下载状态: {status['status']}")
                    time.sleep(10)
                    
        except Exception as e:
            print(f"下载交易历史失败: {e}")
            return None

# 使用示例
downloader = DataDownloader(api_key, api_secret)
order_url = downloader.download_order_history("2023-01-01", "2023-12-31")
trade_url = downloader.download_trade_history("2023-01-01", "2023-12-31")
```

### 风险管理示例
```python
from binance.client import Client

class RiskManager:
    def __init__(self, api_key, api_secret):
        self.client = Client(api_key, api_secret)
    
    def check_api_status(self):
        """检查API状态"""
        try:
            status = self.client.futures_api_trading_status()
            
            if status['isLocked']:
                print(f"⚠️ API被锁定，解锁时间: {status['updateTime']}")
                print(f"违规次数: {status['triggerCondition']['GCR']}")
            else:
                print("✅ API状态正常")
            
            return status
        except Exception as e:
            print(f"检查API状态失败: {e}")
            return None
    
    def check_adl_risk(self, symbol):
        """检查ADL风险"""
        try:
            adl_quantile = self.client.futures_adl_quantile_estimate(symbol=symbol)
            
            print(f"{symbol} ADL队列位置:")
            if 'LONG' in adl_quantile:
                long_quantile = adl_quantile['LONG']
                print(f"  多头: {long_quantile}/5 (数字越大风险越高)")
            
            if 'SHORT' in adl_quantile:
                short_quantile = adl_quantile['SHORT']
                print(f"  空头: {short_quantile}/5 (数字越大风险越高)")
            
            return adl_quantile
        except Exception as e:
            print(f"检查ADL风险失败: {e}")
            return None
    
    def get_commission_rates(self, symbol):
        """获取手续费率"""
        try:
            rates = self.client.futures_commission_rate(symbol=symbol)
            
            maker_rate = float(rates['makerCommissionRate']) * 100
            taker_rate = float(rates['takerCommissionRate']) * 100
            
            print(f"{symbol} 手续费率:")
            print(f"  Maker: {maker_rate:.4f}%")
            print(f"  Taker: {taker_rate:.4f}%")
            
            return rates
        except Exception as e:
            print(f"获取手续费率失败: {e}")
            return None
    
    def comprehensive_risk_check(self, symbols):
        """综合风险检查"""
        print("=== 综合风险检查 ===")
        
        # 检查API状态
        self.check_api_status()
        print()
        
        # 检查各交易对的ADL风险
        for symbol in symbols:
            self.check_adl_risk(symbol)
            self.get_commission_rates(symbol)
            print()

# 使用示例
risk_manager = RiskManager(api_key, api_secret)
risk_manager.comprehensive_risk_check(["BTCUSDT", "ETHUSDT"])
```

## 注意事项

1. **权限要求**: 高级功能通常需要更高级别的API权限
2. **资金安全**: 转账操作需要特别谨慎，建议先小额测试
3. **数据下载**: 大量数据下载可能需要较长时间
4. **频率限制**: 某些功能有特殊的频率限制
5. **风险管理**: 定期检查API状态和风险指标
6. **合规要求**: 遵守当地法律法规和交易所规则
7. **备份策略**: 重要数据建议定期备份
8. **监控告警**: 建议设置风险监控和告警机制

## 功能状态说明

部分高级功能在不同环境下的可用性：
- ✅ **已实现**: 功能完全可用
- ⚠️ **部分实现**: 功能可用但有限制
- ❌ **未实现**: 功能暂不可用
- 🧪 **测试中**: 功能在测试阶段

具体功能状态请参考最新的API文档和测试结果。