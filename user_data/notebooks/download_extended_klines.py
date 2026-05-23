"""Download 1y of BTCUSDT-PERP 1m klines INCLUDING taker_buy_base_asset_volume.

Saves to user_data/data/binance/futures/BTC_USDT_USDT-1m-futures-extended.feather
with columns:
  date, open, high, low, close, volume, quote_volume, num_trades,
  taker_buy_base, taker_buy_quote
"""
import time
from datetime import datetime, timezone
import requests
import pandas as pd

PROXIES = {
    "http": "http://127.0.0.1:7897",
    "https": "http://127.0.0.1:7897",
}
URL = "https://fapi.binance.com/fapi/v1/klines"
SYMBOL = "BTCUSDT"
INTERVAL = "1m"
LIMIT = 1500
TARGET_DAYS = 370
OUT = "user_data/data/binance/futures/BTC_USDT_USDT-1m-futures-extended.feather"

COLUMNS = [
    'open_time', 'open', 'high', 'low', 'close', 'volume',
    'close_time', 'quote_volume', 'num_trades',
    'taker_buy_base', 'taker_buy_quote', 'ignore',
]


def fetch(end_ms):
    params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": LIMIT}
    if end_ms is not None:
        params["endTime"] = end_ms
    r = requests.get(URL, params=params, proxies=PROXIES, timeout=20)
    r.raise_for_status()
    return r.json()


def main():
    target_bars = TARGET_DAYS * 24 * 60
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    end_ms = now_ms
    chunks = []
    fetched = 0
    page = 0
    while fetched < target_bars:
        try:
            raw = fetch(end_ms)
        except Exception as e:
            print(f"[error] page {page}: {e}; retrying in 5s")
            time.sleep(5); continue
        if not raw:
            break
        df = pd.DataFrame(raw, columns=COLUMNS)
        chunks.append(df)
        fetched += len(df)
        oldest_ms = int(df.iloc[0]['open_time'])
        end_ms = oldest_ms - 1
        page += 1
        if page % 20 == 0:
            print(f"  page {page}: fetched {fetched}/{target_bars} bars  oldest={pd.to_datetime(oldest_ms, unit='ms', utc=True)}")
        if len(df) < LIMIT:
            print("  reached earliest available data")
            break
        time.sleep(0.15)  # courtesy

    full = pd.concat(chunks, ignore_index=True).drop_duplicates(subset='open_time')
    for c in ['open', 'high', 'low', 'close', 'volume',
              'quote_volume', 'taker_buy_base', 'taker_buy_quote']:
        full[c] = pd.to_numeric(full[c]).astype('float64')
    full['num_trades'] = pd.to_numeric(full['num_trades']).astype('int64')
    full['date'] = pd.to_datetime(full['open_time'], unit='ms', utc=True)
    full = full.sort_values('date').reset_index(drop=True)
    keep = ['date', 'open', 'high', 'low', 'close', 'volume',
            'quote_volume', 'num_trades', 'taker_buy_base', 'taker_buy_quote']
    full[keep].to_feather(OUT)
    print(f"\nWrote {len(full):,} rows to {OUT}")
    print(f"Range: {full['date'].iloc[0]} -> {full['date'].iloc[-1]}")
    print(f"Sample row:\n{full.iloc[0].to_dict()}")


if __name__ == '__main__':
    main()
