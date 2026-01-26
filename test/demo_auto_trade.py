"""
自动化选币与分析演示
Combining SymbolSelector and LLMAdvisor
"""
import sys
import time
from pathlib import Path

# Setup path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_skills import Settings, create_client, LLMAdvisor, FuturesSymbolSelector

def main():
    print("=== 自动化智能选币与分析 (Auto Scan & Analyze) ===\n")
    
    # 1. 初始化
    settings = Settings.load(ROOT)
    client = create_client(settings)
    selector = FuturesSymbolSelector(client)
    advisor = LLMAdvisor(client)
    
    # 2. 选币 (使用 smart selection mode)
    mode = "cheap" # 可选: hot, small, cheap
    print(f"正在智能筛选候选币种 (模式: {mode}, 仅返回分值最高的一个)...")
    
    # 使用新方法 get_smart_candidates 替代 get_candidates_by_mode
    # limit=1 确保只返回最强的一个
    candidates = selector.get_smart_candidates(mode=mode, limit=1)
    print(f"最终优选: {candidates}\n")
    
    # 3. 逐个 LLM 分析 (无需再做量化分析，因为已经通过了)
    if candidates:
        symbol = candidates[0]
        print(f"--- 深度研判 {symbol} ---")
        try:
            # 获取报告仅为了展示分数 (或者直接传给 LLM)
            report = advisor.get_analysis_report(symbol)
            print(f"量化评分: {report['score']} ✅")
            print(f"信号: {', '.join(report['signals'])}")
            
            print(">> 触发 LLM 深度研判...")
            decision = advisor.ask_llm(symbol)
            if decision:
                print(f"🤖 LLM 决策: {decision.action} {decision.direction}")
                print(f"   信心: {decision.confidence}")
                print(f"   理由: {decision.reasoning}")
                if decision.action == "BUY" or decision.action == "SELL":
                    print(f"   建议参数:")
                    print(f"     - 杠杆: {decision.suggested_params.get('leverage')}x")
                    print(f"     - 本金: {decision.suggested_params.get('usdt_amount')} USDT")
                    print(f"     - 止损: {decision.suggested_params.get('stop_loss')}")
                    print(f"     - 止盈: {decision.suggested_params.get('take_profit')}")
                
        except Exception as e:
            print(f"分析失败: {e}")
        
        print("")
        time.sleep(1) # 避免 API 限制

if __name__ == "__main__":
    main()
