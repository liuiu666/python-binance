"""Build a promotion/watch/reject report for BTC strategy candidates.

The report combines:
- strict offline walk-forward research results
- live signal/shadow settlement results
- current safety config

It is intentionally conservative. A candidate cannot be promoted from offline
results alone, and small live samples remain watch-only.
"""
import json
import os
from collections import defaultdict

OUT = "E:/codex/data"
LAB_FILE = os.path.join(OUT, "strategy_research_lab_report.json")
TEN_MIN_REGIME_FILE = os.path.join(OUT, "ten_min_regime_filter_search.json")
SIGNAL_AUDIT_FILE = os.path.join(OUT, "signal_audit_report.json")
LIVE_BACKTEST_GAP_FILE = os.path.join(OUT, "live_backtest_gap_report.json")
LIVE_AUDIT_FILE = os.path.join(OUT, "live_trade_audit_report.json")
TRADE_CONFIG_FILE = os.path.join(OUT, "trade_config.json")
REPORT_FILE = os.path.join(OUT, "shadow_decision_report.json")

BREAKEVEN_WR = 54.05
MIN_LIVE_SETTLED = 100
MIN_LIVE_WR = 60.0
MAX_LIVE_LOSS_STREAK = 3
MIN_OFFLINE_WR = 60.0
MIN_OFFLINE_TRADES = 80
MIN_OFFLINE_BLOCK_WR = 52.0
EMPTY_LIVE_METRIC = {
    "settled": 0,
    "wins": 0,
    "losses": 0,
    "ties": 0,
    "wr": 0.0,
    "pnl": 0.0,
    "max_loss": 0,
    "pending": 0,
}
CANDIDATE_OFFLINE_KEYS = {
    "BTC_10min": "current_prod",
    "BTC_30min": "current_prod",
    "SHADOW_10m_strict_th58_rsi30_70_all3": "ml_th58_rsi30_70_all3_none",
    "SHADOW_10m_guard_th68_rsi30_70_all3": "ml_th68_rsi30_70_all3_none",
    "SHADOW_10m_more_trades_th60_rsi35_65_vol_hi_majority": "ml_th60_rsi35_65_majority_none",
    "SHADOW_10m_recent_scan_th65_rsi35_65_all3": "ml_th65_rsi35_65_all3_none",
    "SHADOW_10m_ctcool_t630_str30": "ml_th55_rsi30_70_majority_ctcool_t630_str30",
    "SHADOW_10m_bbp_cap105_th55_rsi30_70_majority": "ten_min_regime_filter_search",
    "SHADOW_10m_bbp_cap120_th55_rsi30_70_majority": "ten_min_regime_filter_search",
    "SHADOW_10m_rsi_cap74_th55_rsi30_70_majority": "ten_min_regime_filter_search",
    "SHADOW_10m_skip_hour12_th55_rsi30_70_majority": "ten_min_regime_filter_search",
    "SHADOW_10m_conf_lt40_th55_rsi30_70_majority": "ten_min_regime_filter_search",
    "SHADOW_30m_stable_th58_rsi30_70_all3": "ml_th58_rsi30_70_all3_none",
    "SHADOW_30m_guard_th68_rsi30_70_all3": "ml_th68_rsi30_70_all3_none",
    "SHADOW_30m_ctcool_t625_str30": "ml_th58_rsi30_70_majority_ctcool_t625_str30",
    "SHADOW_RULE_10m_rsi_reversal_30_70": "rule_rsi_rev_30_70_none",
    "SHADOW_RULE_10m_rsi_reversal_no_strong_trend": "rule_rsi_rev_30_70_no_strong_trend_score3",
    "SHADOW_RULE_10m_pullback_follow": "rule_pullback_s3_u60_65_d40_35",
    "SHADOW_RULE_10m_hybrid_regime": "hybrid_rule_regime_s3_rsi30_70",
    "SHADOW_RULE_30m_rsi_reversal_30_70": "rule_rsi_rev_30_70_none",
    "SHADOW_RULE_30m_rsi_reversal_no_strong_trend": "rule_rsi_rev_30_70_no_strong_trend_score3",
    "SHADOW_RULE_30m_pullback_follow": "rule_pullback_s3_u60_65_d40_35",
    "SHADOW_RULE_30m_hybrid_regime": "hybrid_rule_regime_s3_rsi30_70",
}


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def flatten_lab(lab):
    out = defaultdict(dict)
    if not lab:
        return out
    for strategy_id, payload in lab.get("strategies", {}).items():
        buckets = [
            [payload.get("current")] if payload.get("current") else [],
            payload.get("top_score") or [],
            payload.get("top_wr_usable") or [],
            payload.get("top_ml") or [],
            payload.get("top_rule_only") or [],
            payload.get("top_hybrid") or [],
        ]
        for rows in buckets:
            for row in rows:
                if not row:
                    continue
                name = row.get("name") or (row.get("candidate") or {}).get("name")
                if name and name not in out[strategy_id]:
                    out[strategy_id][name] = row
    return out


def flatten_ten_min_regime(report):
    out = {}
    for row in report.get("shadow_candidates") or []:
        if row.get("id"):
            out[row["id"]] = row
    return out


def offline_key_for(candidate_id, strategy_id):
    if candidate_id in CANDIDATE_OFFLINE_KEYS:
        return CANDIDATE_OFFLINE_KEYS[candidate_id]
    if candidate_id.startswith("SHADOW_RULE_"):
        return None
    return "current_prod" if strategy_id in ("BTC_10min", "BTC_30min") else None


def base_strategy_for(candidate_id, row=None):
    if row and row.get("shadowBaseStrategy"):
        return row.get("shadowBaseStrategy")
    if "10m" in candidate_id or candidate_id == "BTC_10min":
        return "BTC_10min"
    if "30m" in candidate_id or candidate_id == "BTC_30min":
        return "BTC_30min"
    return "unknown"


def candidate_type(candidate_id, grouped=None):
    if candidate_id.startswith("POLICY_"):
        return "policy_replay"
    if candidate_id.startswith("SHADOW_RULE_"):
        if "hybrid" in candidate_id:
            return "rule_hybrid"
        return "rule"
    if candidate_id.startswith("SHADOW_"):
        return "model_shadow"
    return "production"


def live_rows(signal_audit):
    rows = {}
    for strategy_id, metric in (signal_audit.get("by_strategy") or {}).items():
        rows[strategy_id] = {**metric, "source": "production_signal_audit"}
    shadow = signal_audit.get("shadow_candidates") or {}
    for strategy_id, metric in (shadow.get("by_strategy") or {}).items():
        rows[strategy_id] = {**metric, "source": "direct_shadow_audit"}
    derived = signal_audit.get("derived_shadow_candidates") or {}
    for strategy_id, metric in (derived.get("by_strategy") or {}).items():
        if strategy_id not in rows or int((rows[strategy_id] or {}).get("settled") or 0) == 0:
            rows[strategy_id] = {**metric, "source": "derived_replay"}
    return rows


def metric_to_live(metric, source):
    return {
        "settled": int(metric.get("trades") or metric.get("settled") or 0),
        "wins": int(metric.get("wins") or 0),
        "losses": int(metric.get("losses") or 0),
        "ties": int(metric.get("ties") or 0),
        "wr": metric.get("wr"),
        "pnl": metric.get("pnl_5u", metric.get("pnl")),
        "max_loss": metric.get("max_loss"),
        "pending": 0,
        "source": source,
    }


def metric_to_offline(metric, block_summary, name):
    return {
        "name": name,
        "kind": "execution_policy_replay",
        "trades": metric.get("trades"),
        "wr": metric.get("wr"),
        "pnl_5u": metric.get("pnl_5u"),
        "max_loss": metric.get("max_loss"),
        "min_block_wr": (block_summary or {}).get("min_block_wr"),
        "positive_blocks": (block_summary or {}).get("positive_blocks"),
        "active_blocks": (block_summary or {}).get("active_blocks"),
    }


def policy_replay_candidates(live_gap):
    rows = []
    for row in live_gap.get("policy_candidates") or []:
        candidate_id = row.get("id")
        if not candidate_id:
            continue
        rows.append({
            "id": candidate_id,
            "base_strategy": row.get("strategy") or base_strategy_for(candidate_id),
            "type": "policy_replay",
            "policy_name": row.get("name"),
            "description": row.get("description"),
            "live": metric_to_live(row.get("live") or {}, "policy_replay"),
            "offline": metric_to_offline(
                row.get("offline") or {},
                row.get("offline_block_summary") or {},
                row.get("name"),
            ),
            "policy_replay": {
                "live_wr_delta_pp": row.get("live_wr_delta_pp"),
                "offline_wr_delta_pp": row.get("offline_wr_delta_pp"),
                "offline_retention_pct": row.get("offline_retention_pct"),
                "evidence": row.get("evidence"),
                "live_block_summary": row.get("live_block_summary"),
                "offline_block_summary": row.get("offline_block_summary"),
            },
        })
    return rows


def offline_summary(row):
    if not row:
        return None
    overall = row.get("overall") or {}
    block = row.get("time_block_summary") or row.get("block_summary") or {}
    return {
        "name": row.get("name") or (row.get("candidate") or {}).get("name"),
        "kind": row.get("kind"),
        "trades": overall.get("trades"),
        "wr": overall.get("wr"),
        "pnl_5u": overall.get("pnl_5u"),
        "max_loss": overall.get("max_loss"),
        "min_block_wr": block.get("min_block_wr"),
        "positive_blocks": block.get("positive_blocks"),
        "active_blocks": block.get("active_blocks"),
    }


def judge(candidate_id, live, offline, config):
    reasons = []
    settled = int(live.get("settled") or 0)
    pending = int(live.get("pending") or 0)
    wr = float(live.get("wr") or 0)
    pnl = float(live.get("pnl") or 0)
    max_loss = int(live.get("max_loss") or 0)

    if config.get("autoTrade") is not False:
        reasons.append("autoTrade_not_disabled")
    if live.get("source") == "derived_replay":
        reasons.append("derived_replay_not_live_shadow")
    if live.get("source") == "policy_replay":
        reasons.append("policy_replay_not_live_shadow")

    if offline is None:
        reasons.append("offline_missing")
    else:
        if int(offline.get("trades") or 0) < MIN_OFFLINE_TRADES:
            reasons.append("offline_too_few_trades")
        if float(offline.get("wr") or 0) < MIN_OFFLINE_WR:
            reasons.append("offline_wr_below_gate")
        if (offline.get("min_block_wr") is None) or float(offline.get("min_block_wr") or 0) < MIN_OFFLINE_BLOCK_WR:
            reasons.append("offline_block_unstable")

    if settled == 0 and pending == 0:
        reasons.append("live_no_samples_yet")
    elif settled < MIN_LIVE_SETTLED:
        reasons.append("live_sample_too_small")
    if settled >= 20 and wr <= BREAKEVEN_WR:
        reasons.append("live_wr_below_breakeven")
    if settled >= 20 and pnl <= 0:
        reasons.append("live_pnl_not_positive")
    if settled >= 20 and max_loss > MAX_LIVE_LOSS_STREAK:
        reasons.append("live_loss_streak_too_high")

    promote_ok = (
        not reasons
        and settled >= MIN_LIVE_SETTLED
        and wr >= MIN_LIVE_WR
        and pnl > 0
        and max_loss <= MAX_LIVE_LOSS_STREAK
    )
    if promote_ok:
        decision = "promote_candidate"
    elif any(r.startswith("live_wr_below") or r.startswith("live_pnl") or r.startswith("live_loss") for r in reasons):
        decision = "reject_live_weak"
    elif offline is not None and any(r.startswith("offline") for r in reasons if r != "offline_missing"):
        decision = "reject_offline_weak"
    else:
        decision = "watch"
    return decision, reasons


def main():
    lab = read_json(LAB_FILE, {})
    ten_min_regime = read_json(TEN_MIN_REGIME_FILE, {})
    signal_audit = read_json(SIGNAL_AUDIT_FILE, {})
    live_gap = read_json(LIVE_BACKTEST_GAP_FILE, {})
    live_audit = read_json(LIVE_AUDIT_FILE, {})
    config = read_json(TRADE_CONFIG_FILE, {})
    lab_index = flatten_lab(lab)
    ten_min_regime_index = flatten_ten_min_regime(ten_min_regime)
    live = live_rows(signal_audit)

    candidates = []
    candidate_ids = sorted(set(CANDIDATE_OFFLINE_KEYS) | set(live))
    for candidate_id in candidate_ids:
        live_metric = live.get(candidate_id, dict(EMPTY_LIVE_METRIC))
        base = base_strategy_for(candidate_id)
        offline_key = offline_key_for(candidate_id, base)
        if offline_key == "ten_min_regime_filter_search":
            offline_row = ten_min_regime_index.get(candidate_id)
        else:
            offline_row = lab_index.get(base, {}).get(offline_key) if offline_key else None
        offline = offline_summary(offline_row)
        decision, reasons = judge(candidate_id, live_metric, offline, config)
        candidates.append({
            "id": candidate_id,
            "base_strategy": base,
            "type": candidate_type(candidate_id),
            "decision": decision,
            "reasons": reasons,
            "offline_key": offline_key,
            "offline": offline,
            "live": live_metric,
        })

    for policy in policy_replay_candidates(live_gap):
        decision, reasons = judge(policy["id"], policy["live"], policy["offline"], config)
        candidates.append({
            **policy,
            "decision": decision,
            "reasons": reasons,
            "offline_key": None,
        })

    summary_counts = defaultdict(int)
    for row in candidates:
        summary_counts[row["decision"]] += 1
    ranked = sorted(
        candidates,
        key=lambda x: (
            {"promote_candidate": 0, "watch": 1, "reject_offline_weak": 2, "reject_live_weak": 3}.get(x["decision"], 9),
            -(x["live"].get("settled") or 0),
            -(x["live"].get("wr") or 0),
        ),
    )
    report = {
        "method": {
            "type": "offline_live_shadow_decision",
            "breakeven_wr": BREAKEVEN_WR,
            "min_live_settled": MIN_LIVE_SETTLED,
            "min_live_wr": MIN_LIVE_WR,
            "max_live_loss_streak": MAX_LIVE_LOSS_STREAK,
            "min_offline_wr": MIN_OFFLINE_WR,
            "min_offline_trades": MIN_OFFLINE_TRADES,
            "min_offline_block_wr": MIN_OFFLINE_BLOCK_WR,
            "note": "Conservative gate; promote_candidate is still not an instruction to auto-trade.",
        },
        "safety": {
            "autoTrade": config.get("autoTrade"),
            "minConfidence": config.get("minConfidence"),
            "preventOverlapOrders": config.get("preventOverlapOrders"),
            "verdict": "do_not_resume_real_auto_trading",
        },
        "live_overall": signal_audit.get("overall"),
        "live_by_strategy": signal_audit.get("by_strategy"),
        "live_shadow_overall": (signal_audit.get("shadow_candidates") or {}).get("overall"),
        "live_trade_audit_overall": live_audit.get("overall") if live_audit else None,
        "summary_counts": dict(summary_counts),
        "candidates": ranked,
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps({
        "summary_counts": report["summary_counts"],
        "safety": report["safety"],
        "top": [
            {
                "id": c["id"],
                "decision": c["decision"],
                "reasons": c["reasons"],
                "live": c["live"],
                "offline": c["offline"],
            }
            for c in ranked[:8]
        ],
    }, indent=2, ensure_ascii=False))
    print(f"Saved {REPORT_FILE}")


if __name__ == "__main__":
    main()
