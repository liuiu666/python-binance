"""Test whether price belongs to the rolling 600-second normal distribution.

The script labels every second as:
- core_inside: stable 600s distribution and price is inside +/-1 sigma
- edge_belong: stable 600s distribution, price near edge, no short-term escape
- escape_up / escape_down: short-term speed says price is leaving the 600s distribution
- unstable_distribution: the 600s distribution itself is not stable enough

Then it measures 10-minute outcomes for each label.
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


def add_normal(data: pd.DataFrame, out: pd.DataFrame, window: int, prefix: str) -> None:
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
    out[f"center_{prefix}"] = center
    out[f"sigma_{prefix}"] = sigma
    out[f"z_{prefix}"] = z
    out[f"sigma_{prefix}_bps"] = sigma / close * 10000.0
    out[f"inside1_{prefix}"] = z.abs().le(1.0).astype(float).rolling(window, min_periods=minp).mean()
    out[f"slope_{prefix}_bps"] = (center / center.shift(max(30, min(window // 2, 300))) - 1.0) * 10000.0


def build_features(data: pd.DataFrame) -> pd.DataFrame:
    close = data["close"].astype(float)
    out = pd.DataFrame(index=data.index)
    out["close"] = close
    out["observed_pct_600"] = data["observed"].astype(float).rolling(600, min_periods=120).mean() * 100.0
    add_normal(data, out, 180, "180")
    add_normal(data, out, 600, "600")
    for sec in (30, 60, 120, 180, 300, 600):
        out[f"ret_{sec}s_bps"] = np.log(close / close.shift(sec)) * 10000.0
    out["sigma_speed_ratio"] = out["sigma_180_bps"] / out["sigma_600_bps"].replace(0.0, np.nan)
    out["z600_outside_streak"] = out["z_600"].abs().gt(1.0).astype(int).groupby(
        out["z_600"].abs().le(1.0).astype(int).cumsum()
    ).cumsum()
    return out


def is_finite(row: pd.Series, keys: list[str]) -> bool:
    return all(np.isfinite(float(row.get(key, np.nan))) for key in keys)


def label_row(row: pd.Series) -> tuple[str, str]:
    keys = [
        "z_600", "z_180", "sigma_600_bps", "sigma_180_bps", "inside1_600",
        "slope_600_bps", "slope_180_bps", "observed_pct_600",
        "ret_60s_bps", "ret_180s_bps", "sigma_speed_ratio",
    ]
    if not is_finite(row, keys):
        return "warmup", "特征不足"
    z600 = float(row["z_600"])
    sigma600 = float(row["sigma_600_bps"])
    inside600 = float(row["inside1_600"])
    slope600 = float(row["slope_600_bps"])
    slope180 = float(row["slope_180_bps"])
    ret60 = float(row["ret_60s_bps"])
    ret180 = float(row["ret_180s_bps"])
    speed_ratio = float(row["sigma_speed_ratio"])
    outside_streak = float(row.get("z600_outside_streak", 0.0))

    stable = (
        float(row["observed_pct_600"]) >= 90.0
        and 2.0 <= sigma600 <= 12.0
        and inside600 >= 0.50
        and abs(slope600) <= 10.0
    )
    if not stable:
        return "unstable_distribution", "600秒分布不稳定或波动不在可解释范围"

    escape_up = (
        z600 > 1.0
        and (
            speed_ratio >= 1.25
            or ret180 >= 12.0
            or ret60 >= 5.0
            or slope180 - slope600 >= 4.0
            or outside_streak >= 90
        )
    )
    escape_down = (
        z600 < -1.0
        and (
            speed_ratio >= 1.25
            or ret180 <= -12.0
            or ret60 <= -5.0
            or slope180 - slope600 <= -4.0
            or outside_streak >= 90
        )
    )
    if escape_up:
        return "escape_up", "短周期速度确认向上脱离600秒正态"
    if escape_down:
        return "escape_down", "短周期速度确认向下脱离600秒正态"

    if abs(z600) <= 1.0:
        return "core_inside", "价格处于600秒正态核心区"
    if abs(z600) <= 1.5:
        return "edge_belong", "价格处于600秒正态边缘但尚未被短周期证明脱离"
    return "outside_no_escape", "价格已在1.5σ外，但短周期速度未确认新区域"


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"samples": 0}
    fut = np.array([float(row["future_600s_bps"]) for row in rows], dtype=float)
    abs_fut = np.abs(fut)
    return {
        "samples": len(rows),
        "upRate": round(float((fut > 0.0).mean() * 100.0), 2),
        "avgFutureBps": round(float(np.mean(fut)), 4),
        "medianFutureBps": round(float(np.median(fut)), 4),
        "avgAbsFutureBps": round(float(np.mean(abs_fut)), 4),
        "medianAbsFutureBps": round(float(np.median(abs_fut)), 4),
        "bigMove10bpRate": round(float((abs_fut >= 10.0).mean() * 100.0), 2),
    }


def summarize_directional(rows: list[dict[str, Any]], direction: str) -> dict[str, Any]:
    if not rows:
        return {"samples": 0}
    sign = 1.0 if direction == "UP" else -1.0
    signed = np.array([float(row["future_600s_bps"]) * sign for row in rows], dtype=float)
    return {
        "samples": len(rows),
        "directionWinRate": round(float((signed > 0.0).mean() * 100.0), 2),
        "avgSignedBps": round(float(np.mean(signed)), 4),
        "medianSignedBps": round(float(np.median(signed)), 4),
    }


def build_event_rows(sample_rows: list[dict[str, Any]], gap_sec: int) -> list[dict[str, Any]]:
    out = []
    last_idx_by_label: dict[str, int] = {}
    prev_label = None
    for idx, row in enumerate(sample_rows):
        label = str(row["label"])
        if label in {"warmup", "core_inside", "unstable_distribution"}:
            prev_label = label
            continue
        last_idx = last_idx_by_label.get(label, -10**12)
        changed_into_label = label != prev_label
        if changed_into_label and idx - last_idx >= max(1, gap_sec // 5):
            out.append(row)
            last_idx_by_label[label] = idx
        prev_label = label
    return out


def run(args: argparse.Namespace) -> dict[str, Any]:
    data = load_second_bars(Path(args.seconds), include_shards=not args.no_shards)
    features = build_features(data)
    start = pd.Timestamp(args.start, tz="UTC") if args.start else data.index.max() - pd.Timedelta(hours=24)
    end = pd.Timestamp(args.end, tz="UTC") if args.end else data.index.max() - pd.Timedelta(seconds=args.horizon_sec)
    start = max(start, data.index.min() + pd.Timedelta(seconds=1800))
    end = min(end, data.index.max() - pd.Timedelta(seconds=args.horizon_sec + 5))
    first = int(data.index.searchsorted(start))
    last = min(int(data.index.searchsorted(end)), len(data) - args.horizon_sec)
    close = data["close"].to_numpy(float)
    rows = []
    for idx in range(first, last, max(1, args.sample_step_sec)):
        label, label_reason = label_row(features.iloc[idx])
        entry = float(close[idx])
        future = float(close[idx + args.horizon_sec])
        future_bps = (future / entry - 1.0) * 10000.0
        row = features.iloc[idx]
        rows.append({
            "time": data.index[idx],
            "label": label,
            "label_reason": label_reason,
            "entry": entry,
            "future": future,
            "future_600s_bps": future_bps,
            "z600": float(row.get("z_600", np.nan)),
            "z180": float(row.get("z_180", np.nan)),
            "sigma600": float(row.get("sigma_600_bps", np.nan)),
            "sigma180": float(row.get("sigma_180_bps", np.nan)),
            "inside600": float(row.get("inside1_600", np.nan)),
            "slope600": float(row.get("slope_600_bps", np.nan)),
            "slope180": float(row.get("slope_180_bps", np.nan)),
            "ret60": float(row.get("ret_60s_bps", np.nan)),
            "ret180": float(row.get("ret_180s_bps", np.nan)),
            "speedRatio": float(row.get("sigma_speed_ratio", np.nan)),
        })
    by_label = {}
    for label in sorted({row["label"] for row in rows}):
        part = [row for row in rows if row["label"] == label]
        item = summarize(part)
        if label == "escape_up":
            item["followEscape"] = summarize_directional(part, "UP")
            item["fadeEscape"] = summarize_directional(part, "DOWN")
        elif label == "escape_down":
            item["followEscape"] = summarize_directional(part, "DOWN")
            item["fadeEscape"] = summarize_directional(part, "UP")
        elif label in {"edge_belong", "outside_no_escape"}:
            upper = [row for row in part if row["z600"] > 0]
            lower = [row for row in part if row["z600"] < 0]
            item["fadeUpperDown"] = summarize_directional(upper, "DOWN")
            item["fadeLowerUp"] = summarize_directional(lower, "UP")
        by_label[label] = item

    event_rows = build_event_rows(rows, args.event_gap_sec)
    by_event_label = {}
    for label in sorted({row["label"] for row in event_rows}):
        part = [row for row in event_rows if row["label"] == label]
        item = summarize(part)
        if label == "escape_up":
            item["followEscape"] = summarize_directional(part, "UP")
            item["fadeEscape"] = summarize_directional(part, "DOWN")
        elif label == "escape_down":
            item["followEscape"] = summarize_directional(part, "DOWN")
            item["fadeEscape"] = summarize_directional(part, "UP")
        elif label in {"edge_belong", "outside_no_escape"}:
            upper = [row for row in part if row["z600"] > 0]
            lower = [row for row in part if row["z600"] < 0]
            item["fadeUpperDown"] = summarize_directional(upper, "DOWN")
            item["fadeLowerUp"] = summarize_directional(lower, "UP")
        by_event_label[label] = item

    frame = pd.DataFrame(clean(rows))
    frame.to_csv(args.out_csv, index=False, encoding="utf-8-sig")
    pd.DataFrame(clean(event_rows)).to_csv(args.events_csv, index=False, encoding="utf-8-sig")
    report = {
        "source": str(Path(args.seconds).resolve()),
        "start": start,
        "end": end,
        "hours": round((end - start).total_seconds() / 3600.0, 4),
        "horizonSec": args.horizon_sec,
        "sampleStepSec": args.sample_step_sec,
        "eventGapSec": args.event_gap_sec,
        "labels": by_label,
        "events": by_event_label,
        "sampleRows": clean(rows[:8] + rows[-8:] if len(rows) > 16 else rows),
    }
    Path(args.out).write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", required=True)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--horizon-sec", type=int, default=600)
    parser.add_argument("--sample-step-sec", type=int, default=5)
    parser.add_argument("--event-gap-sec", type=int, default=600)
    parser.add_argument("--no-shards", action="store_true")
    parser.add_argument("--out", default=str(ROOT / "tmp" / "belong_600_normal.json"))
    parser.add_argument("--out-csv", default=str(ROOT / "tmp" / "belong_600_normal_samples.csv"))
    parser.add_argument("--events-csv", default=str(ROOT / "tmp" / "belong_600_normal_events.csv"))
    args = parser.parse_args()
    report = run(args)
    print(json.dumps(clean({
        "start": report["start"],
        "end": report["end"],
        "hours": report["hours"],
        "labels": report["labels"],
        "events": report["events"],
    }), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
