"""Fetch 180-day (6-month) 1m kline data for BTCUSDT to run extended backtests.
"""

import os
import time
import requests
import pandas as pd

OUT_DIR = "E:/codex/data"
SYMBOL = "btcusdt"
DAYS = 180

def fetch_klines(symbol, days=180):
    print(f"Fetching {symbol.upper()} 1m klines for the past {days} days...")
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    all_data = []
    cursor = start_ms
    
    while cursor < end_ms:
        try:
            r = requests.get("https://api.binance.com/api/v3/klines",
                params={"symbol": symbol.upper(), "interval": "1m", "startTime": cursor, "limit": 1000}, timeout=15)
            if r.status_code != 200:
                print(f"  Error {r.status_code}: {r.text}")
                time.sleep(2)
                continue
                
            batch = r.json()
            if not batch: 
                break
                
            all_data.extend(batch)
            cursor = batch[-1][0] + 60000
            
            if len(all_data) % 10000 == 0 or len(all_data) < 10000:
                print(f"  {symbol}: {len(all_data)} candles fetched...")
                
            time.sleep(0.1) # Respect Binance rate limits
        except Exception as e:
            print(f"  Error: {e}")
            time.sleep(2)
            
    df = pd.DataFrame(all_data, columns=["open_time","open","high","low","close","volume","close_time","quote_vol","trades","taker_buy_vol","taker_buy_qv","ignore"])
    for c in ["open","high","low","close","volume"]: 
        df[c] = df[c].astype(float)
        
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df[["open_time","open","high","low","close","volume"]]
    df = df.drop_duplicates(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)
    return df

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    kline_path = os.path.join(OUT_DIR, f"{SYMBOL}_1m_180d.csv")
    
    t0 = time.time()
    df = fetch_klines(SYMBOL, DAYS)
    df.to_csv(kline_path, index=False)
    
    print(f"\nCompleted! Saved {len(df)} candles to {kline_path} in {time.time() - t0:.1f} seconds.")

if __name__ == "__main__":
    main()
