from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_second_normal_drawdown_router as base
from second_backtest.data import load_second_bars


FEATURE_CSV = ROOT / "tmp" / "second_normal_trend_gate_candidates.csv"
OUT_JSON = ROOT / "tmp" / "second_normal_trend_gate_scan.json"

VALID_DAYS = {
    "2026-06-14",
    "2026-06-15",
    "2026-06-16",
    "2026-06-17",
    "2026-06-18",
    "2026-06-19",
    "2026-06-20",
    "2026-06-23",
    "2026-06-25",
    "2026-06-27",
    "2026-06-28",
    "2026-06-29",
    "2026-06-30",
    "2026-07-01",
    "2026-07-02",
    "2026-07-03",
    "2026-07-04",
}


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, tuple):
        return [clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        value = float(value)
        return value if math.isfinite(value) else None
    return value


def _bps(a: float, b: float) -> float:
    if not math.isfinite(a) or not math.isfinite(b) or b <= 0:
        return float("nan")
    return math.log(a / b) * 10000.0


def build_features() -> pd.DataFrame:
    if FEATURE_CSV.exists():
        return pd.read_csv(FEATURE_CSV)

    candidates = pd.read_csv(base.OUT_CANDIDATES)
    bars = load_second_bars(base.DATA_ANCHOR, include_shards=True)
    close = bars["close"].to_numpy(float)
    high = bars["high"].to_numpy(float)
    low = bars["low"].to_numpy(float)

    rows = []
    for row in candidates.to_dict("records"):
        idx = int(row["idx"])
        signal = str(row["signal"])
        direction = -1.0 if signal == "UP" else 1.0

        out = dict(row)
        for window in (30, 60, 120):
            trend = _bps(close[idx], close[idx - window]) if idx >= window else float("nan")
            out[f"trend_{window}s_bps"] = trend
            out[f"adverse_{window}s_bps"] = direction * trend

        # If an UP signal is printed at a fresh low with almost no bounce, it is
        # more likely a falling-knife state than a completed mean-reversion setup.
        # DOWN is symmetric at fresh highs.
        for window in (120, 600, 1800):
            lo = max(0, idx - window)
            hi = idx + 1
            if signal == "UP":
                segment = low[lo:hi]
                pos = int(np.nanargmin(segment))
                extreme_price = float(segment[pos])
                out[f"extreme_age_{window}s"] = idx - (lo + pos)
                out[f"reversal_from_extreme_{window}s_bps"] = (
                    (float(close[idx]) - extreme_price) / float(close[idx]) * 10000.0
                    if close[idx] > 0
                    else float("nan")
                )
                previous = low[lo:max(lo + 1, idx - 5)]
                prev_extreme = float(np.nanmin(previous)) if len(previous) else extreme_price
                out[f"breakout_beyond_{window}s_bps"] = (
                    (prev_extreme - float(close[idx])) / float(close[idx]) * 10000.0
                    if close[idx] > 0
                    else float("nan")
                )
            else:
                segment = high[lo:hi]
                pos = int(np.nanargmax(segment))
                extreme_price = float(segment[pos])
                out[f"extreme_age_{window}s"] = idx - (lo + pos)
                out[f"reversal_from_extreme_{window}s_bps"] = (
                    (extreme_price - float(close[idx])) / float(close[idx]) * 10000.0
                    if close[idx] > 0
                    else float("nan")
                )
                previous = high[lo:max(lo + 1, idx - 5)]
                prev_extreme = float(np.nanmax(previous)) if len(previous) else extreme_price
                out[f"breakout_beyond_{window}s_bps"] = (
                    (float(close[idx]) - prev_extreme) / float(close[idx]) * 10000.0
                    if close[idx] > 0
                    else float("nan")
                )
        rows.append(out)

    df = pd.DataFrame(rows)
    df.to_csv(FEATURE_CSV, index=False, encoding="utf-8-sig")
    return df


def trend_gate_blocks(row: dict[str, Any], cfg: dict[str, float]) -> bool:
    adverse_60 = float(row.get("adverse_60s_bps", 0.0))
    adverse_120 = float(row.get("adverse_120s_bps", 0.0))
    age_120 = float(row.get("extreme_age_120s", 9999.0))
    age_600 = float(row.get("extreme_age_600s", 9999.0))
    reversal_120 = float(row.get("reversal_from_extreme_120s_bps", 9999.0))
    reversal_600 = float(row.get("reversal_from_extreme_600s_bps", 9999.0))
    breakout_600 = float(row.get("breakout_beyond_600s_bps", 0.0))
    breakout_1800 = float(row.get("breakout_beyond_1800s_bps", 0.0))

    fresh_extreme = (
        age_120 <= cfg["fresh_age_120"]
        and reversal_120 <= cfg["max_reversal_120"]
        and adverse_60 >= cfg["min_adverse_60"]
        and adverse_120 >= cfg["min_adverse_120"]
    )
    range_break = (
        age_600 <= cfg["fresh_age_600"]
        and reversal_600 <= cfg["max_reversal_600"]
        and breakout_600 >= cfg["min_breakout_600"]
        and breakout_1800 >= cfg["min_breakout_1800"]
    )
    return bool(fresh_extreme or range_break)


def select_with_gate(df: pd.DataFrame, cfg: dict[str, float], *, strict_obs: float = 88.0) -> list[dict[str, Any]]:
    usable = df[df["day"].astype(str).isin(VALID_DAYS)].copy()
    by_idx: dict[int, list[dict[str, Any]]] = {}
    for row in usable.to_dict("records"):
        by_idx.setdefault(int(row["idx"]), []).append(row)

    accepted: list[dict[str, Any]] = []
    last_idx = -10**12
    loss_count = 0
    cool_until = -10**12
    for idx in sorted(by_idx):
        if idx - last_idx < 600 or idx < cool_until:
            continue
        rows = by_idx[idx]
        route_sigma = float(rows[0]["routeSigma"])
        selected = None
        for role in base.role_order(route_sigma, low_hi=9.0, mid_hi=22.0, high_lo=16.0):
            role_rows = [row for row in rows if row["role"] == role]
            if not role_rows:
                continue
            candidate = max(role_rows, key=lambda r: abs(float(r.get("p_up", 0.5)) - 0.5))
            if float(candidate.get("observed600Pct", 0.0)) < strict_obs:
                continue
            if float(candidate.get("observedLookbackPct", 0.0)) < strict_obs:
                continue
            if float(candidate["r10"]) > 42.0:
                continue
            if role == "mid" and float(candidate["routeSigma"]) >= 20.0:
                continue
            if candidate["signal"] == "DOWN" and float(candidate["r10"]) > 35.0:
                continue
            if trend_gate_blocks(candidate, cfg):
                continue
            selected = candidate
            break
        if selected is None:
            continue
        accepted.append(selected)
        last_idx = idx
        if selected["won"]:
            loss_count = 0
        else:
            loss_count += 1
            if loss_count >= 2:
                cool_until = idx + 3600
                loss_count = 0
    return accepted


def run() -> dict[str, Any]:
    df = build_features()
    configs = []
    for min_adv60 in (2.0, 4.0, 6.0, 8.0):
        for min_adv120 in (4.0, 8.0, 12.0):
            for rev120 in (0.8, 1.5, 2.5):
                for br600 in (0.0, 1.0, 2.0):
                    configs.append(
                        {
                            "fresh_age_120": 15.0,
                            "max_reversal_120": rev120,
                            "min_adverse_60": min_adv60,
                            "min_adverse_120": min_adv120,
                            "fresh_age_600": 45.0,
                            "max_reversal_600": 2.5,
                            "min_breakout_600": br600,
                            "min_breakout_1800": 0.0,
                        }
                    )

    results = []
    for cfg in configs:
        rows = select_with_gate(df, cfg, strict_obs=88.0)
        total = base.summarize(rows)
        recent = base.summarize([row for row in rows if str(row["day"]) >= base.RECENT_CUTOFF])
        train = base.summarize([row for row in rows if str(row["day"]) < base.RECENT_CUTOFF])
        if total["trades"] < 120 or recent["trades"] < 35:
            continue
        score = (
            total["pnl"]
            - total["maxDrawdownU"] * 7.0
            + recent["pnl"]
            + total["trades"] * 0.1
            - total["losingDays"] * 5.0
        )
        results.append(
            {
                "score": round(float(score), 4),
                "cfg": cfg,
                "total": total,
                "train": train,
                "recent": recent,
            }
        )
    results.sort(
        key=lambda item: (
            float(item["total"]["maxDrawdownU"]),
            -float(item["recent"]["pnl"]),
            -float(item["total"]["pnl"]),
        )
    )
    best_by_score = sorted(results, key=lambda item: item["score"], reverse=True)[:20]
    low_dd = results[:20]
    output = {
        "featureCsv": str(FEATURE_CSV),
        "tested": len(configs),
        "qualified": len(results),
        "bestByScore": best_by_score,
        "lowestDrawdown": low_dd,
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    result = run()
    print(json.dumps(clean({"tested": result["tested"], "qualified": result["qualified"]}), ensure_ascii=False))
    print("BEST_BY_SCORE")
    for item in result["bestByScore"][:10]:
        print(json.dumps(clean(item), ensure_ascii=False))
    print("LOWEST_DD")
    for item in result["lowestDrawdown"][:10]:
        print(json.dumps(clean(item), ensure_ascii=False))
