from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
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


OUT_JSON = ROOT / "tmp" / "normal_state_v9_state_gate.json"
OUT_TRADES = ROOT / "tmp" / "normal_state_v9_state_gate_trades.csv"
OUT_RULES = ROOT / "tmp" / "normal_state_v9_state_gate_rules.csv"

HORIZON_SEC = 600
WIN_PAY = 0.8
LOSS_PAY = -1.0
BREAKEVEN_WR = abs(LOSS_PAY) / (WIN_PAY + abs(LOSS_PAY)) * 100.0


@dataclass(frozen=True)
class StateGate:
    name: str
    description: str


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
        return {"n": 0, "wr": 0.0, "pnl": 0.0, "ev": 0.0, "max_dd": 0.0, "days": [], "modules": {}}
    wons = [bool(r["won"]) for r in rows]
    pnl = sum(payout(w) for w in wons)
    df = pd.DataFrame(rows)
    days = []
    for day, g in df.groupby("day_cn", sort=True):
        gw = [bool(x) for x in g["won"].tolist()]
        gpnl = sum(payout(w) for w in gw)
        days.append({"day": day, "n": int(len(g)), "wr": round(sum(gw) / len(gw) * 100.0, 2), "pnl": round(gpnl, 4), "max_dd": max_drawdown(g.to_dict("records"))})
    modules = {}
    group_col = "strategy_key" if "strategy_key" in df.columns else "module" if "module" in df.columns else ""
    if group_col:
        for module, g in df.groupby(group_col, sort=True):
            gw = [bool(x) for x in g["won"].tolist()]
            modules[str(module)] = {"n": int(len(g)), "wr": round(sum(gw) / len(gw) * 100.0, 2), "pnl": round(sum(payout(w) for w in gw), 4)}
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


def state_gates() -> list[StateGate]:
    return [
        StateGate("none", "No extra state gate; reference confirmed false-break reversion."),
        StateGate("edge_persistence_lt6", "Avoid mean reversion when the 1m z-score has walked near the band for 6 of the last 10 minutes."),
        StateGate("avoid_slow_persistent_edge", "Avoid persistent-edge fades only when half-life is still slow for a 10-minute option: bandwalk>=6 and half-life>8m."),
        StateGate("avoid_lowvol_slow_edge", "Avoid low-volatility persistent edge: sigma10<18, bandwalk>=5, half-life>8m."),
    ]


def gate_allows(row: dict, gate: StateGate) -> tuple[bool, str]:
    bandwalk = v6.finite(row.get("m_bandwalk10"))
    half_life = v6.finite(row.get("m_half_life_min"))
    sigma10 = v6.finite(row.get("sigma10_bps"))
    if gate.name == "none":
        return True, "pass"
    if gate.name == "edge_persistence_lt6":
        ok = np.isfinite(bandwalk) and bandwalk < 6.0
        return ok, "bandwalk_lt6" if ok else "persistent_edge"
    if gate.name == "avoid_slow_persistent_edge":
        bad = np.isfinite(bandwalk) and np.isfinite(half_life) and bandwalk >= 6.0 and half_life > 8.0
        return not bad, "pass" if not bad else "slow_persistent_edge"
    if gate.name == "avoid_lowvol_slow_edge":
        bad = (
            np.isfinite(sigma10)
            and np.isfinite(bandwalk)
            and np.isfinite(half_life)
            and sigma10 < 18.0
            and bandwalk >= 5.0
            and half_life > 8.0
        )
        return not bad, "pass" if not bad else "lowvol_slow_edge"
    raise ValueError(f"unknown gate: {gate.name}")


def selected_specs() -> list[v6.RuleSpec]:
    wanted = {
        "V6_CONSENSUS_3OF5_UPPER",
        "V6_CONSENSUS_2OF5_UPPER",
        "V6_FRESH_TAIL_CAP",
        "V6_SLOPE_WIDTH_REJECT",
    }
    return [spec for spec in v6.rule_specs() if spec.name in wanted]


def apply_rule_and_gate(base_rows: list[dict], spec: v6.RuleSpec, gate: StateGate) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    skipped = {"rule": 0, "state_gate": 0}
    for row in base_rows:
        ok, why = v6.rule_allows(row, spec)
        if not ok:
            skipped["rule"] += 1
            continue
        state_ok, state_reason = gate_allows(row, gate)
        if not state_ok:
            skipped["state_gate"] += 1
            continue
        item = dict(row)
        votes_n, votes = v6.consensus_votes(row)
        item["source_rule"] = spec.name
        item["state_gate"] = gate.name
        item["state_gate_reason"] = state_reason
        item["regime"] = "state_gated_confirmed_false_break_reversion"
        item["reason"] = f"{spec.description}; {gate.description}; confirmed D5/A5 entry"
        item["consensus_votes"] = votes_n
        item["consensus_vote_names"] = ",".join(votes)
        item["rule_filter_detail"] = why
        rows.append(item)
    return rows, skipped


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


def apply_daily_settled_stop(rows: list[dict], *, daily_stop_u: float = -2.0, loss_streak_limit: int = 2) -> tuple[list[dict], dict]:
    accepted: list[dict] = []
    pending: list[dict] = []
    settled_ptr = 0
    halted_days: set[str] = set()
    day_pnl: dict[str, float] = {}
    day_loss_streak: dict[str, int] = {}
    skipped = {"daily_stop": 0, "loss_streak": 0}

    def settle_until(idx: int) -> None:
        nonlocal settled_ptr
        pending.sort(key=lambda r: int(r["settle_idx"]))
        while settled_ptr < len(pending) and int(pending[settled_ptr]["settle_idx"]) <= idx:
            row = pending[settled_ptr]
            settled_ptr += 1
            day = str(row["day_cn"])
            won = bool(row["won"])
            day_pnl[day] = day_pnl.get(day, 0.0) + payout(won)
            if won:
                day_loss_streak[day] = 0
            else:
                day_loss_streak[day] = day_loss_streak.get(day, 0) + 1
            if day_pnl[day] <= daily_stop_u:
                halted_days.add(day)
            if day_loss_streak[day] >= loss_streak_limit:
                halted_days.add(day)

    for row in sorted(rows, key=lambda r: int(r["idx"])):
        idx = int(row["idx"])
        settle_until(idx)
        day = str(row["day_cn"])
        if day in halted_days:
            if day_pnl.get(day, 0.0) <= daily_stop_u:
                skipped["daily_stop"] += 1
            else:
                skipped["loss_streak"] += 1
            continue
        out = dict(row)
        out["risk_gate"] = "daily_settled_stop"
        out["day_pnl_before"] = round(day_pnl.get(day, 0.0), 4)
        out["day_loss_streak_before"] = int(day_loss_streak.get(day, 0))
        accepted.append(out)
        pending.append(out)
    return accepted, {
        "params": {"daily_stop_u": daily_stop_u, "loss_streak_limit": loss_streak_limit, "uses_only_settled_trades": True},
        "skipped": skipped,
    }


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

    meta_rows: list[dict] = []
    all_trades: list[dict] = []
    details: dict[str, dict] = {}

    for spec in selected_specs():
        for gate in state_gates():
            candidates, skipped = apply_rule_and_gate(base_rows, spec, gate)
            confirmed, confirm_meta = v7.apply_confirmation(candidates, bars, delay_sec=5, max_adverse_bps=5.0)
            strategy_key = f"D5_A5_{spec.name}_{gate.name}"
            for row in confirmed:
                row["strategy_key"] = strategy_key
                row["module"] = strategy_key
            stopped, stop_meta = apply_daily_settled_stop(confirmed)
            for row in stopped:
                row["strategy_key"] = f"{strategy_key}_daily_stop"
                row["module"] = row["strategy_key"]
            report = split_report(confirmed)
            stop_report = split_report(stopped)
            fit = fitting_check(report)
            meta_rows.append(
                {
                    "strategy_key": strategy_key,
                    "source_rule": spec.name,
                    "state_gate": gate.name,
                    "state_gate_description": gate.description,
                    "candidate_n_before_rule_gate": len(base_rows),
                    "candidate_n_after_rule_state": len(candidates),
                    "skipped_rule": skipped["rule"],
                    "skipped_state_gate": skipped["state_gate"],
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
                    "daily_stop_n": stop_report["summary"]["n"],
                    "daily_stop_wr": stop_report["summary"]["wr"],
                    "daily_stop_pnl": stop_report["summary"]["pnl"],
                    "daily_stop_max_dd": stop_report["summary"]["max_dd"],
                    "survivor": fit["survivor"],
                    "fit_risk": fit["fit_risk"],
                    "active_days": fit["active_days"],
                    "losing_days": fit["losing_days"],
                    "worst_day_pnl": fit["worst_day_pnl"],
                }
            )
            details[strategy_key] = {
                "report": report,
                "daily_stop_report": stop_report,
                "fit_check": fit,
                "rule_state_skipped": skipped,
                "confirm_meta": confirm_meta,
                "daily_stop_meta": stop_meta,
            }
            all_trades.extend(confirmed)
            all_trades.extend(stopped)

    table = pd.DataFrame(meta_rows).sort_values(
        ["survivor", "recent_pnl", "train_pnl", "n"],
        ascending=[False, False, False, False],
    )
    OUT_RULES.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(OUT_RULES, index=False, encoding="utf-8-sig")
    pd.DataFrame(all_trades).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")

    recommended_key = "D5_A5_V6_CONSENSUS_3OF5_UPPER_avoid_slow_persistent_edge"
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
            "orderbook_available_at_base_pct": round(float(pd.DataFrame(base_rows)["ob_available"].mean() * 100.0), 4) if base_rows else 0.0,
        },
        "payoff": {"win": WIN_PAY, "loss": LOSS_PAY, "breakeven_wr_pct": round(BREAKEVEN_WR, 2)},
        "method": {
            "entry": "D5/A5: wait 5 seconds after upper false-break signal; enter only if adverse move <=5 bps; expiry is 10 minutes after delayed entry.",
            "state_gate_intent": "Do not dynamically chase best parameters. Keep the rule fixed and dynamically skip market states where mean reversion is slow or price is walking the band.",
            "anti_overfit": [
                "State gates are simple market-state rules based on bandwalk and half-life, not recent-day optimization.",
                "Train <= 2026-06-30 and recent >= 2026-07-01 are reported separately.",
                "Daily stop uses only already settled trades.",
                "Fit risk remains medium/high until fresh shadow data adds at least 30-50 new trades.",
            ],
        },
        "top_rules": table.head(20).to_dict("records"),
        "recommended": {
            "strategy_key": recommended_key,
            **details.get(recommended_key, {}),
        },
        "details": details,
        "outputs": {"json": str(OUT_JSON), "trades_csv": str(OUT_TRADES), "rule_report_csv": str(OUT_RULES)},
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    result = run()
    print(
        json.dumps(
            {
                "data": {k: result["data"][k] for k in ("rows_dense", "rows_observed", "observed_pct", "first", "last", "orderbook_available_at_base_pct")},
                "top_rules": result["top_rules"][:12],
                "recommended": {
                    "strategy_key": result["recommended"]["strategy_key"],
                    "report": result["recommended"].get("report", {}).get("summary", {}),
                    "train": result["recommended"].get("report", {}).get("train_to_0630", {}),
                    "recent": result["recommended"].get("report", {}).get("recent_0701_plus", {}),
                    "fit_check": result["recommended"].get("fit_check", {}),
                },
                "outputs": result["outputs"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
