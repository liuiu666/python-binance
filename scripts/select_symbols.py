"""
脚本功能：筛选交易对
主要作用：
1. 根据价格范围、成交量等条件筛选合约交易对
2. 支持按成交量排序取前 N 个
3. 可选是否包含费率信息
4. 将筛选结果打印到控制台或保存为 CSV 文件
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_skills import Settings, create_client
from trading_skills.symbol_selector import FuturesSymbolSelector, SymbolFilter


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-price", type=float, default=10.0)
    parser.add_argument("--min-price", type=float, default=0.0001)
    parser.add_argument("--min-quote-volume", type=float, default=20_000_000.0)
    parser.add_argument("--top-n", type=int, default=30)
    parser.add_argument("--with-fee", action="store_true")
    parser.add_argument("--out", type=str, default="")
    args = parser.parse_args()

    settings = Settings.load(ROOT)
    client = create_client(settings)
    selector = FuturesSymbolSelector(client)
    df = selector.select_symbols(
        SymbolFilter(
            max_price=args.max_price,
            min_price=args.min_price,
            min_quote_volume_24h=args.min_quote_volume,
            top_n=args.top_n,
            include_fee=args.with_fee,
        )
    )

    if df.empty:
        print("没有选到符合条件的币")
        return 2

    pd.set_option("display.max_rows", 200)
    pd.set_option("display.max_columns", 50)
    pd.set_option("display.width", 200)
    print(df.to_string(index=False))

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out_path, index=False, encoding="utf-8-sig")
        print(f"已保存：{out_path.as_posix()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
