import sys
import json
from pathlib import Path

# Setup path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_skills import Settings, create_client, LLMAdvisor

def main():
    print("=== Testing LLMAdvisor ===")
    
    settings = Settings.load(ROOT)
    if not settings.llm_api_key:
        print("Warning: LLM API Key not found in config.json or .env")
    
    client = create_client(settings)
    advisor = LLMAdvisor(client)
    
    symbol = "BTCUSDT"
    print(f"Analyzing {symbol}...")
    
    # 1. Test data fetching
    try:
        data = advisor.fetch_market_data(symbol)
        print(f"Data keys: {list(data.keys())}")
        if data.get('premium_index'):
            print(f"Price: {data['premium_index'].get('markPrice')}")
        else:
            print("Warning: premium_index is empty")
    except Exception as e:
        print(f"Data fetching failed: {e}")
        import traceback
        traceback.print_exc()
        return

    # 2. Test Analysis
    try:
        report = advisor.get_analysis_report(symbol)
        print(f"Analysis Score: {report['score']}")
        print(f"Signals: {report['signals']}")
    except Exception as e:
        print(f"Analysis failed: {e}")
        import traceback
        traceback.print_exc()
        return

    # 3. Test LLM
    print("\nAsking LLM (Real API call)...")
    try:
        decision = advisor.ask_llm(symbol)
        if decision:
            print(f"\nDecision: {decision.action} {decision.direction}")
            print(f"Confidence: {decision.confidence}")
            print(f"Reasoning: {decision.reasoning}")
            print(f"Suggested Params: {decision.suggested_params}")
        else:
            print("No decision returned (LLM failure or config missing).")
    except Exception as e:
        print(f"LLM call failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
