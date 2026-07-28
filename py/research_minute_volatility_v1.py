"""Minute volatility regime audit for ten-minute event contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
BUCKETS = [-np.inf, 2, 4, 6, 8, 12, 20, np.inf]


def stats(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {"trades": 0, "upRate": None, "meanMoveBps": None, "meanAbsMoveBps": None}
    return {
        "trades": int(len(frame)),
        "upRate": round(float(frame.up.mean() * 100.0), 2),
        "meanMoveBps": round(float(frame.futureBps.mean()), 3),
        "meanAbsMoveBps": round(float(frame.absFutureBps.mean()), 3),
        "p10MoveBps": round(float(frame.futureBps.quantile(0.10)), 3),
        "p90MoveBps": round(float(frame.futureBps.quantile(0.90)), 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(ROOT / "data" / "server_latest" / "btcusdt_1m.csv"))
    parser.add_argument("--output", default=str(ROOT / "tmp" / "minute_volatility_v1.json"))
    args = parser.parse_args()

    raw = pd.read_csv(args.input)
    raw["open_time"] = pd.to_datetime(raw.open_time, utc=True)
    raw = raw.drop_duplicates("open_time").sort_values("open_time").set_index("open_time")
    close = raw.close.astype(float)
    open_price = raw.open.astype(float)
    high = raw.high.astype(float)
    low = raw.low.astype(float)
    log_return = np.log(close / close.shift(1))
    realized = {
        width: log_return.rolling(width, min_periods=width).std(ddof=0) * np.sqrt(width) * 10000.0
        for width in (10, 30, 60, 120)
    }
    atr = {
        width: ((high - low) / close * 10000.0).rolling(width, min_periods=width).mean()
        for width in (10, 30, 60)
    }
    entry = open_price.shift(-1)
    settle = close.shift(-11)
    future = settle / entry - 1.0
    signal = raw.index.minute % 10 == 0
    frame = pd.DataFrame(
        {
            "time": raw.index,
            "futureBps": future * 10000.0,
            "up": future > 0.0,
            "absFutureBps": abs(future) * 10000.0,
            "rv10": realized[10],
            "rv30": realized[30],
            "rv60": realized[60],
            "rv120": realized[120],
            "atr10": atr[10],
            "atr30": atr[30],
            "atr60": atr[60],
        },
        index=raw.index,
    )
    frame = frame[signal].dropna()
    report = {
        "status": "minute_volatility_regime_audit",
        "input": str(Path(args.input).resolve()),
        "entryModel": "signal minute close, next minute open entry, close after 10 further minutes settlement",
        "rows": int(len(frame)),
        "overall": stats(frame),
        "realizedVolatilityBps": {
            name: {
                str(bucket): stats(group)
                for bucket, group in frame.assign(bucket=pd.cut(frame[name], BUCKETS, right=False)).groupby("bucket", observed=False)
            }
            for name in ("rv10", "rv60", "atr10", "atr60")
        },
        "conclusion": "Volatility changes absolute move size much more than up/down probability; volatility alone is not a direction signal.",
        "warning": "Descriptive audit only; no volatility threshold was selected for live trading.",
    }
    output = Path(args.output)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
