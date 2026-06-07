"""Search focused 30-minute regime filters for live shadow candidates.

The search starts from the current BTC_30min production signal and applies
simple causal filters that can be computed at signal time. The output is
research-only; candidates must collect live shadow samples before promotion.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "E:/codex/py")
from analyze_live_backtest_gap import live_signal_trades  # noqa: E402
from backtest_enhanced import build_features, load_symbol  # noqa: E402
from research_strategy_lab import build_oos_frame, candidate_signals, metric  # noqa: E402
from validate_strategy_candidates import PAYOUT, STAKE  # noqa: E402

OUT = "E:/codex/data"
CONFIG_FILE = os.path.join(OUT, "prod_config.json")
REPORT_FILE = os.path.join(OUT, "thirty_min_regime_filter_search.json")
BREAKEVEN_WR = 100 / (1 + PAYOUT)
STRATEGY_ID = "BTC_30min"


HAND_PICKED_SHADOWS = [
    {
        "id": "SHADOW_30m_conf_lt40_th55_rsi30_70_majority",
        "name": "confidence_lt_40",
        "filter": {"confidence_max": 40},
        "note": "Highest offline WR in the focused 30m scan; filters rare overextension/high-strength signals.",
    },
    {
        "id": "SHADOW_30m_conf_lt50_th55_rsi30_70_majority",
        "name": "confidence_lt_50",
        "filter": {"confidence_max": 50},
        "note": "Balanced confidence cap with high retention; useful live-drift diagnostic.",
    },
    {
        "id": "SHADOW_30m_skip_hour12_th55_rsi30_70_majority",
        "name": "skip_hour_12",
        "filter": {"skip_hours_utc": [12]},
        "note": "High-retention UTC hour filter with positive offline delta.",
    },
    {
        "id": "SHADOW_30m_skip_hour6_th55_rsi30_70_majority",
        "name": "skip_hour_6",
        "filter": {"skip_hours_utc": [6]},
        "note": "High-retention UTC hour filter with positive offline delta.",
    },
    {
        "id": "SHADOW_30m_bbp105_rsi80_th55_rsi30_70_majority",
        "name": "bbp_1.05_rsi_cap_80",
        "filter": {"bbp_cap": 1.05, "rsi_cap": 80},
        "note": "Win-rate-first BBP+RSI filter; shadow only because max loss is not improved offline.",
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
        if part.empty:
            continue
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
    frame = build_oos_frame(df5, STRATEGY_ID, int(cfg["horizon"]))
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
    return trades


def build_live_replay_trades(df5, cfg):
    rows = [r for r in live_signal_trades() if r.get("strategy") == STRATEGY_ID]
    if not rows:
        return pd.DataFrame()
    features = build_features(df5, int(cfg["horizon"]))
    features = features[features["target"] != 0].copy()
    features["time_key"] = pd.to_datetime(features["time"], utc=True)
    feature_cols = [
        "time_key", "bbp", "hlp20", "hlp50", "trend6", "trend12",
        "trend30", "pre50", "ema_stack", "rsi14",
    ]
    available = [c for c in feature_cols if c in features.columns]
    by_time = features[available].set_index("time_key")
    live_rows = []
    for row in rows:
        signal_time = pd.to_datetime(row.get("time"), utc=True).floor("5min")
        item = {
            "time": signal_time,
            "direction_num": 1 if row.get("direction") == "UP" else 0,
            "direction": row.get("direction"),
            "win": bool(row.get("win")),
            "confidence": float(row.get("confidence") or 0),
            "rsi14": float(row.get("rsi_value") or 50),
            "hour_utc": int(signal_time.hour),
            "trend_score": int(row.get("trend_score") or 0),
        }
        for col in feature_cols:
            if col != "time_key":
                item[col] = np.nan
        if signal_time in by_time.index:
            feat = by_time.loc[signal_time]
            for col in available:
                if col != "time_key":
                    item[col] = float(feat[col])
        live_rows.append(item)
    return pd.DataFrame(live_rows)


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
        "base_strategy": STRATEGY_ID,
        "filter": row["filter"],
        "note": row["note"],
        "overall": overall,
        "time_block_summary": block_summary,
        "blocks": blocks,
        "wr_delta_pp": round(float(overall.get("wr") or 0) - float(base_metrics.get("wr") or 0), 2),
        "trade_retention_pct": round(int(overall.get("trades") or 0) / max(1, int(base_metrics.get("trades") or 0)) * 100, 2),
        "shadow_only": True,
    }


def live_replay_summary(live_trades, base_live_metrics, row):
    if live_trades is None or live_trades.empty:
        return {
            "sample": "none",
            "overall": None,
            "wr_delta_pp": None,
            "trade_retention_pct": None,
            "note": "No settled live signal sample is available for replay.",
        }
    required = []
    if "bbp_cap" in row["filter"]:
        required.append("bbp")
    for col in required:
        if col not in live_trades.columns or live_trades[col].isna().all():
            return {
                "sample": "missing_features",
                "overall": None,
                "wr_delta_pp": None,
                "trade_retention_pct": None,
                "note": f"Live replay cannot evaluate {col}; wait for direct shadow samples with current signal fields.",
            }
    selected = live_trades.loc[filter_mask(live_trades, row["filter"])].copy().reset_index(drop=True)
    overall = metric(selected["win"].to_numpy())
    return {
        "sample": "diagnostic_small_sample" if int(base_live_metrics.get("trades") or 0) < 50 else "readable",
        "overall": overall,
        "wr_delta_pp": round(float(overall.get("wr") or 0) - float(base_live_metrics.get("wr") or 0), 2)
            if int(overall.get("trades") or 0) else None,
        "trade_retention_pct": round(int(overall.get("trades") or 0) / max(1, int(base_live_metrics.get("trades") or 0)) * 100, 2),
        "note": "Live replay is diagnostic only; direct live shadow samples are required for promotion.",
    }


def generate_scan_rows(trades, base_metrics):
    rows = []
    candidates = []
    for cap in [1.05, 1.10, 1.20, 1.30]:
        candidates.append({
            "id": f"SCAN_30m_bbp_cap{int(cap * 100)}",
            "name": f"bbp_not_too_extreme_{cap:.2f}",
            "filter": {"bbp_cap": cap},
            "note": "Generated BBP cap scan.",
        })
    for cap in [72, 74, 76, 78, 80, 82]:
        candidates.append({
            "id": f"SCAN_30m_rsi_cap{cap}",
            "name": f"rsi_extreme_cap_{cap}",
            "filter": {"rsi_cap": cap},
            "note": "Generated RSI cap scan.",
        })
    for cap in [30, 40, 50, 60]:
        candidates.append({
            "id": f"SCAN_30m_conf_lt{cap}",
            "name": f"confidence_lt_{cap}",
            "filter": {"confidence_max": cap},
            "note": "Generated confidence cap scan.",
        })
    for hour in range(24):
        candidates.append({
            "id": f"SCAN_30m_skip_hour{hour}",
            "name": f"skip_hour_{hour}",
            "filter": {"skip_hours_utc": [hour]},
            "note": "Generated hour skip scan.",
        })
    for hours in ([6, 7], [3, 7, 12], [0, 6], [3, 6, 7]):
        candidates.append({
            "id": "SCAN_30m_skip_hours_" + "_".join(str(h) for h in hours),
            "name": "skip_hours_" + "_".join(str(h) for h in hours),
            "filter": {"skip_hours_utc": list(hours)},
            "note": "Generated multi-hour live-drift scan.",
        })
    for bbp_cap in [1.05, 1.10, 1.20]:
        for rsi_cap in [74, 76, 78, 80]:
            candidates.append({
                "id": f"SCAN_30m_bbp{int(bbp_cap * 100)}_rsi{rsi_cap}",
                "name": f"bbp_{bbp_cap:.2f}_rsi_cap_{rsi_cap}",
                "filter": {"bbp_cap": bbp_cap, "rsi_cap": rsi_cap},
                "note": "Generated BBP plus RSI overextension scan.",
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
    cfg = config.get(STRATEGY_ID) or {}
    df5 = load_symbol("btcusdt")
    if df5 is None:
        raise SystemExit("No BTC data found")
    trades = build_current_trades(df5, cfg)
    live_trades = build_live_replay_trades(df5, cfg)
    base_overall = metric(trades["win"].to_numpy())
    base_live_overall = metric(live_trades["win"].to_numpy()) if not live_trades.empty else None
    base_blocks, base_block_summary = chronological_blocks(trades)
    shadows = [summarize_candidate(trades, base_overall, row) for row in HAND_PICKED_SHADOWS]
    for row, source in zip(shadows, HAND_PICKED_SHADOWS):
        row["live_replay"] = live_replay_summary(live_trades, base_live_overall or {}, source)
    scan_rows = generate_scan_rows(trades, base_overall)
    report = {
        "method": {
            "type": "focused_30m_regime_filter_search",
            "payout": PAYOUT,
            "stake": STAKE,
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "note": "Offline OOS search only. These filters must run as live shadow candidates before promotion.",
        },
        "baseline": {
            "id": STRATEGY_ID,
            "overall": base_overall,
            "live_replay_overall": base_live_overall,
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
                "live_replay": row["live_replay"]["sample"],
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
