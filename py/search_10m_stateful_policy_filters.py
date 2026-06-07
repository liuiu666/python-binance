"""Search 10-minute stateful execution policies on top of regime filters.

This is research-only. It simulates policies that need memory, such as not
opening overlapping options for the same strategy or cooling down after a loss.
Promotion still requires direct live shadow samples.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "E:/codex/py")
from backtest_enhanced import load_symbol  # noqa: E402
from research_strategy_lab import metric  # noqa: E402
from search_10m_regime_filters import (  # noqa: E402
    build_current_trades,
    build_live_replay_trades,
    chronological_blocks,
    filter_mask,
    read_json,
)
from validate_strategy_candidates import PAYOUT, STAKE  # noqa: E402

OUT = "E:/codex/data"
CONFIG_FILE = os.path.join(OUT, "prod_config.json")
REPORT_FILE = os.path.join(OUT, "ten_min_stateful_policy_filter_search.json")
STRATEGY_ID = "BTC_10min"
INTERVAL_MIN = 10
ENTRY_DELAY_MIN = 5
BREAKEVEN_WR = 100 / (1 + PAYOUT)


STATIC_FILTERS = [
    {
        "id": "BASE_10m_all_signals",
        "name": "base_all_signals",
        "filter": {},
        "note": "Current 10m production signal before additional regime filters.",
    },
    {
        "id": "SHADOW_10m_bbp105_conf_lt40_th55_rsi30_70_majority",
        "name": "bbp_1.05_confidence_lt_40",
        "filter": {"bbp_cap": 1.05, "confidence_max": 40},
        "note": "Best current 10m combo by stable offline WR/retention balance.",
    },
    {
        "id": "SHADOW_10m_bbp105_rsi78_conf_lt40_th55_rsi30_70_majority",
        "name": "bbp_1.05_rsi_cap_78_confidence_lt_40",
        "filter": {"bbp_cap": 1.05, "rsi_cap": 78, "confidence_max": 40},
        "note": "Stricter combo that lowers max loss in offline scan.",
    },
    {
        "id": "SHADOW_10m_bbp105_rsi78_conf_lt50_th55_rsi30_70_majority",
        "name": "bbp_1.05_rsi_cap_78_confidence_lt_50",
        "filter": {"bbp_cap": 1.05, "rsi_cap": 78, "confidence_max": 50},
        "note": "Moderate-retention strict combo with low offline max loss.",
    },
    {
        "id": "SHADOW_10m_bbp120_rsi74_conf_lt50_th55_rsi30_70_majority",
        "name": "bbp_1.20_rsi_cap_74_confidence_lt_50",
        "filter": {"bbp_cap": 1.20, "rsi_cap": 74, "confidence_max": 50},
        "note": "Alternative strict combo with lower offline max loss.",
    },
]


STATEFUL_POLICIES = [
    {
        "id": "static_only",
        "name": "static_only",
        "description": "Apply the static filter only.",
    },
    {
        "id": "one_open_position",
        "name": "one_open_position_per_strategy",
        "description": "Skip a signal while the same 10m strategy has an unsettled option.",
        "one_open_position": True,
    },
    {
        "id": "same_direction_gap_2x",
        "name": "same_direction_gap_2x_duration",
        "description": "Skip a same-direction signal if the previous selected same-direction entry is within two durations.",
        "same_direction_gap_mult": 2,
    },
    {
        "id": "one_open_loss_cooldown_1x",
        "name": "one_open_plus_loss_cooldown_1x",
        "description": "One open position plus one-duration cooldown after a selected loss settles.",
        "one_open_position": True,
        "loss_cooldown_mult": 1,
    },
    {
        "id": "same_direction_gap_2x_loss_cooldown_1x",
        "name": "same_direction_gap_2x_plus_loss_cooldown_1x",
        "description": "Two-duration same-direction gap plus one-duration cooldown after a selected loss settles.",
        "same_direction_gap_mult": 2,
        "loss_cooldown_mult": 1,
    },
]


def selected_with_entry_times(trades):
    out = trades.copy().reset_index(drop=True)
    out["time"] = pd.to_datetime(out["time"], utc=True)
    out["entry_time"] = out["time"] + pd.Timedelta(minutes=ENTRY_DELAY_MIN)
    out["expiry_time"] = out["entry_time"] + pd.Timedelta(minutes=INTERVAL_MIN)
    return out


def apply_stateful_policy(trades, policy):
    if trades.empty:
        return trades.copy()
    rows = selected_with_entry_times(trades).sort_values(["entry_time", "direction"]).reset_index(drop=True)
    keep = []
    active_until = None
    cooldown_until = None
    last_same_direction = {}
    duration = pd.Timedelta(minutes=INTERVAL_MIN)
    for idx, row in rows.iterrows():
        entry = row["entry_time"]
        expiry = row["expiry_time"]
        direction = row.get("direction")
        if active_until is not None and policy.get("one_open_position") and entry < active_until:
            continue
        if cooldown_until is not None and entry < cooldown_until:
            continue
        gap_mult = policy.get("same_direction_gap_mult")
        if gap_mult:
            previous = last_same_direction.get(direction)
            if previous is not None and entry < previous + duration * int(gap_mult):
                continue
        keep.append(idx)
        if policy.get("one_open_position"):
            active_until = expiry
        if gap_mult:
            last_same_direction[direction] = entry
        if policy.get("loss_cooldown_mult") and not bool(row.get("win")):
            cooldown_until = expiry + duration * int(policy["loss_cooldown_mult"])
    return rows.loc[keep].copy().reset_index(drop=True)


def required_features(params):
    required = []
    if "bbp_cap" in params:
        required.append("bbp")
    if "rsi_cap" in params:
        required.append("rsi14")
    if "confidence_max" in params:
        required.append("confidence")
    return required


def has_required_features(trades, params):
    for col in required_features(params):
        if col not in trades.columns or trades[col].isna().all():
            return False, col
    return True, None


def summarize_selected(selected):
    overall = metric(selected["win"].to_numpy()) if not selected.empty else metric([])
    blocks, block_summary = chronological_blocks(selected[["time", "win"]].copy() if not selected.empty else selected)
    return overall, blocks, block_summary


def summarize_candidate(base_trades, live_trades, base_overall, base_live_overall, static_row, policy):
    static_selected = base_trades.loc[filter_mask(base_trades, static_row["filter"])].copy().reset_index(drop=True)
    static_overall = metric(static_selected["win"].to_numpy()) if not static_selected.empty else metric([])
    selected = apply_stateful_policy(static_selected, policy)
    overall, blocks, block_summary = summarize_selected(selected)

    live_summary = {
        "sample": "none",
        "overall": None,
        "wr_delta_pp": None,
        "trade_retention_pct": None,
        "note": "No settled live signal sample is available for replay.",
    }
    if live_trades is not None and not live_trades.empty:
        ok, missing = has_required_features(live_trades, static_row["filter"])
        if not ok:
            live_summary = {
                "sample": "missing_features",
                "overall": None,
                "wr_delta_pp": None,
                "trade_retention_pct": None,
                "note": f"Live replay cannot evaluate {missing}; collect direct shadow samples with current signal fields.",
            }
        else:
            live_static = live_trades.loc[filter_mask(live_trades, static_row["filter"])].copy().reset_index(drop=True)
            live_selected = apply_stateful_policy(live_static, policy)
            live_overall = metric(live_selected["win"].to_numpy()) if not live_selected.empty else metric([])
            live_summary = {
                "sample": "diagnostic_small_sample" if int(base_live_overall.get("trades") or 0) < 50 else "readable",
                "overall": live_overall,
                "wr_delta_pp": (
                    round(float(live_overall.get("wr") or 0) - float(base_live_overall.get("wr") or 0), 2)
                    if int(live_overall.get("trades") or 0)
                    else None
                ),
                "trade_retention_pct": round(
                    int(live_overall.get("trades") or 0) / max(1, int(base_live_overall.get("trades") or 0)) * 100,
                    2,
                ),
                "note": "Live replay is diagnostic only; direct live shadow samples are required for promotion.",
            }

    return {
        "id": f"STATEFUL_10m_{static_row['name']}_{policy['id']}",
        "kind": "stateful_policy_overlay",
        "static_filter_id": static_row["id"],
        "static_filter_name": static_row["name"],
        "policy_id": policy["id"],
        "policy_name": policy["name"],
        "policy_description": policy["description"],
        "filter": static_row["filter"],
        "policy": {k: v for k, v in policy.items() if k not in ("id", "name", "description")},
        "note": static_row["note"],
        "static_overall": static_overall,
        "overall": overall,
        "time_block_summary": block_summary,
        "blocks": blocks,
        "wr_delta_pp": round(float(overall.get("wr") or 0) - float(base_overall.get("wr") or 0), 2),
        "static_wr_delta_pp": round(float(static_overall.get("wr") or 0) - float(base_overall.get("wr") or 0), 2),
        "stateful_wr_delta_vs_static_pp": round(float(overall.get("wr") or 0) - float(static_overall.get("wr") or 0), 2),
        "trade_retention_pct": round(int(overall.get("trades") or 0) / max(1, int(base_overall.get("trades") or 0)) * 100, 2),
        "stateful_retention_vs_static_pct": round(
            int(overall.get("trades") or 0) / max(1, int(static_overall.get("trades") or 0)) * 100,
            2,
        ),
        "live_replay": live_summary,
        "shadow_only": True,
    }


def rank_candidates(rows):
    stable = [
        r for r in rows
        if int(((r.get("overall") or {}).get("trades")) or 0) >= 180
        and float(((r.get("time_block_summary") or {}).get("min_block_wr")) or 0) >= 55
        and int(((r.get("time_block_summary") or {}).get("positive_blocks")) or 0) >= 9
        and int(((r.get("overall") or {}).get("max_loss")) or 0) <= 7
        and float(r.get("wr_delta_pp") or 0) > 0
    ]
    stable.sort(
        key=lambda r: (
            float(r.get("wr_delta_pp") or 0),
            -int(((r.get("overall") or {}).get("max_loss")) or 0),
            float(((r.get("time_block_summary") or {}).get("min_block_wr")) or 0),
            float(r.get("trade_retention_pct") or 0),
        ),
        reverse=True,
    )
    top = sorted(
        rows,
        key=lambda r: (
            float(r.get("wr_delta_pp") or 0),
            -int(((r.get("overall") or {}).get("max_loss")) or 0),
            float(r.get("trade_retention_pct") or 0),
        ),
        reverse=True,
    )
    overlay = [r for r in stable if r.get("policy_id") != "static_only"]
    overlay.sort(
        key=lambda r: (
            float(r.get("wr_delta_pp") or 0),
            -int(((r.get("overall") or {}).get("max_loss")) or 0),
            float(((r.get("time_block_summary") or {}).get("min_block_wr")) or 0),
            float(r.get("trade_retention_pct") or 0),
        ),
        reverse=True,
    )
    return top, stable, overlay


def main():
    cfg = (read_json(CONFIG_FILE, {}) or {}).get(STRATEGY_ID) or {}
    df5 = load_symbol("btcusdt")
    if df5 is None:
        raise SystemExit("No BTC data found")
    base_trades = build_current_trades(df5, cfg)
    live_trades = build_live_replay_trades(df5, cfg)
    base_overall = metric(base_trades["win"].to_numpy())
    base_live_overall = metric(live_trades["win"].to_numpy()) if live_trades is not None and not live_trades.empty else {}
    base_blocks, base_block_summary = chronological_blocks(base_trades)

    rows = []
    for static_row in STATIC_FILTERS:
        for policy in STATEFUL_POLICIES:
            rows.append(summarize_candidate(base_trades, live_trades, base_overall, base_live_overall, static_row, policy))
    top, stable, overlay = rank_candidates(rows)
    report = {
        "method": {
            "type": "focused_10m_stateful_policy_filter_search",
            "payout": PAYOUT,
            "stake": STAKE,
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "note": "Offline OOS plus live replay diagnostics only. Stateful policies must collect direct live shadow evidence before promotion.",
        },
        "baseline": {
            "id": STRATEGY_ID,
            "overall": base_overall,
            "live_replay_overall": base_live_overall,
            "time_block_summary": base_block_summary,
            "blocks": base_blocks,
        },
        "policy_candidates": rows,
        "top_policy_candidates": top[:10],
        "top_stable_policy_candidates": stable[:10],
        "top_stateful_overlay_candidates": overlay[:10],
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps({
        "baseline": report["baseline"]["overall"],
        "top_stable_policy_candidates": [
            {
                "id": row["id"],
                "wr": row["overall"]["wr"],
                "trades": row["overall"]["trades"],
                "max_loss": row["overall"]["max_loss"],
                "wr_delta_pp": row["wr_delta_pp"],
                "retention": row["trade_retention_pct"],
                "min_block_wr": (row.get("time_block_summary") or {}).get("min_block_wr"),
            }
            for row in stable[:8]
        ],
        "top_stateful_overlay_candidates": [
            {
                "id": row["id"],
                "wr": row["overall"]["wr"],
                "trades": row["overall"]["trades"],
                "max_loss": row["overall"]["max_loss"],
                "wr_delta_pp": row["wr_delta_pp"],
                "retention": row["trade_retention_pct"],
                "min_block_wr": (row.get("time_block_summary") or {}).get("min_block_wr"),
            }
            for row in overlay[:5]
        ],
    }, indent=2, ensure_ascii=False))
    print(f"Saved {REPORT_FILE}")


if __name__ == "__main__":
    main()
