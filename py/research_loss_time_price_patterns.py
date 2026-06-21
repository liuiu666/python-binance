from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from research_arrival_forecast import DEFAULT_OLD_CSV, DEFAULT_SHARD_DIR, load_bars
from second_backtest.execution import execute_signals
from second_backtest.strategies import SecondNormalVwConfirmConfig, generate_normal_vw_confirm_signals


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "tmp" / "loss_time_price_patterns.json"


CONFIGS = [
    SecondNormalVwConfirmConfig(
        strategy_id="BTC_10min_SECOND_VW_STABLE_2700_20_ETA2",
        lookback_sec=2700,
        horizon_sec=600,
        signal_gap_sec=600,
        tail_pct=0.20,
        eta_target_bps=2.0,
        eta_max_wait_sec=45,
        amount=5,
        label="stable",
    ),
    SecondNormalVwConfirmConfig(
        strategy_id="BTC_10min_SECOND_VW_FAST_2700_27_ETA3",
        lookback_sec=2700,
        horizon_sec=600,
        signal_gap_sec=600,
        tail_pct=0.27,
        eta_target_bps=3.0,
        eta_max_wait_sec=45,
        amount=5,
        label="fast",
    ),
]


def bps(a: float, b: float) -> float:
    if not np.isfinite(a) or not np.isfinite(b) or b <= 0:
        return float("nan")
    return float(math.log(a / b) * 10000.0)


def q(values, p):
    arr = np.asarray([v for v in values if np.isfinite(v)], dtype=float)
    if len(arr) == 0:
        return None
    return round(float(np.quantile(arr, p)), 4)


def summary(values):
    arr = np.asarray([v for v in values if np.isfinite(v)], dtype=float)
    if len(arr) == 0:
        return {"n": 0}
    return {
        "n": int(len(arr)),
        "mean": round(float(np.mean(arr)), 4),
        "median": round(float(np.median(arr)), 4),
        "p10": q(arr, 0.10),
        "p25": q(arr, 0.25),
        "p75": q(arr, 0.75),
        "p90": q(arr, 0.90),
    }


def max_run_flags(flags):
    cur = 0
    out = 0
    for flag in flags:
        if flag:
            cur += 1
            out = max(out, cur)
        else:
            cur = 0
    return out


def features_for_trade(row: dict, bars: pd.DataFrame, idx_by_time: dict) -> dict | None:
    close = bars["close"].to_numpy(float)
    volume = bars["volume"].to_numpy(float)
    buy = bars["buy_qty"].to_numpy(float)
    sell = bars["sell_qty"].to_numpy(float)
    t = row["time"]
    idx = idx_by_time.get(t)
    if idx is None:
        return None
    entry = float(row["entry"])
    signal = row["signal"]
    side = 1.0 if signal == "UP" else -1.0
    out = {
        "strategyId": row["strategy_id"],
        "time": t.isoformat(),
        "day": t.date().isoformat(),
        "signal": signal,
        "won": bool(row["won"]),
        "entry": entry,
        "settle": float(row["settle"]),
        "p_up": row.get("p_up"),
        "vw_p_up": row.get("vw_p_up"),
        "eta_delay_sec": row.get("eta_delay_sec"),
    }
    for sec in (30, 60, 180, 300, 600, 900, 1800):
        if idx >= sec:
            ret = bps(close[idx], close[idx - sec])
            out[f"preRet{sec}sBps"] = ret
            out[f"preDirectional{sec}sBps"] = side * ret
            path = np.nansum(np.abs(np.diff(np.log(close[idx - sec : idx + 1]))) * 10000.0)
            out[f"path{sec}sBps"] = round(float(path), 4)
            out[f"efficiency{sec}s"] = round(float(abs(ret) / path), 4) if path > 1e-12 else None
        if idx + sec < len(close):
            ret = bps(close[idx + sec], close[idx])
            out[f"postRet{sec}sBps"] = ret
            out[f"postDirectional{sec}sBps"] = side * ret
    for sec in (60, 180, 300, 600):
        if idx >= sec:
            vol_now = float(np.nansum(volume[idx - sec + 1 : idx + 1]))
            flow = float(np.nansum(buy[idx - sec + 1 : idx + 1] - sell[idx - sec + 1 : idx + 1]))
            prev_start = max(0, idx - 2 * sec + 1)
            prev_end = max(0, idx - sec + 1)
            vol_prev = float(np.nansum(volume[prev_start:prev_end]))
            out[f"vol{sec}s"] = round(vol_now, 6)
            out[f"volAccel{sec}s"] = round(vol_now / max(vol_prev, 1e-12), 4)
            out[f"flowRatio{sec}s"] = round(float(np.nansum(buy[idx - sec + 1 : idx + 1]) / max(np.nansum(sell[idx - sec + 1 : idx + 1]), 1e-12)), 4)
            out[f"directionalFlow{sec}s"] = round(side * flow / max(vol_now, 1e-12), 4)
    if idx >= 600:
        window = close[idx - 600 : idx + 1]
        out["pre10mRangeBps"] = round(bps(float(np.nanmax(window)), float(np.nanmin(window))), 4)
        out["posInPre10m"] = round(float((close[idx] - np.nanmin(window)) / max(np.nanmax(window) - np.nanmin(window), 1e-12)), 4)
    return out


def group_compare(rows: list[dict], field: str) -> dict:
    wins = [r.get(field, float("nan")) for r in rows if r["won"]]
    losses = [r.get(field, float("nan")) for r in rows if not r["won"]]
    return {"wins": summary(wins), "losses": summary(losses)}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--old-csv", default=str(DEFAULT_OLD_CSV))
    p.add_argument("--shard-dir", default=str(DEFAULT_SHARD_DIR))
    p.add_argument("--out", default=str(DEFAULT_OUT))
    args = p.parse_args()

    bars = load_bars(Path(args.old_csv), Path(args.shard_dir))
    idx_by_time = {t: i for i, t in enumerate(bars.index)}
    trades = []
    for cfg in CONFIGS:
        raw = generate_normal_vw_confirm_signals(bars, cfg, apply_config_gap=True)
        executed, _ = execute_signals(raw, per_strategy_lock=True, cooldown_sec=cfg.horizon_sec, use_horizon_as_lock=True)
        for row in executed:
            feat = features_for_trade(row, bars, idx_by_time)
            if feat:
                trades.append(feat)
    trades.sort(key=lambda r: r["time"])
    by_strategy = {}
    for sid in sorted({r["strategyId"] for r in trades}):
        subset = [r for r in trades if r["strategyId"] == sid]
        by_strategy[sid] = {
            "trades": len(subset),
            "wins": sum(r["won"] for r in subset),
            "winRate": round(sum(r["won"] for r in subset) / len(subset) * 100, 2) if subset else 0,
            "maxLoss": max_run_flags([not r["won"] for r in subset]),
        }
    by_day = {}
    for day in sorted({r["day"] for r in trades}):
        subset = [r for r in trades if r["day"] == day]
        by_day[day] = {
            "trades": len(subset),
            "wins": sum(r["won"] for r in subset),
            "winRate": round(sum(r["won"] for r in subset) / len(subset) * 100, 2) if subset else 0,
            "maxLoss": max_run_flags([not r["won"] for r in subset]),
            "losses": [r for r in subset if not r["won"]],
        }
    fields = [
        "preRet60sBps", "preRet180sBps", "preRet300sBps", "preRet600sBps",
        "preDirectional60sBps", "preDirectional180sBps", "preDirectional300sBps", "preDirectional600sBps",
        "postRet60sBps", "postRet300sBps", "postRet600sBps",
        "volAccel60s", "volAccel180s", "volAccel300s", "flowRatio60s", "directionalFlow60s",
        "pre10mRangeBps", "posInPre10m", "eta_delay_sec",
    ]
    comparisons = {field: group_compare(trades, field) for field in fields}
    loss_rows = [r for r in trades if not r["won"]]
    burst_rules = {
        "pre1m_abs_ge_8bps": lambda r: abs(r.get("preRet60sBps", 0)) >= 8,
        "pre3m_abs_ge_15bps": lambda r: abs(r.get("preRet180sBps", 0)) >= 15,
        "pre5m_abs_ge_25bps": lambda r: abs(r.get("preRet300sBps", 0)) >= 25,
        "vol60_accel_ge_2": lambda r: r.get("volAccel60s", 0) >= 2,
        "range10m_ge_50bps": lambda r: r.get("pre10mRangeBps", 0) >= 50,
    }
    burst_stats = {}
    for name, pred in burst_rules.items():
        all_hit = [r for r in trades if pred(r)]
        loss_hit = [r for r in loss_rows if pred(r)]
        burst_stats[name] = {
            "allTradesHit": len(all_hit),
            "allHitRatePct": round(len(all_hit) / max(len(trades), 1) * 100, 2),
            "lossesHit": len(loss_hit),
            "lossHitRatePct": round(len(loss_hit) / max(len(loss_rows), 1) * 100, 2),
            "winRateWhenHit": round(sum(r["won"] for r in all_hit) / len(all_hit) * 100, 2) if all_hit else None,
        }
    report = {
        "sample": {
            "start": bars.index.min().isoformat(),
            "end": bars.index.max().isoformat(),
            "hours": round((bars.index.max() - bars.index.min()).total_seconds() / 3600, 2),
            "rows": int(len(bars)),
            "observedPct": round(float(bars["observed"].mean() * 100), 2),
        },
        "strategies": by_strategy,
        "byDay": {k: {kk: vv for kk, vv in v.items() if kk != "losses"} for k, v in by_day.items()},
        "badDays": {
            k: {
                "trades": v["trades"],
                "winRate": v["winRate"],
                "maxLoss": v["maxLoss"],
                "losses": v["losses"][:20],
            }
            for k, v in by_day.items()
            if v["winRate"] < 60 or v["maxLoss"] >= 3
        },
        "winLossComparisons": comparisons,
        "burstStats": burst_stats,
        "losses": loss_rows,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({
        "sample": report["sample"],
        "strategies": report["strategies"],
        "byDay": report["byDay"],
        "burstStats": report["burstStats"],
    }, ensure_ascii=False, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
