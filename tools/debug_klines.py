
import sys
from pathlib import Path
import pandas as pd
from binance.client import Client

# Add src to sys.path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_skills import Settings, create_client
from trading_skills.data_fetcher import FuturesDataFetcher

def main():
    print("Testing fetch_klines...")
    try:
        settings = Settings.load(ROOT)
        client = create_client(settings)
        fetcher = FuturesDataFetcher(client)
        
        symbol = "AXSUSDT"
        interval = "1m"
        
        print(f"Fetching {symbol} {interval}...")
        df = fetcher.fetch_klines(symbol, interval, limit=10)
        
        print(f"Result type: {type(df)}")
        print(f"Result shape: {df.shape}")
        print("Head:")
        print(df.head())
        
        # Try saving
        out_file = Path("test_kline.csv")
        df.to_csv(out_file)
        print(f"Saved to {out_file.absolute()}")
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
