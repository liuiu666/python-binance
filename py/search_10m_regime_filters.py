"""Search focused 10-minute regime filters for live shadow candidates.

The search starts from the current BTC_10min production signal and applies
simple causal filters that can be computed at signal time. The output is
research-only; candidates must collect live shadow samples before promotion.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "E:/codex/py")
from backtest_enhanced import load_symbol  # noqa: E402
from research_strategy_lab import build_oos_frame, candidate_signals, metric  # noqa: E402
from validate_strategy_candidates import PAYOUT, STAKE  # noqa: E402

OUT = "E:/codex/data"
CONFIG_FILE = os.path.join(OUT, "prod_config.json")
REPORT_FILE = os.path.join(OUT, "ten_min_regime_filter_search.json")
BREAKEVEN_WR = 100 / (1 + PAYOUT)


HAND_PICKED_SHADOWS = [
    {
        "id": "SHADOW_10m_bbp_cap105_th55_rsi30_70_majority",
        "name": "bbp_not_too_extreme_1.05",
        "filter": {"bbp_cap": 1.05},
        "note": "Highest offline WR in the focused 10m filter scan; lower trade retention.",
    },
    {
        "id": "SHADOW_10m_bbp_cap120_th55_rsi30_70_majority",
        "name": "bbp_not_too_extreme_1.20",
        "filter": {"bbp_cap": 1.20},
        "note": "High-retention BBP cap; better fit for high trade count.",
    },
    {
        "id": "SHADOW_10m_rsi_cap74_th55_rsi30_70_majority",
        "name": "rsi_extreme_cap_74",
        "filter": {"rsi_cap": 74},
        "note": "Cuts very stretched RSI signals; improved max loss in offline scan.",
    },
    {
        "id": "SHADOW_10m_skip_hour12_th55_rsi30_70_majority",
        "name": "skip_hour_12",
        "filter": {"skip_hours_utc": [12]},
        "note": "Simple time-of-day filter with high retention in offline scan.",
    },
    {
        "id": "SHADOW_10m_conf_lt40_th55_rsi30_70_majority",
        "name": "confidence_lt_40",
        "filter": {"confidence_max": 40},
        "note": "Drops rare very high-strength signals; almost all trades retained.",
    },
]


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def chronological_blocks(trades, blocks=10):
    if trades.empty:
        return [], {
            "active_blocks": 0,
            "positive_blocks": 0,
            "min_block_wr": None,
            "worst_block": None,
        }
    rows = []
    for i, idx in enumerate(np.array_split(np.arange(len(trades)), blocks), start=1):
        part = trades.iloc[idx]
        m = metric(part["win"].to_numpy())
        rows.append({
            "slice": f"block_{i:02d}",
            "start": str(part["time"].iloc[0]),
            "end": str(part["time"].iloc[-1]),
            **m,
        })
    active = [r for r in rows if int(r.get("trades") or 0) >= 20]
    if not active:
        summary = {
            "active_blocks": 0,
            "positive_blocks": 0,
            "min_block_wr": None,
            "worst_block": None,
        }
    else:
        worst = min(active, key=lambda r: float(r.get("wr") or 0))
        summary = {
            "active_blocks": len(active),
            "positive_blocks": sum(1 for r in active if float(r.get("pnl_5u") or 0) > 0),
            "min_block_wr": worst.get("wr"),
            "worst_block": worst.get("slice"),
        }
    return rows, summary


def build_current_trades(df5, cfg):
    frame = build_oos_frame(df5, "BTC_10min", int(cfg["horizon"]))
    cand = {
        "kind": "ml_rsi",
        "threshold": cfg["threshold"],
        "rsi": [cfg.get("rsi_lo", 30), cfg.get("rsi_hi", 70)],
        "agree_mode": cfg.get("agree_mode", "majority"),
        "trend_gate": "none",
        "skip_hours_utc": cfg.get("skip_hours_utc", []),
    }
    direction, mask = candidate_signals(frame, cand)
    trades = frame.loc[mask].copy().reset_index(drop=True)
    used_direction = direction[mask]
    trades["direction_num"] = used_direction
    trades["direction"] = np.where(used_direction == 1, "UP", "DOWN")
    trades["win"] = used_direction == trades["target"].astype(int).to_numpy()
    trades["confidence"] = np.round(np.abs(trades["avg"].astype(float) - 0.5) * 200, 1)
    trades["align_score"] = np.where(
        trades["direction_num"].to_numpy() == 1,
        trades["trend_score"].to_numpy(),
        -trades["trend_score"].to_numpy(),
    )
    return trades


def filter_mask(trades, params):
    mask = np.ones(len(trades), dtype=bool)
    direction = trades["direction_num"].to_numpy()
    if "bbp_cap" in params:
        cap = float(params["bbp_cap"])
        bbp = trades["bbp"].astype(float).to_numpy()
        mask &= np.where(direction == 0, bbp <= cap, bbp >= 1 - cap)
    if "rsi_cap" in params:
        cap = float(params["rsi_cap"])
        rsi = trades["rsi14"].astype(float).to_numpy()
        mask &= np.where(direction == 0, rsi <= cap, rsi >= 100 - cap)
    if "confidence_max" in params:
        mask &= trades["confidence"].astype(float).to_numpy() < float(params["confidence_max"])
    if params.get("skip_hours_utc"):
        mask &= ~trades["hour_utc"].isin(params["skip_hours_utc"]).to_numpy()
    return mask


def summarize_candidate(trades, base_metrics, row):
    selected = trades.loc[filter_mask(trades, row["filter"])].copy().reset_index(drop=True)
    overall = metric(selected["win"].to_numpy())
    blocks, block_summary = chronological_blocks(selected)
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": "ml_regime_filter",
        "base_strategy": "BTC_10min",
        "filter": row["filter"],
        "note": row["note"],
        "overall": overall,
        "time_block_summary": block_summary,
        "blocks": blocks,
        "wr_delta_pp": round(float(overall.get("wr") or 0) - float(base_metrics.get("wr") or 0), 2),
        "trade_retention_pct": round(int(overall.get("trades") or 0) / max(1, int(base_metrics.get("trades") or 0)) * 100, 2),
        "shadow_only": True,
    }


def generate_scan_rows(trades, base_metrics):
    rows = []
    candidates = []
    for cap in [1.0, 1.05, 1.10, 1.20]:
        candidates.append({
            "id": f"SCAN_10m_bbp_cap{int(cap * 100)}",
            "name": f"bbp_not_too_extreme_{cap:.2f}",
            "filter": {"bbp_cap": cap},
            "note": "Generated BBP cap scan.",
        })
    for cap in [72, 74, 76, 78, 80]:
        candidates.append({
            "id": f"SCAN_10m_rsi_cap{cap}",
            "name": f"rsi_extreme_cap_{cap}",
            "filter": {"rsi_cap": cap},
            "note": "Generated RSI cap scan.",
        })
    for cap in [30, 40, 50]:
        candidates.append({
            "id": f"SCAN_10m_conf_lt{cap}",
            "name": f"confidence_lt_{cap}",
            "filter": {"confidence_max": cap},
            "note": "Generated confidence cap scan.",
        })
    for hour in range(24):
        candidates.append({
            "id": f"SCAN_10m_skip_hour{hour}",
            "name": f"skip_hour_{hour}",
            "filter": {"skip_hours_utc": [hour]},
            "note": "Generated hour skip scan.",
        })
    for row in candidates:
        rows.append(summarize_candidate(trades, base_metrics, row))
    rows.sort(
        key=lambda r: (
            float(r.get("wr_delta_pp") or 0),
            float(r.get("trade_retention_pct") or 0),
            -int((r.get("overall") or {}).get("max_loss") or 0),
        ),
        reverse=True,
    )
    return rows


def main():
    config = read_json(CONFIG_FILE, {})
    cfg = config.get("BTC_10min") or {}
    df5 = load_symbol("btcusdt")
    if df5 is None:
        raise SystemExit("No BTC data found")
    trades = build_current_trades(df5, cfg)
    base_overall = metric(trades["win"].to_numpy())
    base_blocks, base_block_summary = chronological_blocks(trades)
    shadows = [summarize_candidate(trades, base_overall, row) for row in HAND_PICKED_SHADOWS]
    scan_rows = generate_scan_rows(trades, base_overall)
    report = {
        "method": {
            "type": "focused_10m_regime_filter_search",
            "payout": PAYOUT,
            "stake": STAKE,
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "note": "Offline OOS search only. These filters must run as live shadow candidates before promotion.",
        },
        "baseline": {
            "id": "BTC_10min",
            "overall": base_overall,
            "time_block_summary": base_block_summary,
            "blocks": base_blocks,
        },
        "shadow_candidates": shadows,
        "scan_top": scan_rows[:20],
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps({
        "baseline": report["baseline"]["overall"],
        "shadow_candidates": [
            {
                "id": row["id"],
                "wr": row["overall"]["wr"],
                "trades": row["overall"]["trades"],
                "wr_delta_pp": row["wr_delta_pp"],
                "retention": row["trade_retention_pct"],
                "min_block_wr": row["time_block_summary"]["min_block_wr"],
            }
            for row in shadows
        ],
        "scan_top": [
            {
                "name": row["name"],
                "wr": row["overall"]["wr"],
                "trades": row["overall"]["trades"],
                "wr_delta_pp": row["wr_delta_pp"],
                "retention": row["trade_retention_pct"],
            }
            for row in scan_rows[:8]
        ],
    }, indent=2, ensure_ascii=False))
    print(f"Saved {REPORT_FILE}")


if __name__ == "__main__":
    main()
