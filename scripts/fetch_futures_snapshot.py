"""
脚本功能：获取合约快照数据
主要作用：
1. 下载指定币种的 K 线数据
2. 下载订单簿（Orderbook）深度数据
3. 下载近期成交（Trades）数据
4. 将数据保存到指定目录，用于分析或回测
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_skills import Settings, create_client
from trading_skills.data_fetcher import FuturesDataFetcher


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", type=str, default="BTCUSDT")
    parser.add_argument("--interval", type=str, default="1m")
    parser.add_argument("--kline-limit", type=int, default=500)
    parser.add_argument("--orderbook-limit", type=int, default=100)
    parser.add_argument("--trade-limit", type=int, default=1000)
    parser.add_argument("--out-dir", type=str, default="data")
    args = parser.parse_args()

    settings = Settings.load(ROOT)
    client = create_client(settings)
    fetcher = FuturesDataFetcher(client)
    snapshot = fetcher.fetch_snapshot(
        args.symbol,
        args.interval,
        kline_limit=args.kline_limit,
        orderbook_limit=args.orderbook_limit,
        agg_trade_limit=args.trade_limit,
    )
    paths = fetcher.save_snapshot(snapshot, base_dir=args.out_dir)
    for k, v in paths.items():
        print(f"{k}：{v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
