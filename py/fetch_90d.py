"""Fetch 90-day data for all symbols: 1m klines, funding, ls ratio, taker"""
import requests, time, json, os, pandas as pd
from datetime import datetime, timezone

OUT = "E:/codex/data"
SYMBOLS = ["btcusdt", "ethusdt", "solusdt"]
DAYS = 90

def fetch_klines(symbol, days=90):
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    all_data = []
    cursor = start_ms
    while cursor < end_ms:
        try:
            r = requests.get("https://api.binance.com/api/v3/klines",
                params={"symbol": symbol.upper(), "interval": "1m", "startTime": cursor, "limit": 1000}, timeout=15)
            batch = r.json()
            if not batch: break
            all_data.extend(batch)
            cursor = batch[-1][0] + 60000
            if len(all_data) % 10000 == 0:
                print(f"  {symbol}: {len(all_data)} candles fetched...")
            time.sleep(0.1)
        except Exception as e:
            print(f"  Error: {e}"); time.sleep(2)
    df = pd.DataFrame(all_data, columns=["open_time","open","high","low","close","volume","close_time","quote_vol","trades","taker_buy_vol","taker_buy_qv","ignore"])
    for c in ["open","high","low","close","volume"]: df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df[["open_time","open","high","low","close","volume"]]
    df = df.drop_duplicates(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)
    return df

def fetch_funding(symbol, days=90):
    end_ms = int(time.time() * 1000); start_ms = end_ms - days * 86400 * 1000
    all_data = []; cursor = start_ms
    while cursor < end_ms:
        try:
            r = requests.get("https://fapi.binance.com/fapi/v1/fundingRate",
                params={"symbol": symbol.upper(), "startTime": cursor, "limit": 1000}, timeout=15)
            batch = r.json()
            if not batch: break
            all_data.extend(batch)
            cursor = batch[-1]["fundingTime"] + 1
            time.sleep(0.1)
        except Exception as e: print(f"  Funding error: {e}"); time.sleep(2)
    if not all_data: return pd.DataFrame()
    df = pd.DataFrame(all_data)[["fundingTime","fundingRate"]]
    df["fundingRate"] = df["fundingRate"].astype(float)
    df["fundingTime"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
    return df.drop_duplicates().reset_index(drop=True)

def fetch_lsratio(symbol, days=90):
    end_ts = int(time.time() * 1000); start_ts = end_ts - days * 86400 * 1000
    all_data = []; cursor = start_ts
    while cursor < end_ts:
        try:
            r = requests.get("https://fapi.binance.com/futures/data/topLongShortPositionRatio",
                params={"symbol": symbol.upper(), "period": "5m", "startTime": cursor, "limit": 500}, timeout=15)
            batch = r.json()
            if not batch: break
            all_data.extend(batch)
            cursor = int(batch[-1]["timestamp"]) + 1
            time.sleep(0.1)
        except Exception as e: print(f"  LS error: {e}"); time.sleep(2)
    if not all_data: return pd.DataFrame()
    df = pd.DataFrame(all_data)[["timestamp","longShortRatio","longAccount","shortAccount"]]
    for c in ["longShortRatio","longAccount","shortAccount"]: df[c] = df[c].astype(float)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.drop_duplicates().reset_index(drop=True)

def fetch_taker(symbol, days=90):
    end_ts = int(time.time() * 1000); start_ts = end_ts - days * 86400 * 1000
    all_data = []; cursor = start_ts
    while cursor < end_ts:
        try:
            r = requests.get("https://fapi.binance.com/futures/data/takerlongshortRatio",
                params={"symbol": symbol.upper(), "period": "5m", "startTime": cursor, "limit": 500}, timeout=15)
            batch = r.json()
            if not batch: break
            all_data.extend(batch)
            cursor = int(batch[-1]["timestamp"]) + 1
            time.sleep(0.1)
        except Exception as e: print(f"  Taker error: {e}"); time.sleep(2)
    if not all_data: return pd.DataFrame()
    df = pd.DataFrame(all_data)[["timestamp","buySellRatio","buyVol","sellVol"]]
    for c in ["buySellRatio","buyVol","sellVol"]: df[c] = df[c].astype(float)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.drop_duplicates().reset_index(drop=True)

if __name__ == "__main__":
    for sym in SYMBOLS:
        print(f"\n{'='*50}")
        print(f"Fetching {sym.upper()} 90d data...")
        kline_path = os.path.join(OUT, f"{sym}_1m.csv")
        existing = pd.read_csv(kline_path) if os.path.exists(kline_path) else None
        if existing is not None and len(existing) > 100000:
            print(f"  {sym} klines: already {len(existing)} rows, skipping")
        else:
            print(f"  Fetching 1m klines...")
            df = fetch_klines(sym, DAYS)
            df.to_csv(kline_path, index=False)
            print(f"  Saved {len(df)} candles")
        print(f"  Fetching funding rate...")
        fund = fetch_funding(sym, DAYS)
        fund.to_csv(os.path.join(OUT, f"{sym}_funding.csv"), index=False)
        print(f"  Funding: {len(fund)} records")
        print(f"  Fetching long/short ratio...")
        ls = fetch_lsratio(sym, DAYS)
        ls.to_csv(os.path.join(OUT, f"{sym}_lsratio.csv"), index=False)
        print(f"  LS Ratio: {len(ls)} records")
        print(f"  Fetching taker buy/sell...")
        tk = fetch_taker(sym, DAYS)
        tk.to_csv(os.path.join(OUT, f"{sym}_taker.csv"), index=False)
        print(f"  Taker: {len(tk)} records")
    print(f"\nDone! All data fetched.")
