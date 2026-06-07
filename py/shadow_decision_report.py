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
SIGNAL_AUDIT_FILE = os.path.join(OUT, "signal_audit_report.json")
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


def offline_key_for(candidate_id, strategy_id):
    mappings = {
        "BTC_10min": "current_prod",
        "BTC_30min": "current_prod",
        "SHADOW_10m_strict_th58_rsi30_70_all3": "ml_th58_rsi30_70_all3_none",
        "SHADOW_10m_guard_th68_rsi30_70_all3": "ml_th68_rsi30_70_all3_none",
        "SHADOW_10m_more_trades_th60_rsi35_65_vol_hi_majority": "ml_th60_rsi35_65_majority_none",
        "SHADOW_10m_recent_scan_th65_rsi35_65_all3": "ml_th65_rsi35_65_all3_none",
        "SHADOW_10m_ctcool_t630_str30": "ml_th55_rsi30_70_majority_ctcool_t630_str30",
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
    if candidate_id in mappings:
        return mappings[candidate_id]
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
        rows[strategy_id] = metric
    shadow = signal_audit.get("shadow_candidates") or {}
    for strategy_id, metric in (shadow.get("by_strategy") or {}).items():
        rows[strategy_id] = metric
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
    wr = float(live.get("wr") or 0)
    pnl = float(live.get("pnl") or 0)
    max_loss = int(live.get("max_loss") or 0)

    if config.get("autoTrade") is not False:
        reasons.append("autoTrade_not_disabled")

    if offline is None:
        reasons.append("offline_missing")
    else:
        if int(offline.get("trades") or 0) < MIN_OFFLINE_TRADES:
            reasons.append("offline_too_few_trades")
        if float(offline.get("wr") or 0) < MIN_OFFLINE_WR:
            reasons.append("offline_wr_below_gate")
        if (offline.get("min_block_wr") is None) or float(offline.get("min_block_wr") or 0) < MIN_OFFLINE_BLOCK_WR:
            reasons.append("offline_block_unstable")

    if settled < MIN_LIVE_SETTLED:
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
    signal_audit = read_json(SIGNAL_AUDIT_FILE, {})
    live_audit = read_json(LIVE_AUDIT_FILE, {})
    config = read_json(TRADE_CONFIG_FILE, {})
    lab_index = flatten_lab(lab)
    live = live_rows(signal_audit)

    candidates = []
    for candidate_id, live_metric in sorted(live.items()):
        base = base_strategy_for(candidate_id)
        offline_key = offline_key_for(candidate_id, base)
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
