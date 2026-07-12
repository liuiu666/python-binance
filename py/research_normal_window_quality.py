"""Compare normal-distribution windows without assuming 600s is best.

The report measures each rolling window as a market descriptor:
- how stable its sigma is,
- how much its center lags short-term price,
- how often price stays inside +/-1 sigma,
- what happens 10 minutes after upper/lower edge events.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from second_backtest.data import load_second_bars  # noqa: E402


WINDOWS = (120, 180, 300, 600, 900, 1200, 1800)


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        v = float(value)
        return v if math.isfinite(v) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def rolling_normal(data: pd.DataFrame, window: int) -> pd.DataFrame:
    close = data["close"].astype(float)
    volume = data["volume"].astype(float).clip(lower=0.0)
    minp = max(30, window // 3)
    sw = volume.rolling(window, min_periods=minp).sum()
    sx = (close * volume).rolling(window, min_periods=minp).sum()
    sx2 = (close * close * volume).rolling(window, min_periods=minp).sum()
    mean = close.rolling(window, min_periods=minp).mean()
    std = close.rolling(window, min_periods=minp).std(ddof=1)
    vwap = sx / sw.replace(0.0, np.nan)
    var = sx2 / sw.replace(0.0, np.nan) - vwap * vwap
    sigma = np.sqrt(var.clip(lower=0.0)).where(lambda s: s > 1e-9, std)
    center = vwap.fillna(mean)
    z = (close - center) / sigma.replace(0.0, np.nan)
    high = close.rolling(window, min_periods=minp).max()
    low = close.rolling(window, min_periods=minp).min()
    out = pd.DataFrame(index=data.index)
    out["center"] = center
    out["sigma"] = sigma
    out["z"] = z
    out["sigma_bps"] = sigma / close * 10000.0
    out["inside1"] = z.abs().le(1.0).astype(float).rolling(window, min_periods=minp).mean()
    out["center_slope_bps"] = (center / center.shift(max(30, min(window // 2, 300))) - 1.0) * 10000.0
    out["range_bps"] = (high / low - 1.0) * 10000.0
    out["pos"] = (close - low) / (high - low).replace(0.0, np.nan)
    out["center_lag_60_bps"] = (close / center.shift(60) - 1.0) * 10000.0
    out["sigma_change_60"] = out["sigma_bps"] / out["sigma_bps"].shift(60).replace(0.0, np.nan) - 1.0
    return out


def summarize_events(data: pd.DataFrame, f: pd.DataFrame, window: int, horizon_sec: int, gap_sec: int) -> dict[str, Any]:
    close = data["close"].to_numpy(float)
    rows = []
    last_idx = -10**12
    start = max(window + 300, 1800)
    limit = len(data) - horizon_sec
    for idx in range(start, limit):
        if idx - last_idx < gap_sec:
            continue
        row = f.iloc[idx]
        if not np.isfinite(float(row.get("z", np.nan))):
            continue
        z = float(row["z"])
        direction = None
        if z >= 1.0:
            direction = "upper"
            # fade upper means DOWN; positive outcome means price lower after 10m.
            signed = (float(close[idx]) - float(close[idx + horizon_sec])) / float(close[idx]) * 10000.0
        elif z <= -1.0:
            direction = "lower"
            # fade lower means UP; positive outcome means price higher after 10m.
            signed = (float(close[idx + horizon_sec]) - float(close[idx])) / float(close[idx]) * 10000.0
        else:
            continue
        rows.append({
            "time": data.index[idx],
            "edge": direction,
            "z": z,
            "sigma_bps": float(row["sigma_bps"]),
            "inside1": float(row["inside1"]),
            "center_slope_bps": float(row["center_slope_bps"]),
            "range_bps": float(row["range_bps"]),
            "signed_reversion_bps": signed,
            "reverted": bool(signed > 0.0),
        })
        last_idx = idx
    return {
        "events": rows,
        "summary": event_metrics(rows),
        "byEdge": {
            edge: event_metrics([row for row in rows if row["edge"] == edge])
            for edge in ("upper", "lower")
        },
    }


def event_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "events": 0,
            "reversionRate": 0.0,
            "avgReversionBps": None,
            "medianReversionBps": None,
            "avgSigmaBps": None,
            "avgInside1": None,
        }
    signed = np.array([float(row["signed_reversion_bps"]) for row in rows], dtype=float)
    return {
        "events": len(rows),
        "reversionRate": round(float((signed > 0.0).mean() * 100.0), 2),
        "avgReversionBps": round(float(np.mean(signed)), 4),
        "medianReversionBps": round(float(np.median(signed)), 4),
        "avgSigmaBps": round(float(np.mean([row["sigma_bps"] for row in rows])), 4),
        "avgInside1": round(float(np.mean([row["inside1"] for row in rows])), 4),
    }


def descriptor_metrics(f: pd.DataFrame, sample: pd.Series) -> dict[str, Any]:
    valid = f.loc[sample].dropna(subset=["z", "sigma_bps", "inside1", "center_slope_bps", "sigma_change_60"])
    if valid.empty:
        return {}
    return {
        "samples": int(len(valid)),
        "sigmaMedianBps": round(float(valid["sigma_bps"].median()), 4),
        "sigmaP25Bps": round(float(valid["sigma_bps"].quantile(0.25)), 4),
        "sigmaP75Bps": round(float(valid["sigma_bps"].quantile(0.75)), 4),
        "inside1Avg": round(float(valid["inside1"].mean()), 4),
        "centerSlopeAbsAvgBps": round(float(valid["center_slope_bps"].abs().mean()), 4),
        "centerLag60AbsAvgBps": round(float(valid["center_lag_60_bps"].abs().mean()), 4),
        "sigmaChange60AbsAvg": round(float(valid["sigma_change_60"].abs().mean()), 4),
        "zAbsP90": round(float(valid["z"].abs().quantile(0.90)), 4),
        "edgePctAbsZ1": round(float((valid["z"].abs() >= 1.0).mean() * 100.0), 2),
        "extremePctAbsZ15": round(float((valid["z"].abs() >= 1.5).mean() * 100.0), 2),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    data = load_second_bars(Path(args.seconds), include_shards=not args.no_shards)
    start = pd.Timestamp(args.start, tz="UTC") if args.start else data.index.max() - pd.Timedelta(hours=24)
    end = pd.Timestamp(args.end, tz="UTC") if args.end else data.index.max() - pd.Timedelta(seconds=args.horizon_sec)
    start = max(start, data.index.min() + pd.Timedelta(seconds=max(WINDOWS) + 600))
    end = min(end, data.index.max() - pd.Timedelta(seconds=args.horizon_sec + 5))
    sample = (data.index >= start) & (data.index < end)
    report = {
        "source": str(Path(args.seconds).resolve()),
        "start": start,
        "end": end,
        "hours": round((end - start).total_seconds() / 3600.0, 4),
        "horizonSec": args.horizon_sec,
        "gapSec": args.gap_sec,
        "windows": {},
    }
    all_events = []
    for window in WINDOWS:
        f = rolling_normal(data, window)
        events = summarize_events(data, f, window, args.horizon_sec, args.gap_sec)
        filtered_events = [row for row in events["events"] if start <= pd.Timestamp(row["time"]) < end]
        item = {
            "descriptor": descriptor_metrics(f, sample),
            "edgeEvents": event_metrics(filtered_events),
            "byEdge": {
                edge: event_metrics([row for row in filtered_events if row["edge"] == edge])
                for edge in ("upper", "lower")
            },
        }
        report["windows"][str(window)] = item
        for row in filtered_events:
            all_events.append({"window": window, **row})
    pd.DataFrame(clean(all_events)).to_csv(args.events_out, index=False, encoding="utf-8-sig")
    Path(args.out).write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", required=True)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--horizon-sec", type=int, default=600)
    parser.add_argument("--gap-sec", type=int, default=600)
    parser.add_argument("--no-shards", action="store_true")
    parser.add_argument("--out", default=str(ROOT / "tmp" / "normal_window_quality.json"))
    parser.add_argument("--events-out", default=str(ROOT / "tmp" / "normal_window_quality_events.csv"))
    args = parser.parse_args()
    report = run(args)
    compact = {
        window: {
            "descriptor": item["descriptor"],
            "edgeEvents": item["edgeEvents"],
            "byEdge": item["byEdge"],
        }
        for window, item in report["windows"].items()
    }
    print(json.dumps(clean({"start": report["start"], "end": report["end"], "windows": compact}), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
