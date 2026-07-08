from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_normal_state_v1 as v1
import research_normal_state_v3 as v3
import research_normal_state_v4 as v4
import research_normal_state_v6 as v6
import research_normal_state_v7_confirm_reentry as v7


OUT_JSON = ROOT / "tmp" / "normal_state_v8_walkforward_veto.json"
OUT_TRADES = ROOT / "tmp" / "normal_state_v8_walkforward_veto_trades.csv"
OUT_RULES = ROOT / "tmp" / "normal_state_v8_walkforward_veto_rules.csv"

HORIZON_SEC = 600
WIN_PAY = 0.8
LOSS_PAY = -1.0
BREAKEVEN_WR = abs(LOSS_PAY) / (WIN_PAY + abs(LOSS_PAY)) * 100.0


def payout(won: bool) -> float:
    return WIN_PAY if won else LOSS_PAY


def max_drawdown(rows: list[dict]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for row in rows:
        equity += payout(bool(row["won"]))
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return round(worst, 4)


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {
            "n": 0,
            "wr": 0.0,
            "pnl": 0.0,
            "ev": 0.0,
            "max_dd": 0.0,
            "days": [],
            "modules": {},
        }
    wons = [bool(r["won"]) for r in rows]
    pnl = sum(payout(w) for w in wons)
    df = pd.DataFrame(rows)
    days = []
    for day, g in df.groupby("day_cn", sort=True):
        gw = [bool(x) for x in g["won"].tolist()]
        gpnl = sum(payout(w) for w in gw)
        days.append(
            {
                "day": day,
                "n": int(len(g)),
                "wr": round(sum(gw) / len(gw) * 100.0, 2),
                "pnl": round(gpnl, 4),
                "max_dd": max_drawdown(g.to_dict("records")),
            }
        )
    modules = {}
    if "strategy_key" in df.columns:
        group_col = "strategy_key"
    elif "module" in df.columns:
        group_col = "module"
    else:
        group_col = ""
    if group_col:
        for module, g in df.groupby(group_col, sort=True):
            gw = [bool(x) for x in g["won"].tolist()]
            modules[str(module)] = {
                "n": int(len(g)),
                "wr": round(sum(gw) / len(gw) * 100.0, 2),
                "pnl": round(sum(payout(w) for w in gw), 4),
            }
    return {
        "n": int(len(rows)),
        "wr": round(sum(wons) / len(wons) * 100.0, 2),
        "pnl": round(pnl, 4),
        "ev": round(pnl / len(rows), 5),
        "max_dd": max_drawdown(rows),
        "days": days,
        "modules": modules,
    }


def split_report(rows: list[dict]) -> dict:
    return {
        "summary": summarize(rows),
        "train_to_0630": summarize([r for r in rows if r["day_cn"] <= "2026-06-30"]),
        "recent_0701_plus": summarize([r for r in rows if r["day_cn"] >= "2026-07-01"]),
        "d0701": summarize([r for r in rows if r["day_cn"] == "2026-07-01"]),
        "d0702": summarize([r for r in rows if r["day_cn"] == "2026-07-02"]),
        "d0703": summarize([r for r in rows if r["day_cn"] == "2026-07-03"]),
    }


def trend_flow_continuation_veto(row: dict) -> bool:
    side = v6.finite(row.get("breakout_side"))
    side_slope = side * v6.finite(row.get("m_slope60_bps"))
    side_flow = side * v6.finite(row.get("flow60"))
    return np.isfinite(side_slope) and np.isfinite(side_flow) and side_slope > 70.0 and side_flow > 0.0


def apply_veto(rows: list[dict], veto_mode: str) -> tuple[list[dict], dict]:
    kept: list[dict] = []
    skipped = {"trend_flow_continuation": 0}
    for row in rows:
        if veto_mode == "none":
            kept.append(row)
            continue
        if veto_mode == "trend_flow_veto" and trend_flow_continuation_veto(row):
            skipped["trend_flow_continuation"] += 1
            continue
        kept.append(row)
    return kept, {"veto_mode": veto_mode, "skipped": skipped}


def selected_specs() -> list[v6.RuleSpec]:
    wanted = {
        "V6_BASE_UPPER_QUALITY",
        "V6_CONSENSUS_2OF5_UPPER",
        "V6_CONSENSUS_3OF5_UPPER",
        "V6_FRESH_TAIL_CAP",
        "V6_SLOPE_WIDTH_REJECT",
    }
    return [spec for spec in v6.rule_specs() if spec.name in wanted]


def apply_rule(base_rows: list[dict], spec: v6.RuleSpec) -> list[dict]:
    out: list[dict] = []
    for row in base_rows:
        ok, why = v6.rule_allows(row, spec)
        if not ok:
            continue
        item = dict(row)
        votes_n, votes = v6.consensus_votes(row)
        item["source_rule"] = spec.name
        item["regime"] = "walkforward_confirmed_false_break_reversion"
        item["reason"] = spec.description
        item["consensus_votes"] = votes_n
        item["consensus_vote_names"] = ",".join(votes)
        item["rule_filter_detail"] = why
        out.append(item)
    return out


def build_strategy_rows(
    bars: pd.DataFrame,
    base_rows: list[dict],
) -> tuple[dict[str, list[dict]], list[dict]]:
    rows_by_key: dict[str, list[dict]] = {}
    meta_rows: list[dict] = []
    delay_grid = [5, 15]
    adverse_grid = [0.0, 1.0, 2.0, 5.0]
    veto_modes = ["none", "trend_flow_veto"]
    for spec in selected_specs():
        rule_rows = apply_rule(base_rows, spec)
        for veto_mode in veto_modes:
            vetoed, veto_meta = apply_veto(rule_rows, veto_mode)
            for delay_sec in delay_grid:
                for max_adverse_bps in adverse_grid:
                    confirmed, confirm_meta = v7.apply_confirmation(
                        vetoed,
                        bars,
                        delay_sec=delay_sec,
                        max_adverse_bps=max_adverse_bps,
                    )
                    key = f"D{delay_sec}_A{max_adverse_bps:g}_{spec.name}_{veto_mode}"
                    for row in confirmed:
                        row["strategy_key"] = key
                        row["module"] = key
                        row["veto_mode"] = veto_mode
                        row["source_rule"] = spec.name
                    rows_by_key[key] = confirmed
                    report = split_report(confirmed)
                    fit = fitting_check(report)
                    meta_rows.append(
                        {
                            "strategy_key": key,
                            "source_rule": spec.name,
                            "delay_sec": delay_sec,
                            "max_adverse_bps": max_adverse_bps,
                            "veto_mode": veto_mode,
                            "candidate_n_before_veto": len(rule_rows),
                            "candidate_n_after_veto": len(vetoed),
                            "veto_skipped": veto_meta["skipped"].get("trend_flow_continuation", 0),
                            "confirm_rejected_adverse": confirm_meta["rejected"]["adverse_confirmation"],
                            "confirm_cooldown_skipped": confirm_meta["cooldown_skipped"],
                            "n": report["summary"]["n"],
                            "wr": report["summary"]["wr"],
                            "pnl": report["summary"]["pnl"],
                            "max_dd": report["summary"]["max_dd"],
                            "train_n": report["train_to_0630"]["n"],
                            "train_wr": report["train_to_0630"]["wr"],
                            "train_pnl": report["train_to_0630"]["pnl"],
                            "recent_n": report["recent_0701_plus"]["n"],
                            "recent_wr": report["recent_0701_plus"]["wr"],
                            "recent_pnl": report["recent_0701_plus"]["pnl"],
                            "survivor": fit["survivor"],
                            "fit_risk": fit["fit_risk"],
                            "active_days": fit["active_days"],
                            "losing_days": fit["losing_days"],
                            "worst_day_pnl": fit["worst_day_pnl"],
                        }
                    )
    return rows_by_key, meta_rows


def fitting_check(report: dict) -> dict:
    train = report["train_to_0630"]
    recent = report["recent_0701_plus"]
    summary = report["summary"]
    days = summary.get("days", [])
    losing_days = sum(1 for d in days if float(d.get("pnl", 0.0)) < 0.0)
    survivor = (
        train["n"] >= 15
        and recent["n"] >= 5
        and train["wr"] >= 60.0
        and recent["wr"] >= 60.0
        and train["pnl"] > 0.0
        and recent["pnl"] > 0.0
    )
    fit_risk = "high"
    if survivor and summary["n"] >= 25 and losing_days <= max(4, math.ceil(len(days) * 0.35)):
        fit_risk = "medium"
    if survivor and summary["n"] >= 50 and recent["n"] >= 15 and losing_days <= max(3, math.ceil(len(days) * 0.25)):
        fit_risk = "low"
    return {
        "survivor": survivor,
        "fit_risk": fit_risk,
        "active_days": len(days),
        "losing_days": losing_days,
        "worst_day_pnl": round(min((float(d.get("pnl", 0.0)) for d in days), default=0.0), 4),
    }


def strategy_stats(rows: list[dict]) -> dict:
    if not rows:
        return {
            "n": 0,
            "wr": 0.0,
            "pnl": 0.0,
            "max_dd": 0.0,
            "active_days": 0,
            "losing_days": 0,
            "score": -9999.0,
        }
    summary = summarize(rows)
    active_days = len(summary["days"])
    losing_days = sum(1 for d in summary["days"] if float(d["pnl"]) < 0.0)
    score = (
        float(summary["pnl"])
        - abs(float(summary["max_dd"])) * 0.45
        + min(int(summary["n"]), 40) * 0.04
        - losing_days * 0.25
    )
    return {
        "n": summary["n"],
        "wr": summary["wr"],
        "pnl": summary["pnl"],
        "max_dd": summary["max_dd"],
        "active_days": active_days,
        "losing_days": losing_days,
        "score": round(score, 6),
    }


def eligible_train_best(stats: dict) -> bool:
    return stats["n"] >= 15 and stats["wr"] >= 60.0 and stats["pnl"] > 0.0


def eligible_robust(stats: dict) -> bool:
    if stats["n"] < 20 or stats["wr"] < 65.0 or stats["pnl"] < 3.0:
        return False
    if stats["max_dd"] < -3.5 or stats["active_days"] < 5:
        return False
    return stats["losing_days"] <= max(3, math.ceil(stats["active_days"] * 0.35))


def first_idx_by_cn_day(bars: pd.DataFrame) -> dict[str, int]:
    cn_days = pd.Series(bars.index.tz_convert("Asia/Shanghai").strftime("%Y-%m-%d"), index=np.arange(len(bars)))
    return cn_days.groupby(cn_days).apply(lambda s: int(s.index.min())).to_dict()


def simulate_daily_walkforward(
    rows_by_key: dict[str, list[dict]],
    bars: pd.DataFrame,
    *,
    mode: str,
) -> tuple[list[dict], list[dict]]:
    day_start = first_idx_by_cn_day(bars)
    days = sorted(day_start)
    accepted: list[dict] = []
    selections: list[dict] = []

    for day in days:
        start_idx = day_start[day]
        scored: list[tuple[float, str, dict]] = []
        for key, rows in rows_by_key.items():
            history = [r for r in rows if int(r["settle_idx"]) <= start_idx]
            stats = strategy_stats(history)
            if mode == "train_best":
                if not eligible_train_best(stats):
                    continue
            elif mode == "robust":
                if not eligible_robust(stats):
                    continue
            else:
                raise ValueError(f"unknown mode: {mode}")
            scored.append((float(stats["score"]), key, stats))
        if not scored:
            selections.append({"day": day, "selected_key": "", "mode": mode, "reason": "no_eligible_strategy"})
            continue
        scored.sort(key=lambda x: (x[0], x[2]["pnl"], x[2]["n"]), reverse=True)
        _, key, stats = scored[0]
        day_rows = [dict(r) for r in rows_by_key[key] if str(r["day_cn"]) == day]
        for row in day_rows:
            row["walkforward_mode"] = mode
            row["selected_by_prior_key"] = key
            row["prior_n"] = stats["n"]
            row["prior_wr"] = stats["wr"]
            row["prior_pnl"] = stats["pnl"]
            row["prior_max_dd"] = stats["max_dd"]
            row["module"] = f"WF_{mode}"
        accepted.extend(day_rows)
        selections.append(
            {
                "day": day,
                "selected_key": key,
                "mode": mode,
                "reason": "eligible",
                **{f"prior_{k}": v for k, v in stats.items()},
                "day_trade_n": len(day_rows),
            }
        )
    accepted.sort(key=lambda r: int(r["idx"]))
    return accepted, selections


def run() -> dict:
    bars, second_sources = v3.load_merged_bars_v3()
    minute = v1.load_minute_features(bars.index)
    orderbook, orderbook_sources = v3.load_orderbook_features_v3(bars.index)
    features = pd.concat(
        [
            minute.drop(columns=["minute_source"], errors="ignore"),
            orderbook.drop(columns=["orderbook_sources"], errors="ignore"),
        ],
        axis=1,
    )
    ctx = v1.build_second_context(bars, 180 * 60)
    base_rows = v7.prepare_base_rows(bars, features, ctx)
    rows_by_key, rule_rows = build_strategy_rows(bars, base_rows)
    train_best_rows, train_best_selections = simulate_daily_walkforward(rows_by_key, bars, mode="train_best")
    robust_rows, robust_selections = simulate_daily_walkforward(rows_by_key, bars, mode="robust")

    rule_table = pd.DataFrame(rule_rows).sort_values(
        ["survivor", "recent_pnl", "train_pnl", "n"],
        ascending=[False, False, False, False],
    )
    OUT_RULES.parent.mkdir(parents=True, exist_ok=True)
    rule_table.to_csv(OUT_RULES, index=False, encoding="utf-8-sig")

    all_trades: list[dict] = []
    for rows in rows_by_key.values():
        all_trades.extend(rows)
    all_trades.extend(train_best_rows)
    all_trades.extend(robust_rows)
    pd.DataFrame(all_trades).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")

    fixed_keys = [
        "D5_A5_V6_CONSENSUS_3OF5_UPPER_none",
        "D5_A5_V6_CONSENSUS_3OF5_UPPER_trend_flow_veto",
        "D5_A5_V6_CONSENSUS_2OF5_UPPER_trend_flow_veto",
        "D5_A5_V6_FRESH_TAIL_CAP_trend_flow_veto",
    ]
    report = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "data": {
            "rows_dense": int(len(bars)),
            "rows_observed": int(bars["observed"].sum()),
            "observed_pct": round(float(bars["observed"].mean() * 100.0), 4),
            "first": bars.index.min().isoformat(),
            "last": bars.index.max().isoformat(),
            "second_sources": second_sources,
            "minute_source": minute["minute_source"].iloc[0] if "minute_source" in minute else "",
            "orderbook_sources": orderbook_sources,
        },
        "payoff": {"win": WIN_PAY, "loss": LOSS_PAY, "breakeven_wr_pct": round(BREAKEVEN_WR, 2)},
        "method": {
            "base": "V7 confirmed upper false-break reversion.",
            "confirmation": "Wait delay_sec after signal and enter only if adverse move is within max_adverse_bps.",
            "continuation_veto": "Reject upper fade when breakout-side 60m slope > 70 bps and 60s taker flow supports the breakout.",
            "walkforward": "At each CN day start, select a strategy using only trades that settled before that day.",
        },
        "base_counts": {"base_upper_quality": len(base_rows), "strategy_variants": len(rows_by_key)},
        "top_rules": rule_table.head(20).to_dict("records"),
        "fixed_reports": {key: split_report(rows_by_key.get(key, [])) for key in fixed_keys},
        "walkforward": {
            "train_best": {
                "report": split_report(train_best_rows),
                "fit_check": fitting_check(split_report(train_best_rows)),
                "selections": train_best_selections,
            },
            "robust": {
                "report": split_report(robust_rows),
                "fit_check": fitting_check(split_report(robust_rows)),
                "selections": robust_selections,
            },
        },
        "outputs": {"json": str(OUT_JSON), "trades_csv": str(OUT_TRADES), "rule_report_csv": str(OUT_RULES)},
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    result = run()
    compact_fixed = {
        key: result["fixed_reports"][key]["summary"]
        for key in result["fixed_reports"]
    }
    print(
        json.dumps(
            {
                "data": {k: result["data"][k] for k in ("rows_dense", "rows_observed", "observed_pct", "first", "last")},
                "base_counts": result["base_counts"],
                "top_rules": result["top_rules"][:12],
                "fixed": compact_fixed,
                "walkforward_train_best": result["walkforward"]["train_best"]["report"]["summary"],
                "walkforward_robust": result["walkforward"]["robust"]["report"]["summary"],
                "outputs": result["outputs"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
