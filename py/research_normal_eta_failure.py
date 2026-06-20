from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from research_arrival_forecast import DEFAULT_OLD_CSV, DEFAULT_SHARD_DIR, first_hit, forecast_eta, load_bars, signal_side
from research_normal_eta import SecondNormalConfig, metrics
from second_backtest.execution import execute_signals
from second_backtest.strategies import generate_normal_signals


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "tmp" / "normal_eta_failure_latest.json"


BALANCED = {"lookback": 2700, "tail": 0.27, "target": 3.0, "wait": 45, "downOnly": False}
STABLE = {"lookback": 4200, "tail": 0.20, "target": 2.0, "wait": 45, "downOnly": False}


def build_features(bars: pd.DataFrame) -> pd.DataFrame:
    close = bars["close"].astype(float)
    buy = bars["buy_qty"].astype(float)
    sell = bars["sell_qty"].astype(float)
    volume = bars["volume"].astype(float)
    ret1 = np.log(close / close.shift(1)).replace([np.inf, -np.inf], np.nan)
    feat = pd.DataFrame(index=bars.index)
    for sec, name in ((60, "r1m"), (300, "r5m"), (900, "r15m"), (1800, "r30m"), (3600, "r1h"), (7200, "r2h")):
        feat[name] = close / close.shift(sec) - 1.0
    feat["vol5m"] = ret1.rolling(300, min_periods=60).std(ddof=1) * np.sqrt(300)
    feat["vol30m"] = ret1.rolling(1800, min_periods=300).std(ddof=1) * np.sqrt(1800)
    feat["volRatio"] = feat["vol5m"] / feat["vol30m"].clip(lower=1e-12)
    feat["flow60"] = (buy - sell).rolling(60, min_periods=1).sum()
    feat["flow300"] = (buy - sell).rolling(300, min_periods=1).sum()
    feat["volSum60"] = volume.rolling(60, min_periods=1).sum()
    feat["flow60Ratio"] = feat["flow60"] / feat["volSum60"].clip(lower=1e-12)
    lo = close.rolling(3600, min_periods=600).min()
    hi = close.rolling(3600, min_periods=600).max()
    feat["pos1h"] = (close - lo) / (hi - lo + 1e-12)
    return feat


def run_case(bars: pd.DataFrame, candidate: dict) -> list[dict]:
    cfg = SecondNormalConfig(
        strategy_id=f"NORMAL_{candidate['lookback']}_{int(candidate['tail'] * 100)}",
        lookback_sec=int(candidate["lookback"]),
        horizon_sec=600,
        signal_gap_sec=600,
        tail_pct=float(candidate["tail"]),
        second_filter="none",
    )
    raw = generate_normal_signals(bars, cfg, apply_config_gap=True)
    signals, _ = execute_signals(raw, per_strategy_lock=True, cooldown_sec=600, use_horizon_as_lock=True)
    close = bars["close"].to_numpy(float)
    high = bars["high"].to_numpy(float)
    low = bars["low"].to_numpy(float)
    buy = bars["buy_qty"].to_numpy(float)
    sell = bars["sell_qty"].to_numpy(float)
    feat = build_features(bars)
    rows = []
    for sig in signals:
        if candidate["downOnly"] and sig["signal"] != "DOWN":
            continue
        idx = int(sig["idx"])
        if idx + int(candidate["wait"]) + 600 >= len(close):
            continue
        side = signal_side(sig["signal"])
        fc = forecast_eta(
            close,
            buy,
            sell,
            idx,
            side,
            float(candidate["target"]),
            speed_window=30,
            accel_window=10,
            min_speed_bps=0.005,
        )
        if not fc.get("ok") or fc["eta_sec"] > int(candidate["wait"]):
            continue
        hit_idx, entry = first_hit(high, low, close, idx, side, float(candidate["target"]), int(candidate["wait"]))
        if hit_idx is None:
            continue
        settle = close[hit_idx + 600]
        won = bool(settle > entry if sig["signal"] == "UP" else settle < entry)
        f = feat.iloc[idx]
        rows.append(
            {
                "signal_time": bars.index[idx],
                "entry_time": bars.index[hit_idx],
                "day": bars.index[hit_idx].date().isoformat(),
                "signal": sig["signal"],
                "won": won,
                "delay_sec": int(hit_idx - idx),
                "eta_sec": float(fc["eta_sec"]),
                "r5m": float(f["r5m"]) if np.isfinite(f["r5m"]) else None,
                "r15m": float(f["r15m"]) if np.isfinite(f["r15m"]) else None,
                "r30m": float(f["r30m"]) if np.isfinite(f["r30m"]) else None,
                "r1h": float(f["r1h"]) if np.isfinite(f["r1h"]) else None,
                "r2h": float(f["r2h"]) if np.isfinite(f["r2h"]) else None,
                "volRatio": float(f["volRatio"]) if np.isfinite(f["volRatio"]) else None,
                "flow60Ratio": float(f["flow60Ratio"]) if np.isfinite(f["flow60Ratio"]) else None,
                "pos1h": float(f["pos1h"]) if np.isfinite(f["pos1h"]) else None,
            }
        )
    return rows


def summarize_filter(rows: list[dict], name: str, predicate) -> dict:
    kept = [row for row in rows if predicate(row)]
    if not kept:
        return {"name": name, "kept": 0}
    start = min(row["entry_time"] for row in kept)
    end = max(row["entry_time"] for row in kept)
    return {"name": name, "kept": len(kept), **metrics(kept, start, end)}


def build_report(args: argparse.Namespace) -> dict:
    bars = load_bars(Path(args.old_csv), Path(args.shard_dir))
    report = {
        "sample": {
            "start": bars.index.min().isoformat(),
            "end": bars.index.max().isoformat(),
            "hours": round((bars.index.max() - bars.index.min()).total_seconds() / 3600, 2),
        },
        "cases": {},
    }
    for label, candidate in (("balanced", BALANCED), ("stable", STABLE)):
        rows = run_case(bars, candidate)
        start = min(row["entry_time"] for row in rows) if rows else bars.index.min()
        end = max(row["entry_time"] for row in rows) if rows else bars.index.max()
        filters = [
            ("down_only", lambda r: r["signal"] == "DOWN"),
            ("up_only", lambda r: r["signal"] == "UP"),
            ("avoid_high_volratio_gt_1_5", lambda r: (r["volRatio"] or 0) <= 1.5),
            ("avoid_high_volratio_gt_1_2", lambda r: (r["volRatio"] or 0) <= 1.2),
            ("flow_align", lambda r: (r["flow60Ratio"] or 0) > 0 if r["signal"] == "UP" else (r["flow60Ratio"] or 0) < 0),
            ("trend_align", lambda r: (r["r30m"] or 0) > 0 if r["signal"] == "UP" else (r["r30m"] or 0) < 0),
            ("not_extreme_pos", lambda r: 0.15 <= (r["pos1h"] or 0.5) <= 0.85),
            ("down_or_flow_align", lambda r: r["signal"] == "DOWN" or ((r["flow60Ratio"] or 0) > 0)),
            ("up_requires_flow_and_trend", lambda r: r["signal"] == "DOWN" or ((r["flow60Ratio"] or 0) > 0 and (r["r30m"] or 0) > 0)),
        ]
        by_day = {}
        for day in sorted({row["day"] for row in rows}):
            sub = [row for row in rows if row["day"] == day]
            by_day[day] = metrics(sub, min(r["entry_time"] for r in sub), max(r["entry_time"] for r in sub))
        report["cases"][label] = {
            "candidate": candidate,
            "all": metrics(rows, start, end) if rows else {},
            "byDay": by_day,
            "filters": [summarize_filter(rows, name, pred) for name, pred in filters],
            "lossExamples": [
                {
                    **row,
                    "signal_time": row["signal_time"].isoformat(),
                    "entry_time": row["entry_time"].isoformat(),
                }
                for row in rows
                if not row["won"]
            ][:60],
        }
    return report


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--old-csv", default=str(DEFAULT_OLD_CSV))
    p.add_argument("--shard-dir", default=str(DEFAULT_SHARD_DIR))
    p.add_argument("--out", default=str(DEFAULT_OUT))
    return p.parse_args()


def main() -> None:
    args = parse_args()
    report = build_report(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report["sample"], ensure_ascii=False))
    for name, case in report["cases"].items():
        print(name, json.dumps(case["all"], ensure_ascii=False))
        for item in case["filters"]:
            print(json.dumps(item, ensure_ascii=False))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
