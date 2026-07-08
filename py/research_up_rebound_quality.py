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

import research_second_normal_drawdown_router as base  # noqa: E402
from second_backtest.data import load_second_bars  # noqa: E402


OUT_JSON = ROOT / "tmp" / "up_rebound_quality_research.json"
OUT_BUCKETS = ROOT / "tmp" / "up_rebound_quality_buckets.csv"
OUT_TRADES = ROOT / "tmp" / "up_rebound_quality_trades.csv"

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


def bps(now: float, prev: float) -> float:
    if not math.isfinite(now) or not math.isfinite(prev) or prev <= 0:
        return float("nan")
    return math.log(now / prev) * 10000.0


def attach_rebound_features(rows: list[dict[str, Any]], bars: pd.DataFrame) -> list[dict[str, Any]]:
    close = bars["close"].to_numpy(float)
    low = bars["low"].to_numpy(float)
    buy = bars["buy_qty"].to_numpy(float) if "buy_qty" in bars else np.zeros(len(bars))
    sell = bars["sell_qty"].to_numpy(float) if "sell_qty" in bars else np.zeros(len(bars))
    out: list[dict[str, Any]] = []
    for row in rows:
        idx = int(row["idx"])
        if idx <= 1800 or idx >= len(close):
            continue
        item = dict(row)
        price = float(close[idx])
        for sec in (60, 120, 300, 600, 1800):
            item[f"ret_{sec}s_bps"] = bps(price, float(close[idx - sec]))
        for sec in (120, 300, 600):
            start = max(0, idx - sec)
            segment = low[start : idx + 1]
            low_pos = int(np.nanargmin(segment))
            low_idx = start + low_pos
            low_price = float(low[low_idx])
            item[f"low_age_{sec}s"] = int(idx - low_idx)
            item[f"bounce_from_low_{sec}s_bps"] = (price / low_price - 1.0) * 10000.0 if low_price > 0 else float("nan")
        for sec in (30, 60, 120):
            start = max(0, idx - sec + 1)
            b = float(np.nansum(buy[start : idx + 1]))
            s = float(np.nansum(sell[start : idx + 1]))
            item[f"flow_{sec}s"] = (b - s) / (b + s) if b + s > 0 else 0.0
        out.append(item)
    return out


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return base.summarize(rows)


def bucketize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    frame = pd.DataFrame(rows)
    if frame.empty:
        return []
    specs = {
        "ret_60s_bps": [-999, -20, -10, -5, 0, 5, 10, 999],
        "ret_300s_bps": [-999, -40, -25, -10, 0, 10, 999],
        "ret_1800s_bps": [-999, -120, -80, -40, 0, 40, 999],
        "bounce_from_low_120s_bps": [-1, 0.5, 1.0, 2.0, 4.0, 8.0, 999],
        "bounce_from_low_600s_bps": [-1, 0.5, 1.0, 2.0, 4.0, 8.0, 999],
        "low_age_120s": [-1, 5, 15, 30, 60, 120, 9999],
        "low_age_600s": [-1, 10, 30, 60, 180, 600, 9999],
        "flow_60s": [-2, -0.5, -0.2, 0, 0.2, 0.5, 2],
    }
    rows_out: list[dict[str, Any]] = []
    for col, bins in specs.items():
        frame[f"{col}_bucket"] = pd.cut(frame[col].astype(float), bins=bins, include_lowest=True)
        for key, group in frame.groupby(f"{col}_bucket", observed=True, sort=True):
            recs = group.to_dict("records")
            s = summarize(recs)
            rows_out.append(
                {
                    "feature": col,
                    "bucket": str(key),
                    "trades": s["trades"],
                    "winRate": s["winRate"],
                    "pnl": s["pnl"],
                    "maxDrawdownU": s["maxDrawdownU"],
                    "maxLoss": s["maxLoss"],
                }
            )
    return rows_out


def up_rebound_bad(row: dict[str, Any], cfg: dict[str, float]) -> bool:
    if row.get("signal") != "UP":
        return False
    ret_300 = float(row.get("ret_300s_bps", 0.0))
    ret_1800 = float(row.get("ret_1800s_bps", 0.0))
    age_120 = float(row.get("low_age_120s", 9999.0))
    bounce_120 = float(row.get("bounce_from_low_120s_bps", 9999.0))
    bounce_600 = float(row.get("bounce_from_low_600s_bps", 9999.0))
    flow_60 = float(row.get("flow_60s", 0.0))
    fresh_weak = age_120 <= cfg["fresh_age_120"] and bounce_120 <= cfg["min_bounce_120"]
    broad_down = ret_300 <= cfg["max_ret_300"] or ret_1800 <= cfg["max_ret_1800"]
    no_flow = flow_60 <= cfg["min_flow_60"]
    no_broad_bounce = bounce_600 <= cfg["min_bounce_600"]
    return bool(fresh_weak and broad_down and (no_flow or no_broad_bounce))


def select_with_up_quality(rows: list[dict[str, Any]], cfg: dict[str, float]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_idx: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        if str(row.get("day")) not in VALID_DAYS:
            continue
        by_idx.setdefault(int(row["idx"]), []).append(row)

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    last_idx = -10**12
    cool_until = -10**12
    loss_streak = 0
    rolling: list[bool] = []
    for idx in sorted(by_idx):
        if idx - last_idx < 600:
            rejected.append({"idx": idx, "reason": "gap"})
            continue
        if idx < cool_until:
            rejected.append({"idx": idx, "reason": "loss_cooldown"})
            continue
        rows_at = by_idx[idx]
        route_sigma = float(rows_at[0]["routeSigma"])
        selected = None
        for role in base.role_order(route_sigma, low_hi=9.0, mid_hi=22.0, high_lo=16.0):
            role_rows = [row for row in rows_at if row["role"] == role]
            if not role_rows:
                continue
            candidate = sorted(role_rows, key=lambda r: abs(float(r.get("p_up", 0.5)) - 0.5), reverse=True)[0]
            if float(candidate.get("observed600Pct", 0.0)) < 88.0:
                continue
            if float(candidate.get("observedLookbackPct", 0.0)) < 88.0:
                continue
            if float(candidate["r10"]) > 42.0:
                continue
            if candidate["signal"] == "DOWN" and float(candidate["r10"]) > 35.0:
                continue
            if role == "mid" and float(candidate["routeSigma"]) >= 20.0:
                continue
            if up_rebound_bad(candidate, cfg):
                rejected.append({"idx": idx, "reason": "up_rebound_quality", "role": role})
                continue
            selected = candidate
            break
        if selected is None:
            continue
        item = dict(selected)
        item["policy"] = "up_quality"
        accepted.append(item)
        last_idx = idx
        if bool(item["won"]):
            loss_streak = 0
        else:
            loss_streak += 1
            if loss_streak >= 2:
                cool_until = max(cool_until, idx + 3600)
                loss_streak = 0
        rolling.append(bool(item["won"]))
        while len(rolling) > 6:
            rolling.pop(0)
        if len(rolling) >= 4 and sum(1 for won in rolling if not won) >= 3:
            cool_until = max(cool_until, idx + 3600)
            rolling = []
    return accepted, rejected


def run() -> dict[str, Any]:
    bars = load_second_bars(base.DATA_ANCHOR, include_shards=True)
    if base.OUT_CANDIDATES.exists():
        candidates = pd.read_csv(base.OUT_CANDIDATES).to_dict("records")
    else:
        candidates = base.build_candidates(bars)
        pd.DataFrame(candidates).to_csv(base.OUT_CANDIDATES, index=False, encoding="utf-8-sig")
    enriched = attach_rebound_features(candidates, bars)
    baseline = base.select_router(
        candidates,
        r10_cap=42.0,
        mid_route_sigma_cap=20.0,
        down_r10_cap=35.0,
        global_loss_cool_count=2,
        global_loss_cool_sec=3600,
        allowed_days=VALID_DAYS,
        min_observed_600_pct=88.0,
        min_observed_lookback_pct=88.0,
    )
    baseline_keys = {(int(row["idx"]), str(row["role"]), str(row["signal"])) for row in baseline}
    baseline_enriched = [
        row for row in enriched
        if (int(row["idx"]), str(row["role"]), str(row["signal"])) in baseline_keys
    ]
    up_baseline = [row for row in baseline_enriched if row.get("signal") == "UP"]

    configs = []
    for fresh_age in (5.0, 10.0, 15.0, 25.0):
        for b120 in (0.5, 1.0, 1.5, 2.5):
            for r300 in (-10.0, -20.0, -30.0, -40.0):
                for r1800 in (-40.0, -80.0, -120.0):
                    for flow in (-0.3, -0.1, 0.0, 0.2):
                        configs.append(
                            {
                                "fresh_age_120": fresh_age,
                                "min_bounce_120": b120,
                                "max_ret_300": r300,
                                "max_ret_1800": r1800,
                                "min_flow_60": flow,
                                "min_bounce_600": 1.0,
                            }
                        )
    results = []
    for cfg in configs:
        rows, rejected = select_with_up_quality(enriched, cfg)
        total = summarize(rows)
        recent = summarize([row for row in rows if str(row.get("day")) >= "2026-06-29"])
        removed = [row for row in baseline_enriched if up_rebound_bad(row, cfg)]
        if total["trades"] < 150 or recent["trades"] < 50:
            continue
        score = (
            total["pnl"]
            - total["maxDrawdownU"] * 6.0
            + recent["pnl"]
            - total["losingDays"] * 5.0
            + min(total["trades"], 220) * 0.1
        )
        results.append(
            {
                "score": round(float(score), 4),
                "cfg": cfg,
                "total": total,
                "recent": recent,
                "removedFromBaseline": summarize(removed),
                "rejectReasons": pd.Series([r["reason"] for r in rejected]).value_counts().head(12).to_dict() if rejected else {},
            }
        )
    results.sort(key=lambda item: item["score"], reverse=True)
    best_cfg = results[0]["cfg"] if results else None
    best_rows, _ = select_with_up_quality(enriched, best_cfg) if best_cfg else ([], [])

    bucket_rows = bucketize(up_baseline)
    pd.DataFrame(bucket_rows).to_csv(OUT_BUCKETS, index=False, encoding="utf-8-sig")
    pd.DataFrame(best_rows).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "data": {
            "anchor": str(base.DATA_ANCHOR),
            "rows": int(len(bars)),
            "start": bars.index.min().isoformat(),
            "end": bars.index.max().isoformat(),
            "validDays": sorted(VALID_DAYS),
        },
        "baseline": summarize(baseline),
        "baselineUp": summarize(up_baseline),
        "bucketCsv": str(OUT_BUCKETS),
        "tested": len(configs),
        "qualified": len(results),
        "best": results[:20],
        "tradesCsv": str(OUT_TRADES),
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    result = run()
    print(json.dumps(clean({
        "baseline": result["baseline"],
        "baselineUp": result["baselineUp"],
        "tested": result["tested"],
        "qualified": result["qualified"],
    }), ensure_ascii=False))
    print("BEST")
    for item in result["best"][:10]:
        print(json.dumps(clean(item), ensure_ascii=False))
