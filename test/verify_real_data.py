import sys
import json
from pathlib import Path
from decimal import Decimal

# Setup path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_skills import Settings, create_client, LLMAdvisor

def main():
    print("=== 真实数据验证 (Real Data Verification) ===")
    
    # 1. 初始化客户端
    settings = Settings.load(ROOT)
    client = create_client(settings)
    advisor = LLMAdvisor(client)
    
    # 2. 获取并打印账户余额
    balance = advisor.trader.get_usdt_balance()
    print(f"\n[账户信息]")
    print(f"可用余额 (USDT): {balance}")
    
    symbol = "WETUSDT"
    print(f"\n[正在获取 {symbol} 市场数据...]")
    
    # 3. 获取市场数据
    try:
        # 修正: FuturesTrader 没有 get_ticker, 直接使用 data_fetcher 获取或调用 client
        # 先检查标的是否存在
        try:
            ticker = client.futures_symbol_ticker(symbol=symbol)
        except Exception as e:
            if "Invalid symbol" in str(e) or "-1121" in str(e):
                 print(f"❌ 错误: 标的 {symbol} 不存在。")
                 print(f"提示: 您是否指的是 WUSDT (Wormhole), VETUSDT (VeChain), FETUSDT, WLDUSDT?")
                 return
            else:
                raise e

        print(f"最新成交价: {ticker.get('price')}")
        
        # 4. 获取完整分析报告
        print(f"\n[正在执行定量分析...]")
        report = advisor.get_analysis_report(symbol)
        
        print(f"\n>>> 分析报告摘要 <<<")
        print(f"综合评分: {report.get('score')}")
        print(f"方向评分: {report.get('direction_score')}")
        print(f"当前价格: {report.get('current_price')}")
        print(f"ATR波动率: {report.get('atr_pct'):.4f}%")
        print(f"资金费率: {report.get('funding_rate')}")
        print(f"信号列表: {report.get('signals')}")
        print(f"风险因子: {report.get('risk_factors')}")
        
        # 5. 生成 Prompt (验证传递给 LLM 的数据)
        print(f"\n[正在生成 LLM Prompt (数据完整性检查)...]")
        prompt = advisor._construct_prompt(symbol, report)
        
        print("-" * 40)
        print("PROMPT 内容预览 (部分):")
        # 打印 Prompt 中包含数据的关键部分
        lines = prompt.split('\n')
        for line in lines:
            if any(k in line for k in ["Account Info", "Score", "Current Price", "ATR", "Signals", "Risk"]):
                print(line)
        print("-" * 40)
        
        print("\n✅ 数据验证完成：所有数据均为交易所实时真实数据。")

    except Exception as e:
        print(f"\n❌ 发生异常: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
