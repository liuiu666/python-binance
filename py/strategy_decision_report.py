"""Build one decision report from backtest, shadow signal, and live execution.

For 85% payout binary options, breakeven win rate is 1 / (1 + 0.85).
This report makes the optimization loop explicit: validated edge first, then
shadow/live evidence as it accumulates.
"""
import json
import os

OUT = "E:/codex/data"
PAYOUT = 0.85
BREAKEVEN_WR = 100 / (1 + PAYOUT)
SHADOW_MIN_READABLE = 50
SHADOW_MIN_PROMOTION = 100
SHADOW_MIN_WR_GAIN_PP = 1.5
SHADOW_MAX_LOSS_EXTRA = 2

FILES = {
    "validation": os.path.join(OUT, "strategy_candidate_validation.json"),
    "signal": os.path.join(OUT, "signal_audit_report.json"),
    "live_backtest_gap": os.path.join(OUT, "live_backtest_gap_report.json"),
    "live": os.path.join(OUT, "live_trade_audit_report.json"),
    "latency": os.path.join(OUT, "execution_latency_validation.json"),
    "robustness": os.path.join(OUT, "strategy_robustness_profile.json"),
    "session_filters": os.path.join(OUT, "session_filter_validation.json"),
    "ten_min_filter_scan": os.path.join(OUT, "optimize_10min_filters.json"),
    "parallel_portfolio": os.path.join(OUT, "parallel_portfolio_report.json"),
    "queue_execution_policy": os.path.join(OUT, "queue_execution_policy_report.json"),
    "dual_causal_filter_search": os.path.join(OUT, "dual_strategy_causal_filter_search.json"),
    "dual_candidate_stability": os.path.join(OUT, "dual_strategy_candidate_stability.json"),
    "portfolio_filter_search": os.path.join(OUT, "portfolio_risk_filter_search.json"),
    "portfolio_filter_stability": os.path.join(OUT, "portfolio_filter_stability.json"),
    "health": os.path.join(OUT, "strategy_health_report.json"),
    "config": os.path.join(OUT, "prod_config.json"),
    "shadow_decision": os.path.join(OUT, "shadow_decision_report.json"),
}
REPORT_FILE = os.path.join(OUT, "strategy_decision_report.json")


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def edge(wr):
    return round(float(wr or 0) - BREAKEVEN_WR, 2)


def summarize_validation(validation):
    out = {}
    for strategy, data in validation.get("results", {}).items():
        ranked = data.get("ranked", [])
        if not ranked:
            continue
        top = ranked[0]
        full = top.get("full_oos", {})
        out[strategy] = {
            "selected_candidate": top.get("name"),
            "win_rate": full.get("wr"),
            "edge_over_breakeven": edge(full.get("wr")),
            "trades": full.get("trades"),
            "pnl_5u": full.get("pnl_5u"),
            "max_loss": full.get("max_loss"),
            "positive_blocks": top.get("positive_blocks"),
            "min_block_wr": top.get("min_block_wr"),
            "params": top.get("params"),
        }
    return out


def summarize_validation_ranking(validation):
    out = {}
    for strategy, data in validation.get("results", {}).items():
        rows = []
        for row in data.get("ranked", []):
            full = row.get("full_oos", {})
            rows.append({
                "name": row.get("name"),
                "win_rate": full.get("wr"),
                "edge_over_breakeven": edge(full.get("wr")),
                "trades": full.get("trades"),
                "pnl_5u": full.get("pnl_5u"),
                "max_loss": full.get("max_loss"),
                "positive_blocks": row.get("positive_blocks"),
                "min_block_wr": row.get("min_block_wr"),
                "params": row.get("params"),
            })
        out[strategy] = rows
    return out


def summarize_audit(report):
    by_strategy = {}
    for strategy, metrics in report.get("by_strategy", {}).items():
        by_strategy[strategy] = {
            **metrics,
            "edge_over_breakeven": edge(metrics.get("wr")),
        }
    out = {
        "overall": {
            **report.get("overall", {}),
            "edge_over_breakeven": edge(report.get("overall", {}).get("wr")),
        },
        "by_strategy": by_strategy,
        "reason_counts": report.get("reason_counts"),
        "snapshots": report.get("signal_snapshots"),
        "tradeable_signals": report.get("tradeable_signals"),
        "deduped_snapshots": report.get("deduped_snapshots"),
        "execution_funnel": report.get("execution_funnel"),
        "queue_execution": report.get("queue_execution"),
        "latest_heartbeat": ((report.get("execution_funnel") or {}).get("latest_heartbeat")),
        "readiness": report.get("readiness"),
    }
    if report.get("shadow_candidates"):
        out["shadow_candidates"] = summarize_audit(report["shadow_candidates"])
    return out


def summarize_live_backtest_gap(report):
    if not report:
        return {
            "status": "missing",
            "note": "Run py/analyze_live_backtest_gap.py to compare live drift against walk-forward OOS buckets.",
        }
    strategies = {}
    for strategy, row in (report.get("strategies") or {}).items():
        repeat = row.get("repeated_exposure") or {}
        strategies[strategy] = {
            "live": row.get("live"),
            "offline": row.get("offline"),
            "wr_gap_live_minus_offline_pp": row.get("wr_gap_live_minus_offline_pp"),
            "sample_warning": row.get("sample_warning"),
            "repeated_exposure": repeat,
            "loss_clusters": row.get("loss_clusters"),
            "filter_screen": row.get("filter_screen"),
            "strong_countertrend": (
                ((row.get("bucket_comparison") or {}).get("align_bucket") or {}).get("strong_countertrend")
            ),
        }
    return {
        "status": "ready",
        "method": report.get("method"),
        "safety": report.get("safety"),
        "data": report.get("data"),
        "overall": report.get("overall"),
        "policy_candidates": report.get("policy_candidates") or [],
        "repeated_exposure": report.get("repeated_exposure"),
        "strategies": strategies,
        "diagnosis": report.get("diagnosis") or [],
    }


def live_readiness(live):
    trades = int(live.get("trades") or 0)
    settled = int(live.get("overall", {}).get("settled") or 0)
    if settled >= 50:
        return "enough_for_first_live_read"
    if trades > 0:
        return "collect_more_settled_trades"
    readiness = live.get("readiness")
    if readiness == "has_autojs_events":
        return "autojs_seen_waiting_for_order_done"
    if readiness == "waiting_for_autojs_events":
        return "waiting_for_autojs_events"
    return "waiting_for_first_real_order"


def summarize_latency(latency):
    out = {}
    for strategy, data in latency.get("results", {}).items():
        by_delay = {}
        worst_edge = None
        worst_delay = None
        for delay, metrics in data.get("by_delay_min", {}).items():
            e = edge(metrics.get("wr"))
            by_delay[delay] = {
                **metrics,
                "edge_over_breakeven": e,
            }
            if worst_edge is None or e < worst_edge:
                worst_edge = e
                worst_delay = delay
        out[strategy] = {
            "opportunities": data.get("opportunities"),
            "by_delay_min": by_delay,
            "worst_delay_min": worst_delay,
            "worst_edge_over_breakeven": worst_edge,
        }
    return out


def summarize_robustness(robustness):
    out = {}
    for strategy, data in robustness.get("results", {}).items():
        overall = data.get("overall", {})
        risks = data.get("risk_summary", {})
        out[strategy] = {
            "overall": {
                **overall,
                "edge_over_breakeven": edge(overall.get("wr")),
            },
            "frequency": data.get("frequency", {}),
            "risk_summary": risks,
            "worst_time_block": (risks.get("time_blocks") or {}).get("worst_slice"),
            "min_time_block_wr": (risks.get("time_blocks") or {}).get("min_wr"),
            "worst_hour_utc": (risks.get("hour_utc") or {}).get("worst_slice"),
            "min_hour_wr": (risks.get("hour_utc") or {}).get("min_wr"),
            "worst_vol_quartile": (risks.get("vol_quartile") or {}).get("worst_slice"),
            "min_vol_quartile_wr": (risks.get("vol_quartile") or {}).get("min_wr"),
        }
    return out


def summarize_session_filters(session_filters):
    out = {}
    for strategy, data in session_filters.get("results", {}).items():
        ranked = data.get("ranked", [])
        if not ranked:
            continue
        best = ranked[0]
        current = next((r for r in ranked if r.get("name") == "all_hours"), None)
        out[strategy] = {
            "best_policy": best.get("name"),
            "skip_hours_utc": best.get("skip_hours_utc", []),
            "win_rate": (best.get("full_oos") or {}).get("wr"),
            "trades": (best.get("full_oos") or {}).get("trades"),
            "trade_retention_pct": best.get("trade_retention_pct"),
            "positive_blocks": best.get("positive_blocks"),
            "min_block_wr": best.get("min_block_wr"),
            "current_all_hours": current,
        }
    return out


def summarize_10min_filter_scan(scan):
    rows = scan.get("all") or scan.get("top") or []
    if not rows:
        return {
            "status": "missing",
            "note": "Run py/optimize_10min_filters.py to compare high-threshold 10-minute filters.",
        }

    production_like = next(
        (
            r for r in rows
            if abs(float(r.get("threshold", 0)) - 0.58) < 1e-9
            and int(r.get("rsi_lo", -1)) == 30
            and int(r.get("rsi_hi", -1)) == 70
        ),
        None,
    )
    high_wr = sorted(rows, key=lambda r: (float(r.get("wr") or 0), int(r.get("trades") or 0)), reverse=True)[:8]
    usable = [
        r for r in rows
        if float(r.get("wr") or 0) >= 60 and int(r.get("trades") or 0) >= 100
    ]
    usable.sort(key=lambda r: (float(r.get("wr") or 0), int(r.get("trades") or 0)), reverse=True)
    return {
        "method_note": scan.get("note"),
        "stake": scan.get("stake"),
        "production_like_recent_scan": production_like,
        "top_high_wr": high_wr,
        "usable_60pct_min100": usable[:5],
        "decision_note": (
            "High-threshold 10-minute filters can show very high recent WR, "
            "but candidates under 100 trades are treated as shadow-only evidence, not production changes."
        ),
    }


def build_production_summary(robustness_summary, latency_summary):
    out = {}
    for strategy, row in robustness_summary.items():
        overall = row.get("overall", {})
        latency = latency_summary.get(strategy, {})
        out[strategy] = {
            "win_rate": overall.get("wr"),
            "edge_over_breakeven": overall.get("edge_over_breakeven"),
            "trades": overall.get("trades"),
            "trades_per_day": (row.get("frequency") or {}).get("trades_per_day"),
            "trades_per_week": (row.get("frequency") or {}).get("trades_per_week"),
            "calendar_days": (row.get("frequency") or {}).get("calendar_days"),
            "pnl_5u": overall.get("pnl_5u"),
            "max_loss": overall.get("max_loss"),
            "min_time_block_wr": row.get("min_time_block_wr"),
            "positive_time_blocks": ((row.get("risk_summary") or {}).get("time_blocks") or {}).get("positive_slices"),
            "total_time_blocks": ((row.get("risk_summary") or {}).get("time_blocks") or {}).get("total_slices"),
            "worst_latency_edge_over_breakeven": latency.get("worst_edge_over_breakeven"),
            "worst_latency_delay_min": latency.get("worst_delay_min"),
        }
    return out


def summarize_parallel_portfolio(portfolio):
    if not portfolio:
        return {
            "status": "missing",
            "note": "Run py/analyze_parallel_portfolio.py to evaluate 10m + 30m together.",
        }
    overall = portfolio.get("overall") or {}
    per_strategy = {}
    for strategy, row in (portfolio.get("per_strategy") or {}).items():
        metrics = row.get("metrics") or {}
        per_strategy[strategy] = {
            "interval_min": row.get("interval_min"),
            "amount_usdt": row.get("amount_usdt"),
            "win_rate": metrics.get("wr"),
            "edge_over_breakeven": edge(metrics.get("wr")),
            "trades": metrics.get("trades"),
            "max_loss": metrics.get("max_loss"),
            "trades_per_day": (row.get("frequency") or {}).get("trades_per_day"),
        }
    return {
        "status": "ready",
        "win_rate": overall.get("wr"),
        "edge_over_breakeven": edge(overall.get("wr")),
        "trades": overall.get("trades"),
        "max_loss": overall.get("max_loss"),
        "pnl_5u": overall.get("pnl_5u"),
        "frequency": portfolio.get("frequency"),
        "per_strategy": per_strategy,
        "queue_policy": portfolio.get("queue_policy"),
        "overlap": portfolio.get("overlap"),
    }


def summarize_queue_execution_policy(report):
    if not report:
        return {
            "status": "missing",
            "note": "Run py/analyze_queue_execution_policy.py to compare AutoJS queue execution policies.",
        }
    out = {
        "status": "ready",
        "method": report.get("method"),
        "baseline_policy": report.get("baseline_policy"),
        "ranking": report.get("ranking") or [],
    }
    results = {}
    for name, row in (report.get("results") or {}).items():
        metrics = row.get("metrics") or {}
        results[name] = {
            "win_rate": metrics.get("wr"),
            "trades": metrics.get("trades"),
            "pnl_5u": metrics.get("pnl_5u"),
            "max_loss": metrics.get("max_loss"),
            "trades_per_day": (row.get("frequency") or {}).get("trades_per_day"),
            "same_candle_groups": row.get("same_candle_groups"),
            "direction_conflict_groups": row.get("direction_conflict_groups"),
        }
    out["results"] = results
    out["stability"] = {}
    for name, row in (report.get("stability") or {}).items():
        summary = row.get("summary") or {}
        out["stability"][name] = {
            "baseline_policy": row.get("baseline_policy"),
            "candidate_policy": row.get("candidate_policy"),
            "improved_blocks": summary.get("improved_blocks"),
            "worsened_blocks": summary.get("worsened_blocks"),
            "unchanged_blocks": summary.get("unchanged_blocks"),
            "blocks_with_trade_loss": summary.get("blocks_with_trade_loss"),
            "blocks_with_higher_max_loss": summary.get("blocks_with_higher_max_loss"),
            "min_candidate_wr": summary.get("min_candidate_wr"),
            "min_baseline_wr": summary.get("min_baseline_wr"),
        }
    delay_summary = ((report.get("delay_sensitivity") or {}).get("summary") or {})
    out["delay_sensitivity_summary"] = delay_summary
    out["best_policy"] = out["ranking"][0] if out["ranking"] else None
    high_frequency_candidates = [
        row for row in out["ranking"]
        if int(row.get("trade_delta_vs_baseline") or 0) >= -50
        and float(row.get("wr_delta_vs_baseline") or 0) >= 0
    ]
    out["best_high_frequency_policy"] = (
        sorted(
            high_frequency_candidates,
            key=lambda r: (
                float(r.get("wr_delta_vs_baseline") or 0),
                int(r.get("trades") or 0),
                float(r.get("pnl_5u") or 0),
            ),
            reverse=True,
        )[0]
        if high_frequency_candidates else None
    )
    return out


def summarize_portfolio_filter_search(search):
    ranked = search.get("ranked") or []
    baseline = search.get("baseline") or {}
    if not ranked:
        return {
            "status": "missing",
            "note": "Run py/optimize_portfolio_risk_filters.py to compare portfolio-level filter candidates.",
        }
    base_val = (baseline.get("validation") or {}).get("metrics") or {}
    best = ranked[0]
    best_val = (best.get("validation") or {}).get("metrics") or {}
    best_full = (best.get("full") or {}).get("metrics") or {}
    return {
        "status": "candidate_found" if best.get("name") != "baseline_parallel" else "baseline_best",
        "method": search.get("method"),
        "baseline_validation": {
            "win_rate": base_val.get("wr"),
            "trades": base_val.get("trades"),
            "max_loss": base_val.get("max_loss"),
            "edge_over_breakeven": edge(base_val.get("wr")),
        },
        "best_candidate": {
            "name": best.get("name"),
            "kind": best.get("kind"),
            "skip_hours_by_strategy": best.get("skip_hours_by_strategy"),
            "skip_hours_utc": best.get("skip_hours_utc"),
            "keep_strategy": best.get("keep_strategy"),
            "validation_win_rate": best_val.get("wr"),
            "validation_edge_over_breakeven": edge(best_val.get("wr")),
            "validation_trades": best_val.get("trades"),
            "validation_trade_retention_pct": best.get("validation_trade_retention_pct"),
            "validation_max_loss": best_val.get("max_loss"),
            "full_win_rate": best_full.get("wr"),
            "full_trades": best_full.get("trades"),
            "full_max_loss": best_full.get("max_loss"),
            "full_trades_per_day": ((best.get("full") or {}).get("frequency") or {}).get("trades_per_day"),
            "calibration_selected": best.get("calibration_selected"),
            "score": best.get("score"),
        },
        "top_candidates": [
            {
                "name": row.get("name"),
                "validation_win_rate": (((row.get("validation") or {}).get("metrics") or {}).get("wr")),
                "validation_trades": (((row.get("validation") or {}).get("metrics") or {}).get("trades")),
                "validation_max_loss": (((row.get("validation") or {}).get("metrics") or {}).get("max_loss")),
                "validation_trade_retention_pct": row.get("validation_trade_retention_pct"),
                "score": row.get("score"),
            }
            for row in ranked[:5]
        ],
    }


def summarize_portfolio_filter_stability(stability):
    if not stability:
        return {
            "status": "missing",
            "note": "Run py/validate_portfolio_filter_stability.py to audit candidate block stability.",
        }
    summary = stability.get("summary") or {}
    decision = stability.get("decision") or {}
    candidate = stability.get("candidate") or {}
    return {
        "status": decision.get("status"),
        "reason": decision.get("reason"),
        "candidate": candidate,
        "best_production_candidate": (
            (stability.get("best_production_candidate") or {}).get("candidate")
        ),
        "best_shadow_candidate": (
            (stability.get("best_shadow_candidate") or {}).get("candidate")
        ),
        "improved_blocks": summary.get("improved_blocks"),
        "worsened_blocks": summary.get("worsened_blocks"),
        "non_positive_candidate_blocks": summary.get("non_positive_candidate_blocks"),
        "min_candidate_wr": summary.get("min_candidate_wr"),
        "min_baseline_wr": summary.get("min_baseline_wr"),
        "avg_trade_retention_pct": summary.get("avg_trade_retention_pct"),
        "min_trade_retention_pct": summary.get("min_trade_retention_pct"),
        "blocks_with_higher_max_loss": summary.get("blocks_with_higher_max_loss"),
        "blocks": [
            {
                "slice": row.get("slice"),
                "baseline_wr": ((row.get("baseline") or {}).get("wr")),
                "candidate_wr": ((row.get("candidate") or {}).get("wr")),
                "wr_delta_pp": row.get("wr_delta_pp"),
                "trade_retention_pct": row.get("trade_retention_pct"),
                "max_loss_delta": row.get("max_loss_delta"),
            }
            for row in (stability.get("blocks") or [])
        ],
        "candidate_audits": [
            {
                "name": ((row.get("candidate") or {}).get("name")),
                "status": ((row.get("decision") or {}).get("status")),
                "reason": ((row.get("decision") or {}).get("reason")),
                "improved_blocks": ((row.get("summary") or {}).get("improved_blocks")),
                "worsened_blocks": ((row.get("summary") or {}).get("worsened_blocks")),
                "non_positive_candidate_blocks": ((row.get("summary") or {}).get("non_positive_candidate_blocks")),
                "min_candidate_wr": ((row.get("summary") or {}).get("min_candidate_wr")),
                "avg_trade_retention_pct": ((row.get("summary") or {}).get("avg_trade_retention_pct")),
            }
            for row in (stability.get("candidate_audits") or [])[:8]
        ],
    }


def summarize_dual_causal_search(search):
    if not search:
        return {
            "status": "missing",
            "note": "Run py/search_dual_strategy_causal_filters.py to scan dual-strategy causal filters.",
        }
    best = search.get("best_by_strategy") or {}
    hint = (search.get("production_hint") or {})
    out_best = {}
    for strategy, row in best.items():
        metrics = row.get("rolling_filtered") or {}
        cand = row.get("candidate") or {}
        out_best[strategy] = {
            "candidate": {
                "name": cand.get("name"),
                "threshold": cand.get("threshold"),
                "rsi": cand.get("rsi"),
                "agree_mode": cand.get("agree_mode"),
            },
            "max_skip_hours": row.get("max_skip_hours"),
            "stable_skip_hours": row.get("stable_skip_hours"),
            "rolling_win_rate": metrics.get("wr"),
            "rolling_trades": metrics.get("trades"),
            "rolling_max_loss": metrics.get("max_loss"),
            "rolling_trades_per_day": metrics.get("trades_per_day"),
            "score": row.get("score"),
        }
    combined = ((hint.get("combined") or {}).get("metrics") or {})
    return {
        "status": "ready",
        "method": search.get("method"),
        "best_by_strategy": out_best,
        "production_hint_combined": {
            "win_rate": combined.get("wr"),
            "edge_over_breakeven": edge(combined.get("wr")),
            "trades": combined.get("trades"),
            "max_loss": combined.get("max_loss"),
            "trades_per_day": combined.get("trades_per_day"),
        },
    }


def summarize_dual_candidate_stability(stability):
    if not stability:
        return {
            "status": "missing",
            "note": "Run py/validate_dual_strategy_candidate_stability.py to compare current config with causal candidate.",
        }
    combined = stability.get("combined") or {}
    decision = stability.get("decision") or {}
    per_strategy = {}
    for strategy, row in (stability.get("per_strategy") or {}).items():
        cur = row.get("current") or {}
        cand = row.get("candidate") or {}
        summary = row.get("summary") or {}
        per_strategy[strategy] = {
            "current_win_rate": cur.get("wr"),
            "current_trades": cur.get("trades"),
            "candidate_win_rate": cand.get("wr"),
            "candidate_trades": cand.get("trades"),
            "candidate_max_loss": cand.get("max_loss"),
            "candidate_trades_per_day": cand.get("trades_per_day"),
            "improved_blocks": summary.get("improved_blocks"),
            "worsened_blocks": summary.get("worsened_blocks"),
            "non_positive_candidate_blocks": summary.get("non_positive_candidate_blocks"),
            "candidate_config": row.get("candidate_config"),
        }
    cur = combined.get("current") or {}
    cand = combined.get("candidate") or {}
    summary = combined.get("summary") or {}
    return {
        "status": decision.get("status"),
        "reason": decision.get("reason"),
        "combined": {
            "current_win_rate": cur.get("wr"),
            "current_trades": cur.get("trades"),
            "candidate_win_rate": cand.get("wr"),
            "candidate_edge_over_breakeven": edge(cand.get("wr")),
            "candidate_trades": cand.get("trades"),
            "candidate_max_loss": cand.get("max_loss"),
            "candidate_trades_per_day": cand.get("trades_per_day"),
            "improved_blocks": summary.get("improved_blocks"),
            "worsened_blocks": summary.get("worsened_blocks"),
            "non_positive_candidate_blocks": summary.get("non_positive_candidate_blocks"),
        },
        "per_strategy": per_strategy,
    }


def evaluate_shadow_candidates(shadow_summary, production_summary):
    candidates = (shadow_summary or {}).get("by_strategy") or {}
    prod10 = (production_summary or {}).get("BTC_10min") or {}
    prod_wr = float(prod10.get("win_rate") or 0)
    prod_max_loss = int(prod10.get("max_loss") or 0)
    rows = []
    for strategy, metrics in sorted(candidates.items()):
        settled = int(metrics.get("settled") or 0)
        wr = float(metrics.get("wr") or 0)
        max_loss = int(metrics.get("max_loss") or 0)
        wr_gain = round(wr - prod_wr, 2)
        if settled < SHADOW_MIN_READABLE:
            status = "collect_more"
            reason = f"needs at least {SHADOW_MIN_READABLE} settled shadow trades for a first read"
        elif settled < SHADOW_MIN_PROMOTION:
            status = "readable_not_promotable"
            reason = f"needs at least {SHADOW_MIN_PROMOTION} settled shadow trades before promotion"
        elif wr_gain < SHADOW_MIN_WR_GAIN_PP:
            status = "reject"
            reason = f"WR gain {wr_gain}pp is below required {SHADOW_MIN_WR_GAIN_PP}pp"
        elif max_loss > prod_max_loss + SHADOW_MAX_LOSS_EXTRA:
            status = "reject"
            reason = f"max loss {max_loss} is worse than production allowance {prod_max_loss + SHADOW_MAX_LOSS_EXTRA}"
        else:
            status = "promote_candidate"
            reason = "shadow candidate beats production gates; validate with a fresh walk-forward pass before switching"
        rows.append({
            "strategy_id": strategy,
            "status": status,
            "reason": reason,
            "settled": settled,
            "win_rate": metrics.get("wr"),
            "wr_gain_vs_10min_prod": wr_gain,
            "pnl": metrics.get("pnl"),
            "max_loss": max_loss,
            "production_10min_wr": prod_wr,
            "production_10min_max_loss": prod_max_loss,
        })
    return {
        "gates": {
            "min_readable_settled": SHADOW_MIN_READABLE,
            "min_promotion_settled": SHADOW_MIN_PROMOTION,
            "min_wr_gain_vs_10min_prod_pp": SHADOW_MIN_WR_GAIN_PP,
            "max_loss_extra_allowed": SHADOW_MAX_LOSS_EXTRA,
        },
        "candidates": rows,
        "status": "no_tradeable_shadow_signals_yet" if not rows else "tracking",
    }


def main():
    validation = read_json(FILES["validation"], {})
    signal = read_json(FILES["signal"], {})
    live_backtest_gap = read_json(FILES["live_backtest_gap"], {})
    live = read_json(FILES["live"], {})
    latency = read_json(FILES["latency"], {})
    robustness = read_json(FILES["robustness"], {})
    session_filters = read_json(FILES["session_filters"], {})
    ten_min_filter_scan = read_json(FILES["ten_min_filter_scan"], {})
    parallel_portfolio = read_json(FILES["parallel_portfolio"], {})
    queue_execution_policy = read_json(FILES["queue_execution_policy"], {})
    dual_causal_filter_search = read_json(FILES["dual_causal_filter_search"], {})
    dual_candidate_stability = read_json(FILES["dual_candidate_stability"], {})
    portfolio_filter_search = read_json(FILES["portfolio_filter_search"], {})
    portfolio_filter_stability = read_json(FILES["portfolio_filter_stability"], {})
    health = read_json(FILES["health"], {})
    config = read_json(FILES["config"], {})
    shadow_decision_gate = read_json(FILES["shadow_decision"], {})

    latency_summary = summarize_latency(latency)
    robustness_summary = summarize_robustness(robustness)
    session_filter_summary = summarize_session_filters(session_filters)

    shadow_signal_summary = summarize_audit(signal)
    production_summary = build_production_summary(robustness_summary, latency_summary)

    report = {
        "binary_options_assumptions": {
            "payout": PAYOUT,
            "breakeven_win_rate": round(BREAKEVEN_WR, 2),
            "meaning": "For each 1 USDT stake, win earns 0.85 USDT and loss loses 1 USDT.",
        },
        "production_config": config,
        "validated_walkforward": summarize_validation(validation),
        "walkforward_candidate_ranking": summarize_validation_ranking(validation),
        "production_summary": production_summary,
        "execution_latency": latency_summary,
        "robustness_profile": robustness_summary,
        "parallel_portfolio": summarize_parallel_portfolio(parallel_portfolio),
        "queue_execution_policy": summarize_queue_execution_policy(queue_execution_policy),
        "dual_causal_filter_search": summarize_dual_causal_search(dual_causal_filter_search),
        "dual_candidate_stability": summarize_dual_candidate_stability(dual_candidate_stability),
        "portfolio_filter_search": summarize_portfolio_filter_search(portfolio_filter_search),
        "portfolio_filter_stability": summarize_portfolio_filter_stability(portfolio_filter_stability),
        "session_filter_validation": session_filter_summary,
        "ten_min_filter_scan": summarize_10min_filter_scan(ten_min_filter_scan),
        "system_health": {
            "overall": health.get("overall"),
            "price": health.get("price"),
            "signals": health.get("signals"),
            "trade_audit": health.get("trade_audit"),
        },
        "shadow_signal_audit": shadow_signal_summary,
        "live_backtest_gap": summarize_live_backtest_gap(live_backtest_gap),
        "live_shadow_gate": shadow_decision_gate,
        "shadow_candidate_decision": evaluate_shadow_candidates(
            (shadow_signal_summary.get("shadow_candidates") or {}),
            production_summary,
        ),
        "live_execution_audit": summarize_audit(live),
        "live_readiness": live_readiness(live),
        "recommendation": [],
    }

    gate_safety = (shadow_decision_gate.get("safety") or {})
    gate_counts = shadow_decision_gate.get("summary_counts") or {}
    if gate_safety.get("verdict") == "do_not_resume_real_auto_trading":
        report["recommendation"].append(
            "HARD SAFETY GATE: do not resume real auto trading. "
            f"Shadow promotion report shows watch={gate_counts.get('watch', 0)}, "
            f"reject_live_weak={gate_counts.get('reject_live_weak', 0)}, "
            f"reject_offline_weak={gate_counts.get('reject_offline_weak', 0)}; "
            "no candidate has passed the live shadow promotion gate."
        )

    for strategy, row in report["validated_walkforward"].items():
        latency_row = report["execution_latency"].get(strategy, {})
        robust_row = report["robustness_profile"].get(strategy, {})
        production_row = report["production_summary"].get(strategy, {})
        session_row = report["session_filter_validation"].get(strategy, {})
        worst_latency_edge = latency_row.get("worst_edge_over_breakeven")
        min_block_wr = robust_row.get("min_time_block_wr")
        latency_ok = worst_latency_edge is None or worst_latency_edge > 0
        if row["edge_over_breakeven"] > 2 and row.get("positive_blocks") == 4 and latency_ok:
            report["recommendation"].append(
                f"{strategy}: keep current candidate; validated edge {row['edge_over_breakeven']}pp over breakeven across 4/4 blocks, latency worst edge {worst_latency_edge}pp, 10-block min WR {min_block_wr}%."
            )
        elif not latency_ok:
            report["recommendation"].append(
                f"{strategy}: pause or reduce stake; execution-latency test falls below breakeven."
            )
        else:
            report["recommendation"].append(
                f"{strategy}: do not increase stake; validation edge or block stability is weak."
            )
        if session_row.get("best_policy") and session_row.get("best_policy") != "all_hours":
            report["recommendation"].append(
                f"{strategy}: session filter candidate {session_row['best_policy']} improves OOS WR to {session_row['win_rate']}% with {session_row['trade_retention_pct']}% trade retention."
            )
        if production_row:
            report["recommendation"].append(
                f"{strategy}: current production profile WR {production_row['win_rate']}% over {production_row['trades']} trades ({production_row.get('trades_per_day')} trades/day); worst latency edge {production_row['worst_latency_edge_over_breakeven']}pp."
            )

    trade_audit = ((report.get("system_health") or {}).get("trade_audit") or {})
    if trade_audit.get("readiness") == "autojs_loader_error":
        report["recommendation"].append(
            "AutoJS loader reached the server but failed before autojs_start; inspect the tablet AutoJS log for autojs_loader_error, then retry http://192.168.0.105:3000/auto_btc_loader.js."
        )
    elif trade_audit.get("readiness") == "loader_seen_waiting_for_autojs":
        report["recommendation"].append(
            "AutoJS loader reached the server; wait for autojs_start, or run http://192.168.0.105:3000/auto_btc.js directly if the loader does not continue."
        )
    elif trade_audit.get("latest_tablet_page_ping_age_ms") is None and report["live_readiness"] == "waiting_for_autojs_events":
        report["recommendation"].append(
            "No tablet page ping has reached the server yet; first open http://192.168.0.105:3000/tablet.html on the tablet to confirm network access."
        )
    if report["live_readiness"] == "waiting_for_first_real_order":
        report["recommendation"].append(
            "Live execution edge is not proven yet; wait for AutoJS order_done events, then compare live vs shadow signal results."
        )
    elif report["live_readiness"] == "waiting_for_autojs_events":
        report["recommendation"].append(
            "No AutoJS audit events have reached the server yet; confirm the tablet is running the latest auto_btc.js and can POST /api/trade-audit."
        )
    elif report["live_readiness"] == "autojs_seen_waiting_for_order_done":
        report["recommendation"].append(
            "AutoJS audit events are reaching the server, but no order_done has been recorded yet; inspect execution_funnel abort/skipped reasons."
        )
    elif report["live_readiness"] == "collect_more_settled_trades":
        report["recommendation"].append(
            "Live trades exist but sample is small; keep fixed 5U until at least 50 settled live trades."
        )
    live_queue = ((report.get("live_execution_audit") or {}).get("queue_execution") or {})
    second_delay = live_queue.get("second_order_delay_sec") or {}
    if live_queue.get("multi_order_batches"):
        report["recommendation"].append(
            f"Live queue audit: {live_queue.get('multi_order_batches')} multi-order batches seen; "
            f"second-order delay p50={second_delay.get('p50')}s p90={second_delay.get('p90')}s max={second_delay.get('max')}s."
        )
    elif live_queue:
        report["recommendation"].append(
            "Live queue audit: no multi-order batch has been observed yet; keep collecting AutoJS order_attempt/order_done events to verify queue-delay assumptions."
        )

    live_gap = report.get("live_backtest_gap") or {}
    if live_gap.get("status") == "ready":
        overall_gap = (live_gap.get("overall") or {}).get("wr_gap_live_minus_offline_pp")
        overall_live = ((live_gap.get("overall") or {}).get("live") or {})
        overall_offline = ((live_gap.get("overall") or {}).get("offline") or {})
        report["recommendation"].append(
            f"Live/backtest gap audit: live signal WR {overall_live.get('wr')}% over {overall_live.get('trades')} trades "
            f"vs walk-forward WR {overall_offline.get('wr')}%; gap {overall_gap}pp. Treat this as diagnostic only until live sample reaches 50+ settled signals."
        )
        for strategy, row in (live_gap.get("strategies") or {}).items():
            live_m = row.get("live") or {}
            offline_m = row.get("offline") or {}
            gap = row.get("wr_gap_live_minus_offline_pp")
            repeat = row.get("repeated_exposure") or {}
            strong = row.get("strong_countertrend") or {}
            strong_live = strong.get("live") or {}
            strong_offline = strong.get("offline") or {}
            if live_m.get("trades") and gap is not None and gap <= -5:
                report["recommendation"].append(
                    f"{strategy}: live/backtest drift is material: live WR {live_m.get('wr')}% over {live_m.get('trades')} trades "
                    f"vs offline WR {offline_m.get('wr')}%, gap {gap}pp. Do not increase stake or resume real auto trading from this state."
                )
            if strong_live.get("trades"):
                report["recommendation"].append(
                    f"{strategy}: current live signals concentrate in strong countertrend "
                    f"({strong_live.get('trades')} trades, WR {strong_live.get('wr')}%) while offline strong-countertrend WR is {strong_offline.get('wr')}%; keep collecting shadow data for this regime."
                )
            if float(repeat.get("repeat_rate_pct") or 0) >= 50:
                report["recommendation"].append(
                    f"{strategy}: repeated same-direction exposure is high ({repeat.get('repeat_rate_pct')}% within one duration, WR {(repeat.get('repeat_metrics') or {}).get('wr')}%); "
                    "test same-strategy non-overlap/cooldown in shadow before allowing repeated real entries."
                )
        policies = live_gap.get("policy_candidates") or []
        useful_policy_candidates = [
            p for p in policies
            if p.get("name") != "baseline_all_signals"
            and float(p.get("offline_wr_delta_pp") or 0) > 0
            and float(p.get("offline_retention_pct") or 0) >= 80
            and (((p.get("offline_block_summary") or {}).get("min_block_wr") or 0) >= 52)
        ]
        useful_policy_candidates.sort(
            key=lambda p: (
                float(p.get("offline_wr_delta_pp") or 0),
                float(p.get("offline_retention_pct") or 0),
            ),
            reverse=True,
        )
        if useful_policy_candidates:
            best_policy = useful_policy_candidates[0]
            live_m = best_policy.get("live") or {}
            offline_m = best_policy.get("offline") or {}
            report["recommendation"].append(
                f"Best diagnostic policy candidate: {best_policy.get('id')} keeps {best_policy.get('offline_retention_pct')}% offline trades, "
                f"offline WR {offline_m.get('wr')}% ({best_policy.get('offline_wr_delta_pp')}pp), "
                f"live replay WR {live_m.get('wr')}% over {live_m.get('trades')} trades. Keep as shadow/replay only."
            )
        ten_repeat = next(
            (p for p in policies if p.get("id") == "POLICY_10m_same_direction_gap_1x_duration"),
            None,
        )
        if ten_repeat:
            report["recommendation"].append(
                "10m repeat-control replay is not a clear fix yet: "
                f"offline WR delta {ten_repeat.get('offline_wr_delta_pp')}pp with {ten_repeat.get('offline_retention_pct')}% retention, "
                f"live WR delta {ten_repeat.get('live_wr_delta_pp')}pp on a small sample."
            )

    portfolio = report.get("parallel_portfolio") or {}
    if portfolio.get("status") == "ready":
        overlap = portfolio.get("overlap") or {}
        report["recommendation"].append(
            f"Parallel 10m+30m profile: combined WR {portfolio.get('win_rate')}% over {portfolio.get('trades')} trades "
            f"({(portfolio.get('frequency') or {}).get('trades_per_day')} trades/day), max loss {portfolio.get('max_loss')}; "
            f"same-candle groups {overlap.get('same_candle_signal_groups')}, direction conflicts {overlap.get('direction_conflict_groups')}."
        )

    queue_policy = report.get("queue_execution_policy") or {}
    if queue_policy.get("status") == "ready":
        best = queue_policy.get("best_policy") or {}
        best_high_freq = queue_policy.get("best_high_frequency_policy") or {}
        baseline = (queue_policy.get("results") or {}).get(queue_policy.get("baseline_policy")) or {}
        report["recommendation"].append(
            f"Queue execution policy with 1m conservative second-order delay: best={best.get('policy')} "
            f"WR {best.get('wr')}% over {best.get('trades')} trades ({best.get('trades_per_day')} trades/day), "
            f"delta vs current queue {best.get('wr_delta_vs_baseline')}pp / {best.get('trade_delta_vs_baseline')} trades; "
            f"current queue WR {baseline.get('win_rate')}% over {baseline.get('trades')} trades."
        )
        if best_high_freq and best_high_freq.get("policy") != best.get("policy"):
            report["recommendation"].append(
                f"Queue high-frequency balanced policy: {best_high_freq.get('policy')} keeps {best_high_freq.get('trades')} trades "
                f"({best_high_freq.get('trades_per_day')} trades/day) and improves queue WR by "
                f"{best_high_freq.get('wr_delta_vs_baseline')}pp; this better matches the high-trade-count target than {best.get('policy')}."
            )
        stability = queue_policy.get("stability") or {}
        confidence_stability = stability.get("both_confidence_desc") or {}
        if confidence_stability:
            report["recommendation"].append(
                "Queue production default: both_confidence_desc keeps all trades and is stable enough for production "
                f"({confidence_stability.get('improved_blocks')}/10 blocks improved, "
                f"{confidence_stability.get('worsened_blocks')}/10 worsened, min candidate WR {confidence_stability.get('min_candidate_wr')}%)."
            )
        conflict_confidence_stability = stability.get("skip_direction_conflicts_confidence_desc") or {}
        if conflict_confidence_stability:
            report["recommendation"].append(
                "Queue optional WR-first mode: skip_direction_conflicts_confidence_desc improves "
                f"{conflict_confidence_stability.get('improved_blocks')}/10 blocks but removes conflict trades; keep it configurable until live audit confirms."
            )
        delay_summary = queue_policy.get("delay_sensitivity_summary") or {}
        confidence_delay = delay_summary.get("both_confidence_desc") or {}
        conflict_delay = delay_summary.get("skip_direction_conflicts_confidence_desc") or {}
        if confidence_delay:
            report["recommendation"].append(
                "Queue delay stress: both_confidence_desc keeps all trades; across 0/1/2/3/5m second-order delays "
                f"min WR is {confidence_delay.get('min_wr')}% and worst delta vs 30m-first is {confidence_delay.get('min_wr_delta_vs_baseline')}pp."
            )
        if conflict_delay:
            report["recommendation"].append(
                "Queue delay stress optional mode: skip_direction_conflicts_confidence_desc has "
                f"min WR {conflict_delay.get('min_wr')}% and worst delta {conflict_delay.get('min_wr_delta_vs_baseline')}pp while removing 10 conflict trades."
            )

    dual_stability = report.get("dual_candidate_stability") or {}
    if dual_stability.get("status") == "production_candidate":
        combined = dual_stability.get("combined") or {}
        btc10 = (dual_stability.get("per_strategy") or {}).get("BTC_10min") or {}
        report["recommendation"].append(
            f"Dual causal filter candidate is production-ready: combined WR {combined.get('candidate_win_rate')}% "
            f"over {combined.get('candidate_trades')} trades ({combined.get('candidate_trades_per_day')} trades/day), "
            f"10m improves from {btc10.get('current_win_rate')}%/{btc10.get('current_trades')} trades to "
            f"{btc10.get('candidate_win_rate')}%/{btc10.get('candidate_trades')} trades; "
            f"block audit improved {combined.get('improved_blocks')}/10 and has "
            f"{combined.get('non_positive_candidate_blocks')} non-positive blocks."
        )
    elif dual_stability.get("status"):
        report["recommendation"].append(
            f"Dual causal filter candidate is {dual_stability.get('status')}; keep current production until block stability improves."
        )

    filter_search = report.get("portfolio_filter_search") or {}
    filter_stability = report.get("portfolio_filter_stability") or {}
    if filter_search.get("status") == "candidate_found":
        search_cand = filter_search.get("best_candidate") or {}
        stable_cand = filter_stability.get("candidate") or {}
        cand = stable_cand if stable_cand.get("name") else search_cand
        base = filter_search.get("baseline_validation") or {}
        if filter_stability.get("status") == "production_candidate":
            report["recommendation"].append(
                f"Portfolio filter candidate {cand.get('name')}: stable across block audit. "
                f"Stability audit improved {filter_stability.get('improved_blocks')}/10 blocks, min WR {filter_stability.get('min_candidate_wr')}%, "
                "so it is an offline production candidate; still require live AutoJS audit before raising stake."
            )
        else:
            report["recommendation"].append(
                f"Portfolio filter search best {search_cand.get('name')}: validation WR {search_cand.get('validation_win_rate')}% vs baseline {base.get('win_rate')}%. "
                f"Best audited candidate is {cand.get('name')}; keep as shadow/proposed only because stability status is {filter_stability.get('status') or 'unknown'}."
            )
    elif filter_search.get("status") == "baseline_best":
        report["recommendation"].append(
            "Portfolio-level filter search did not beat the current parallel baseline on holdout validation."
        )

    shadow_decision = report.get("shadow_candidate_decision") or {}
    shadow_candidates = shadow_decision.get("candidates") or []
    if not shadow_candidates:
        report["recommendation"].append(
            "Shadow 10m candidates are being tracked, but none has produced settled tradeable signals yet; keep production unchanged."
        )
    else:
        promotable = [r for r in shadow_candidates if r.get("status") == "promote_candidate"]
        if promotable:
            best = max(promotable, key=lambda r: float(r.get("wr_gain_vs_10min_prod") or 0))
            report["recommendation"].append(
                f"Shadow candidate {best['strategy_id']} passed promotion gates with WR gain {best['wr_gain_vs_10min_prod']}pp; run fresh walk-forward validation before changing production."
            )
        else:
            pending = [r for r in shadow_candidates if r.get("status") in ("collect_more", "readable_not_promotable")]
            if pending:
                report["recommendation"].append(
                    "Shadow 10m candidates have not reached promotion gates yet; continue collecting samples without changing production."
                )
            else:
                report["recommendation"].append(
                    "Shadow 10m candidates failed promotion gates; keep production unchanged."
                )

    if health.get("overall") in ("warn", "fail"):
        report["recommendation"].append(
            f"Operational health is {health.get('overall')}; fix stale price/signals before trusting live performance."
        )
    scan_summary = report.get("ten_min_filter_scan") or {}
    usable = scan_summary.get("usable_60pct_min100") or []
    ranking_10 = report.get("walkforward_candidate_ranking", {}).get("BTC_10min", [])
    strict_by_name = {r.get("name"): r for r in ranking_10}
    strict_recent = [
        strict_by_name[name] for name in (
            "recent_scan_th65_rsi40_60_all3",
            "recent_scan_th65_rsi35_65_all3",
        )
        if name in strict_by_name
    ]
    if strict_recent:
        best_recent = max(strict_recent, key=lambda r: float(r.get("win_rate") or 0))
        report["recommendation"].append(
            f"BTC_10min: recent-scan high-WR candidates failed strict walk-forward confirmation; best recent-scan validation is {best_recent.get('name')} WR {best_recent.get('win_rate')}% over {best_recent.get('trades')} trades, so do not switch production to it."
        )
    elif not usable:
        report["recommendation"].append(
            "BTC_10min: recent high-threshold scan found high-WR small-sample filters, but no >=60% / >=100-trade candidate; keep production config and monitor shadow results."
        )
    else:
        best = usable[0]
        report["recommendation"].append(
            f"BTC_10min: recent scan usable candidate {best.get('label')} WR {best.get('wr')}% over {best.get('trades')} trades; keep as shadow candidate until walk-forward/session validation confirms it."
        )

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Saved {REPORT_FILE}")


if __name__ == "__main__":
    main()
