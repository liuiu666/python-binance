"""快速看一眼录到的 OI / Ratios 数据规律。"""
from __future__ import annotations

import glob
from pathlib import Path

import pandas as pd

pd.set_option("display.max_rows", 200)
pd.set_option("display.width", 200)

FLOW_DIR = Path("user_data/data/flow")


def main() -> None:
    files = sorted(FLOW_DIR.glob("*.parquet"))
    print(f"找到 {len(files)} 个 parquet 文件\n")

    # OI
    oi_files = [f for f in files if f.name.startswith("oi.")]
    if oi_files:
        oi = pd.concat([pd.read_parquet(f) for f in oi_files], ignore_index=True)
        oi["dt"] = pd.to_datetime(oi["ts"], unit="ms", utc=True)
        oi = oi.sort_values("dt").drop_duplicates("ts").reset_index(drop=True)
        print(f"=== OI 数据 ({len(oi)} 行) ===")
        print(f"时间范围: {oi['dt'].iloc[0]} -> {oi['dt'].iloc[-1]}")
        print(f"列: {list(oi.columns)}")
        print(oi[["dt", "sumOpenInterest", "sumOpenInterestValue"]].tail(10).to_string(index=False))
        print(f"\nOI 名义量变化:  {oi['sumOpenInterest'].iloc[0]:.0f} -> {oi['sumOpenInterest'].iloc[-1]:.0f}  "
              f"({(oi['sumOpenInterest'].iloc[-1] / oi['sumOpenInterest'].iloc[0] - 1) * 100:+.2f}%)")
        print(f"OI 名义价值变化: {oi['sumOpenInterestValue'].iloc[0] / 1e9:.3f}B -> "
              f"{oi['sumOpenInterestValue'].iloc[-1] / 1e9:.3f}B")

    # Ratios（按 endpoint 拆开）
    rat_files = [f for f in files if f.name.startswith("ratios.")]
    if rat_files:
        rat = pd.concat([pd.read_parquet(f) for f in rat_files], ignore_index=True)
        rat["dt"] = pd.to_datetime(rat["ts"], unit="ms", utc=True)
        rat = rat.sort_values("dt").reset_index(drop=True)
        print(f"\n=== Ratios 数据 ({len(rat)} 行) ===")
        print(f"endpoint: {rat['endpoint'].unique().tolist()}")

        for ep, sub in rat.groupby("endpoint"):
            sub = sub.dropna(axis=1, how="all").drop_duplicates("ts").sort_values("dt").reset_index(drop=True)
            print(f"\n--- {ep} ({len(sub)} 行) ---")
            cols = [c for c in sub.columns if c not in ("ts", "endpoint", "dt")]
            print(f"时间范围: {sub['dt'].iloc[0]} -> {sub['dt'].iloc[-1]}")
            print(sub[["dt"] + cols].tail(8).to_string(index=False))

            # 简单统计
            for c in cols:
                if pd.api.types.is_numeric_dtype(sub[c]):
                    s = sub[c].dropna()
                    if len(s) > 1:
                        print(f"  {c}: min={s.min():.4f}  max={s.max():.4f}  "
                              f"mean={s.mean():.4f}  std={s.std():.4f}  "
                              f"latest={s.iloc[-1]:.4f}")


if __name__ == "__main__":
    main()
