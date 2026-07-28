"""Compare realized volatility measured over different minute windows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = (1, 2, 3, 5, 10, 15, 30, 60)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(ROOT / "data" / "server_latest" / "btcusdt_1m.csv"))
    parser.add_argument("--output", default=str(ROOT / "tmp" / "minute_window_volatility_v1.json"))
    args = parser.parse_args()

    raw = pd.read_csv(args.input)
    raw["open_time"] = pd.to_datetime(raw.open_time, utc=True)
    raw = raw.drop_duplicates("open_time").sort_values("open_time").set_index("open_time")
    close = raw.close.astype(float)
    entry = raw.open.astype(float).shift(-1)
    settle = close.shift(-11)
    future_bps = (settle / entry - 1.0) * 10000.0
    signal = raw.index.minute % 10 == 0
    log_return = np.log(close / close.shift(1))
    base = pd.DataFrame({"time": raw.index, "futureBps": future_bps}, index=raw.index)
    base = base[signal]

    results: dict[str, dict] = {}
    for width in WINDOWS:
        if width == 1:
            rv = log_return.abs() * 10000.0
        else:
            rv = log_return.rolling(width, min_periods=width).std(ddof=0) * np.sqrt(width) * 10000.0
        past_return = (close / close.shift(width) - 1.0) * 10000.0
        frame = base.assign(rv=rv, pastReturn=past_return).dropna()
        quantiles = frame.rv.quantile([0.2, 0.4, 0.6, 0.8]).to_list()
        frame["bucket"] = pd.cut(
            frame.rv,
            bins=[-np.inf, *quantiles, np.inf],
            labels=["Q1", "Q2", "Q3", "Q4", "Q5"],
            include_lowest=True,
        )
        rows = {}
        for bucket, group in frame.groupby("bucket", observed=False):
            direction_won = np.sign(group.pastReturn) * group.futureBps > 0.0
            rows[str(bucket)] = {
                "trades": int(len(group)),
                "directionWinRate": round(float(direction_won.mean() * 100.0), 2),
                "upRate": round(float((group.futureBps > 0.0).mean() * 100.0), 2),
                "meanFutureAbsBps": round(float(group.futureBps.abs().mean()), 3),
                "meanFutureBps": round(float(group.futureBps.mean()), 3),
            }
        results[str(width)] = {
            "windowMinutes": width,
            "rows": int(len(frame)),
            "corrWithFutureAbsMove": round(float(frame.rv.corr(frame.futureBps.abs())), 3),
            "corrWithFutureSignedMove": round(float(frame.rv.corr(frame.futureBps)), 3),
            "quantileBreakdown": rows,
        }
    report = {
        "status": "minute_window_volatility_audit",
        "input": str(Path(args.input).resolve()),
        "entryModel": "signal minute close, next minute open entry, close after 10 further minutes settlement",
        "windowsMinutes": list(WINDOWS),
        "results": results,
        "conclusion": "Longer windows explain future absolute movement better, but no window provides a stable direction edge by itself.",
        "warning": "Descriptive audit only; quantile buckets are not deployment thresholds.",
    }
    output = Path(args.output)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
