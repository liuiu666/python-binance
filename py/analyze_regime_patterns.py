"""Compare market regimes, rule signals, ML signals, and hybrid candidates.

This is a research-only report. It explains which signal family is working
in each 10m/30m horizon and highlights weak regimes in the current ML signal.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "E:/codex/py")
from backtest_enhanced import load_symbol  # noqa: E402
from research_strategy_lab import (  # noqa: E402
    BREAKEVEN_WR,
    HORIZONS,
    block_summary,
    build_oos_frame,
    candidate_signals,
    metric,
    time_blocks,
)

OUT = "E:/codex/data"
CONFIG_FILE = os.path.join(OUT, "prod_config.json")
TRADE_CONFIG_FILE = os.path.join(OUT, "trade_config.json")
REPORT_FILE = os.path.join(OUT, "regime_pattern_report.json")


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def align_bucket(direction, score):
    align = np.where(direction == 1, score, -score)
    if isinstance(align, np.ndarray):
        return np.select(
            [align >= 3, align > 0, align == 0, align <= -3],
            ["strong_aligned", "mild_aligned", "neutral", "strong_countertrend"],
            default="mild_countertrend",
        )
    if align >= 3:
        return "strong_aligned"
    if align > 0:
        return "mild_aligned"
    if align == 0:
        return "neutral"
    if align <= -3:
        return "strong_countertrend"
    return "mild_countertrend"


def bbp_zone(value):
    value = float(value)
    if value < 0:
        return "below_band"
    if value <= 0.5:
        return "lower_half"
    if value <= 1:
        return "upper_half"
    return "above_band"


def group_metric(rows, group_cols, win_col="win", min_rows=20, limit=12):
    if rows.empty:
        return []
    out = []
    for key, part in rows.groupby(group_cols, sort=True):
        if len(part) < min_rows:
            continue
        key_tuple = key if isinstance(key, tuple) else (key,)
        row = {col: val for col, val in zip(group_cols, key_tuple)}
        row.update(metric(part[win_col].to_numpy()))
        out.append(row)
    out.sort(key=lambda r: (float(r.get("wr") or 0), int(r.get("trades") or 0)), reverse=True)
    return out[:limit]


def market_regime_rows(frame):
    data = frame.copy()
    data["is_up"] = data["target"].astype(int) == 1
    data["bbp_zone"] = data["bbp"].astype(float).map(bbp_zone)
    out = []
    for key, part in data.groupby(["trend_label", "rsi_zone", "bbp_zone"], sort=True):
        total = int(len(part))
        if total < 50:
            continue
        up_rate = float(part["is_up"].mean() * 100)
        dominant = max(up_rate, 100 - up_rate)
        out.append({
            "trend_label": key[0],
            "rsi_zone": key[1],
            "bbp_zone": key[2],
            "rows": total,
            "up_rate": round(up_rate, 2),
            "down_rate": round(100 - up_rate, 2),
            "dominant_direction": "UP" if up_rate >= 50 else "DOWN",
            "dominant_rate": round(dominant, 2),
        })
    out.sort(key=lambda r: (float(r["dominant_rate"]), int(r["rows"])), reverse=True)
    return out[:18]


def candidate_set(strategy_id, cfg):
    base = cfg[strategy_id]
    skip_hours = sorted({int(h) for h in base.get("skip_hours_utc", [])})
    current = {
        "name": "current_ml_prod",
        "group": "model",
        "kind": "ml_rsi",
        "threshold": float(base.get("threshold", 0.55)),
        "rsi": (float(base.get("rsi_lo", 30)), float(base.get("rsi_hi", 70))),
        "agree_mode": base.get("agree_mode", "majority"),
        "trend_gate": "none",
        "skip_hours_utc": skip_hours,
    }
    return [
        current,
        {
            "name": "model_more_trades_rsi35_65",
            "group": "model",
            "kind": "ml_rsi",
            "threshold": 0.55,
            "rsi": (35, 65),
            "agree_mode": "majority",
            "trend_gate": "none",
            "skip_hours_utc": skip_hours,
        },
        {
            "name": "model_strict_th58_rsi30_70",
            "group": "model",
            "kind": "ml_rsi",
            "threshold": 0.58,
            "rsi": (30, 70),
            "agree_mode": "all3",
            "trend_gate": "none",
            "skip_hours_utc": skip_hours,
        },
        {
            "name": "rule_rsi_reversal_30_70",
            "group": "rule",
            "kind": "rule_rsi_reversal",
            "rsi": (30, 70),
            "trend_gate": "none",
        },
        {
            "name": "rule_rsi_reversal_35_65",
            "group": "rule",
            "kind": "rule_rsi_reversal",
            "rsi": (35, 65),
            "trend_gate": "none",
        },
        {
            "name": "rule_trend_follow_s3_rsi40_70",
            "group": "rule",
            "kind": "rule_trend_follow",
            "score_min": 3,
            "rsi_band": (40, 70),
        },
        {
            "name": "rule_pullback_follow_s3",
            "group": "rule",
            "kind": "rule_pullback_follow",
            "score_min": 3,
            "up_rsi_max": 60,
            "up_bbp_max": 0.65,
            "down_rsi_min": 40,
            "down_bbp_min": 0.35,
        },
        {
            "name": "hybrid_rule_regime_s3_rsi30_70",
            "group": "hybrid",
            "kind": "hybrid_rule_regime",
            "score_min": 3,
            "rsi": (30, 70),
        },
        {
            "name": "hybrid_ml_trend60_range55_s3",
            "group": "hybrid",
            "kind": "hybrid_trend_else_ml",
            "trend_threshold": 0.60,
            "range_threshold": 0.55,
            "score_min": 3,
            "rsi": (30, 70),
            "agree_mode": "majority",
            "skip_hours_utc": skip_hours,
        },
    ]


def evaluate_candidate(frame, cand):
    direction, mask = candidate_signals(frame, cand)
    selected = frame.loc[mask].copy().reset_index(drop=True)
    if selected.empty:
        return {
            "name": cand["name"],
            "group": cand["group"],
            "kind": cand["kind"],
            "overall": metric([]),
            "time_block_summary": {},
            "by_align": [],
            "weak_buckets": [],
        }
    selected_dir = direction[mask]
    selected["direction"] = np.where(selected_dir == 1, "UP", "DOWN")
    selected["win"] = selected_dir == selected["target"].astype(int).to_numpy()
    selected["align_bucket"] = align_bucket(selected_dir, selected["trend_score"].astype(int).to_numpy())
    selected["bbp_zone"] = selected["bbp"].astype(float).map(bbp_zone)
    blocks = time_blocks(frame, direction, mask)
    by_bucket = group_metric(selected, ["align_bucket", "rsi_zone"], min_rows=20, limit=20)
    weak = [
        r for r in by_bucket
        if int(r.get("trades") or 0) >= 20 and float(r.get("wr") or 0) < BREAKEVEN_WR
    ]
    weak.sort(key=lambda r: (float(r.get("wr") or 0), -int(r.get("trades") or 0)))
    return {
        "name": cand["name"],
        "group": cand["group"],
        "kind": cand["kind"],
        "overall": metric(selected["win"].to_numpy()),
        "time_block_summary": block_summary(blocks),
        "by_align": by_bucket,
        "weak_buckets": weak[:6],
    }


def best_by_group(rows):
    out = {}
    for group in sorted({r["group"] for r in rows}):
        group_rows = [
            r for r in rows
            if r["group"] == group
            and int((r.get("overall") or {}).get("trades") or 0) >= 50
        ]
        if not group_rows:
            continue
        group_rows.sort(
            key=lambda r: (
                float((r["overall"] or {}).get("wr") or 0),
                float((r["time_block_summary"] or {}).get("min_block_wr") or 0),
                -int((r["overall"] or {}).get("max_loss") or 0),
            ),
            reverse=True,
        )
        out[group] = group_rows[0]
    return out


def conclusions(strategy_id, candidates):
    out = []
    by_group = best_by_group(candidates)
    current = next((r for r in candidates if r["name"] == "current_ml_prod"), None)
    if current:
        cm = current["overall"]
        out.append(
            f"{strategy_id}: current ML reversal WR {cm.get('wr')}% over {cm.get('trades')} trades; max loss {cm.get('max_loss')}."
        )
    rule = by_group.get("rule")
    if current and rule:
        delta = round(float(rule["overall"].get("wr") or 0) - float(current["overall"].get("wr") or 0), 2)
        out.append(
            f"{strategy_id}: best rule-only candidate {rule['name']} WR {rule['overall'].get('wr')}% ({delta}pp vs current ML)."
        )
    hybrid = by_group.get("hybrid")
    if current and hybrid:
        delta = round(float(hybrid["overall"].get("wr") or 0) - float(current["overall"].get("wr") or 0), 2)
        out.append(
            f"{strategy_id}: best naive hybrid {hybrid['name']} WR {hybrid['overall'].get('wr')}% ({delta}pp vs current ML), so simple trend-follow switching is not enough."
        )
    if current and current.get("weak_buckets"):
        w = current["weak_buckets"][0]
        out.append(
            f"{strategy_id}: weakest current ML bucket is {w.get('align_bucket')} / {w.get('rsi_zone')} "
            f"WR {w.get('wr')}% over {w.get('trades')} trades."
        )
    return out


def main():
    cfg = read_json(CONFIG_FILE, {})
    trade_cfg = read_json(TRADE_CONFIG_FILE, {})
    df5 = load_symbol("btcusdt")
    if df5 is None:
        raise SystemExit("No BTC data found")
    report = {
        "method": {
            "type": "regime_pattern_rule_model_hybrid_comparison",
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "note": "Research only. This report compares signal families; it does not change production or enable trading.",
        },
        "safety": {
            "autoTrade": trade_cfg.get("autoTrade"),
            "verdict": "research_only_do_not_resume_real_auto_trading",
        },
        "data": {
            "start": str(df5["time"].min()),
            "end": str(df5["time"].max()),
            "rows_5m": int(len(df5)),
        },
        "strategies": {},
        "conclusions": [],
    }
    for strategy_id, horizon in HORIZONS.items():
        frame = build_oos_frame(df5, strategy_id, horizon)
        candidates = [evaluate_candidate(frame, cand) for cand in candidate_set(strategy_id, cfg)]
        candidates.sort(
            key=lambda r: (
                float((r["overall"] or {}).get("wr") or 0),
                int((r["overall"] or {}).get("trades") or 0),
            ),
            reverse=True,
        )
        strategy_conclusions = conclusions(strategy_id, candidates)
        report["strategies"][strategy_id] = {
            "horizon": horizon,
            "interval_min": int(horizon * 5),
            "market_regime_patterns": market_regime_rows(frame),
            "candidate_comparison": candidates,
            "best_by_group": best_by_group(candidates),
            "conclusions": strategy_conclusions,
        }
        report["conclusions"].extend(strategy_conclusions)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps({
        "safety": report["safety"],
        "conclusions": report["conclusions"],
        "best_by_group": {
            sid: {
                group: {
                    "name": row["name"],
                    "wr": row["overall"]["wr"],
                    "trades": row["overall"]["trades"],
                    "max_loss": row["overall"]["max_loss"],
                }
                for group, row in payload["best_by_group"].items()
            }
            for sid, payload in report["strategies"].items()
        },
    }, indent=2, ensure_ascii=False))
    print(f"Saved {REPORT_FILE}")


if __name__ == "__main__":
    main()
