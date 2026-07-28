"""Build long-horizon ten-minute samples from minute and positioning data."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_CSV = ROOT / "tmp" / "minute_context_events_10m.csv"
OUT_JSON = ROOT / "tmp" / "minute_context_events_report.json"


def read_klines() -> pd.DataFrame:
    frames = []
    for name in ("btcusdt_1m_180d.csv", "btcusdt_1m.csv"):
        data = pd.read_csv(ROOT / "data" / name, parse_dates=["open_time"])
        data = data.rename(columns={"open_time": "time"})
        frames.append(data)
    return pd.concat(frames, ignore_index=True).drop_duplicates("time", keep="last").sort_values("time")


def read_context() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    taker = pd.read_csv(ROOT / "data" / "btcusdt_taker.csv").rename(columns={"timestamp": "time"})
    ratio = pd.read_csv(ROOT / "data" / "btcusdt_lsratio.csv").rename(columns={"timestamp": "time"})
    funding = pd.read_csv(ROOT / "data" / "btcusdt_funding.csv").rename(columns={"fundingTime": "time"})
    for frame in (taker, ratio, funding):
        frame["time"] = pd.to_datetime(frame["time"], utc=True, format="mixed", errors="coerce")
        frame.dropna(subset=["time"], inplace=True)
    # Binance five-minute positioning endpoints timestamp the beginning of the
    # bucket.  The bucket is not causally available until five minutes later.
    taker["time"] += pd.Timedelta(minutes=5)
    ratio["time"] += pd.Timedelta(minutes=5)
    return taker.sort_values("time"), ratio.sort_values("time"), funding.sort_values("time")


def main() -> None:
    data = read_klines().set_index("time")
    close = data["close"].astype(float)
    features = pd.DataFrame(index=data.index)
    for width in (1, 3, 5, 10, 30, 60):
        features[f"ret_{width}m"] = close.pct_change(width, fill_method=None) * 10000.0
    one_minute = close.pct_change(fill_method=None) * 10000.0
    for width in (10, 30, 60):
        features[f"vol_{width}m"] = one_minute.rolling(width, min_periods=width).std(ddof=0) * np.sqrt(width)
        center = close.rolling(width, min_periods=width).mean()
        sigma = close.rolling(width, min_periods=width).std(ddof=0)
        features[f"z_{width}m"] = (close - center) / sigma.replace(0.0, np.nan)
    features["range_10m_bps"] = (
        data.high.astype(float).rolling(10, min_periods=10).max()
        / data.low.astype(float).rolling(10, min_periods=10).min()
        - 1.0
    ) * 10000.0
    volume10 = data.volume.astype(float).rolling(10, min_periods=10).sum()
    volume60 = data.volume.astype(float).rolling(60, min_periods=60).sum() / 6.0
    features["volume_ratio_10m"] = volume10 / volume60.replace(0.0, np.nan)
    features = features.reset_index().sort_values("time")

    taker, ratio, funding = read_context()
    features = pd.merge_asof(features, taker[["time", "buySellRatio", "buyVol", "sellVol"]], on="time", direction="backward", tolerance=pd.Timedelta("10min"))
    features = pd.merge_asof(features, ratio[["time", "longAccount", "shortAccount", "longShortRatio"]], on="time", direction="backward", tolerance=pd.Timedelta("10min"))
    features = pd.merge_asof(features, funding[["time", "fundingRate"]], on="time", direction="backward", tolerance=pd.Timedelta("9h"))
    features = features.set_index("time")
    for name in ("buySellRatio", "longShortRatio"):
        features[f"{name}_change_30m"] = features[name].pct_change(30, fill_method=None)
        features[f"{name}_change_60m"] = features[name].pct_change(60, fill_method=None)

    features["entry"] = close.reindex(features.index)
    features["settle"] = close.shift(-10).reindex(features.index)
    features["raw_move_bps"] = (features.settle / features.entry - 1.0) * 10000.0
    features["up"] = (features.raw_move_bps > 0.0).astype(int)
    mask = (features.index.minute % 10 == 0) & features.settle.notna()
    events = features.loc[mask].reset_index()
    events = events[events.buySellRatio.notna() & events.longShortRatio.notna()].copy()
    events.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    report = {
        "rows": len(events),
        "start": events.time.min().isoformat() if not events.empty else None,
        "end": events.time.max().isoformat() if not events.empty else None,
        "days": round((events.time.max() - events.time.min()).total_seconds() / 86400.0, 2) if not events.empty else 0.0,
        "warning": "Minute close labels test slow-context predictability only and are not executable-price backtests.",
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
