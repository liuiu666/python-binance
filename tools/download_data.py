import sys
import time
from pathlib import Path

# Add src to sys.path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_skills import Settings, create_client
from trading_skills.data_fetcher import FuturesDataFetcher
from trading_skills.symbol_selector import FuturesSymbolSelector, SymbolFilter

def main():
    # 1. Initialize
    print("正在初始化客户端...")
    settings = Settings.load(ROOT)
    client = create_client(settings)
    fetcher = FuturesDataFetcher(client)
    selector = FuturesSymbolSelector(client)

    # 2. Dynamic Selection using Rule
    print("正在根据规则筛选币种...")
    # Define rule: Price 0.0001~10.0, Min Volume 20M, Top 10 by Volume
    rule = SymbolFilter(
        max_price=10.0,
        min_price=0.0001,
        min_quote_volume_24h=20_000_000.0,
        top_n=10, # Top 10 as requested
        include_fee=False
    )
    
    df_selected = selector.select_symbols(rule)
    
    if df_selected.empty:
        print("未筛选到符合条件的币种。")
        return

    symbols = df_selected["symbol"].tolist()
    print(f"筛选结果 (Top {len(symbols)}): {symbols}")
    
    # Show details
    print(df_selected[["symbol", "lastPrice", "quoteVolume", "priceChangePercent"]].to_string(index=False))

    base_dir = ROOT / "data"
    print(f"\n目标目录: {base_dir}")
    
    # 3. Download loop
    intervals = ["1m", "5m", "15m", "1h", "4h"]
    
    for symbol in symbols:
        print(f"\n正在下载 {symbol} 数据...")
        
        # Download multiple timeframes
        for interval in intervals:
            try:
                print(f"  -> [{interval}] 获取中...", end="", flush=True)
                snapshot = fetcher.fetch_snapshot(
                    symbol=symbol,
                    interval=interval,
                    kline_limit=1000,
                    orderbook_limit=100, # Orderbook is snapshot, shared logic but called per interval to be safe or just once? 
                                         # Ideally orderbook is realtime, klines are historical. 
                                         # The fetch_snapshot method bundles them. We will call it for each interval.
                    agg_trade_limit=1000,
                    funding_limit=100,
                    ratio_limit=500
                )
                
                # Save
                paths = fetcher.save_snapshot(snapshot, base_dir=str(base_dir))
                print(f" 保存成功")
                
            except Exception as e:
                print(f" 失败: {e}")
            
            # Rate limit protection between intervals
            time.sleep(0.5)

        # Rate limit protection between symbols
        time.sleep(1)

    print("\n所有任务完成。")

if __name__ == "__main__":
    main()
