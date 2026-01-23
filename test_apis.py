import sys
import os
import time

# 添加项目根目录到 sys.path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from handlers.binance_client import BinanceClient
from handlers.llm_client import LLMClient

def print_header(title):
    print(f"\n{'='*20} 测试: {title} {'='*20}")

def print_result(success, msg):
    if success:
        print(f"✅ [PASS] {msg}")
    else:
        print(f"❌ [FAIL] {msg}")

def test_binance_connectivity():
    print_header("Binance 连通性与账户")
    client = BinanceClient()
    
    # 1. 测试账户信息
    try:
        account = client.get_account_info()
        if account and 'balance' in account:
            bal = account['balance']
            print_result(True, f"获取账户成功. 权益: {bal.get('total_wallet_balance')}, 可用: {bal.get('available_balance')}")
        else:
            print_result(False, "获取账户失败或为空")
    except Exception as e:
        print_result(False, f"获取账户异常: {e}")

    return client

def test_binance_market_data(client):
    print_header("Binance 市场数据")
    symbol = 'BTCUSDT'
    
    # 1. 交易对状态
    try:
        symbols = client.get_trading_symbols()
        if symbols and symbol in symbols:
            print_result(True, f"获取交易对列表成功. 总数: {len(symbols)}, 包含 {symbol}")
        else:
            print_result(False, f"获取交易对列表失败或不包含 {symbol}")
    except Exception as e:
        print_result(False, f"交易对列表异常: {e}")

    # 2. 24h 行情
    try:
        tickers = client.get_ticker_24hr()
        if tickers and len(tickers) > 0:
            print_result(True, f"获取 24h 行情成功. 收到的数据量: {len(tickers)}")
        else:
            print_result(False, "获取 24h 行情失败")
    except Exception as e:
        print_result(False, f"24h 行情异常: {e}")

    # 3. 盘口数据
    try:
        book = client.get_book_tickers(symbol)
        if book and float(book['askPrice']) > 0 and float(book['bidPrice']) > 0:
            print_result(True, f"获取盘口成功. {symbol} Ask: {book['askPrice']}, Bid: {book['bidPrice']}")
        else:
            print_result(False, f"获取盘口失败: {book}")
    except Exception as e:
        print_result(False, f"盘口异常: {e}")

    # 4. 交易规则过滤器
    try:
        filters = client.get_symbol_filters(symbol)
        if filters and filters.get('min_notional') is not None:
            print_result(True, f"获取规则成功. {symbol} MinNotional: {filters['min_notional']}, TickSize: {filters['tick_size']}")
        else:
            print_result(False, f"获取规则失败: {filters}")
    except Exception as e:
        print_result(False, f"规则异常: {e}")

def test_llm_analysis():
    print_header("LLM 智能分析")
    llm = LLMClient()
    
    # 模拟数据
    mock_data = {
        "symbol": "TESTUSDT",
        "current_price": 100.0,
        "change_pct": 5.5,
        "atr": 2.0,
        "rsi": 75.0,
        "ma_status": "Bullish",
        "ma5": 102.0,
        "ma20": 98.0,
        "net_inflow": 1000000,
        "buy_sell_ratio": 1.5
    }
    
    try:
        print(">>> 发送模拟数据请求 LLM 分析...")
        signal, reason = llm.get_trading_advice(mock_data)
        if signal in ['BUY', 'SELL', 'HOLD'] and reason:
            print_result(True, f"LLM 响应正常. 信号: {signal}, 理由: {reason}")
        else:
            print_result(False, f"LLM 响应格式不符. 信号: {signal}, 理由: {reason}")
    except Exception as e:
        print_result(False, f"LLM 调用异常: {e}")

if __name__ == "__main__":
    print("🚀 开始全系统 API 检查...")
    client = test_binance_connectivity()
    if client:
        test_binance_market_data(client)
    test_llm_analysis()
    print("\n🏁 测试结束")
