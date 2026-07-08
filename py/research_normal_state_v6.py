from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_normal_state_v1 as v1
import research_normal_state_v3 as v3
import research_normal_state_v4 as v4


OUT_JSON = ROOT / "tmp" / "normal_state_v6_focused_reversion.json"
OUT_TRADES = ROOT / "tmp" / "normal_state_v6_focused_reversion_trades.csv"
OUT_RULES = ROOT / "tmp" / "normal_state_v6_rule_report.csv"

HORIZON_SEC = 600
WIN_PAY = 0.8
LOSS_PAY = -1.0
BREAKEVEN_WR = abs(LOSS_PAY) / (WIN_PAY + abs(LOSS_PAY)) * 100.0


@dataclass(frozen=True)
class RuleSpec:
    name: str
    side_mode: str
    priority: int
    max_outside_sec: int | None = None
    max_peak_abs_z: float | None = None
    max_side_slope_bps: float | None = None
    max_width_ratio: float | None = None
    min_width_ratio: float | None = None
    max_side_flow: float | None = None
    max_inside_abs_z: float | None = None
    max_half_life_min: float | None = None
    max_bandwalk10: float | None = None
    require_ob_not_continuation: bool = False
    min_consensus_votes: int = 0
    description: str = ""


def finite(value: object) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return out if np.isfinite(out) else float("nan")


def payout(won: bool) -> float:
    return WIN_PAY if won else LOSS_PAY


def split_report(rows: list[dict]) -> dict:
    return {
        "summary": v4.summarize(rows),
        "train_to_0630": v4.summarize([r for r in rows if r["day_cn"] <= "2026-06-30"]),
        "recent_0701_plus": v4.summarize([r for r in rows if r["day_cn"] >= "2026-07-01"]),
        "d0701": v4.summarize([r for r in rows if r["day_cn"] == "2026-07-01"]),
        "d0702": v4.summarize([r for r in rows if r["day_cn"] == "2026-07-02"]),
        "d0703": v4.summarize([r for r in rows if r["day_cn"] == "2026-07-03"]),
    }


def side_allowed(row: dict, side_mode: str) -> bool:
    signal = str(row.get("signal"))
    if side_mode == "upper_only":
        return signal == "DOWN" and finite(row.get("breakout_side")) > 0
    if side_mode == "lower_only":
        return signal == "UP" and finite(row.get("breakout_side")) < 0
    if side_mode == "both":
        return signal in {"UP", "DOWN"}
    raise ValueError(f"unknown side_mode: {side_mode}")


def side_values(row: dict) -> dict:
    side = finite(row.get("breakout_side"))
    z = finite(row.get("z"))
    return {
        "side": side,
        "inside_abs_z": side * z if np.isfinite(side) and np.isfinite(z) else float("nan"),
        "side_slope": side * finite(row.get("m_slope60_bps")),
        "side_flow": side * finite(row.get("flow60")),
        "side_ob_imb": side * finite(row.get("ob_imb20")),
        "side_ob_micro": side * finite(row.get("ob_micro_bps")),
    }


def build_base_quality_cfg(side_mode: str) -> v3.V3Config:
    return v3.V3Config(
        name=f"V6_base_quality_{side_mode}",
        lookback_min=180,
        reentry_z=1.96,
        max_outside_sec=60,
        side_mode=side_mode,
        min_score_ratio=0.78,
        min_width_ratio=0.45,
        max_width_ratio=3.0,
        max_slope_side_bps=120,
        max_bandwalk10=6,
        max_half_life_min=40,
        max_flow60_side=0.10,
        max_ob_imb_side=0.10,
        max_ob_micro_side=0.001,
        max_peak_abs_z=3.2,
        cooldown_sec=0,
    )


def annotate_base_quality(row: dict, side_mode: str) -> dict | None:
    if not side_allowed(row, side_mode):
        return None
    ok, meta = v3.config_allows_v3(row, build_base_quality_cfg(side_mode))
    if not ok:
        return None
    out = dict(row)
    out["settle_idx"] = int(out["idx"]) + HORIZON_SEC
    out["base_quality_score_ratio"] = meta["score_ratio"]
    out["base_quality_score_ok"] = meta["score_ok"]
    out["base_quality_score_total"] = meta["score_total"]
    out["base_quality_failed_votes"] = ",".join(meta["failed"])
    return out


def consensus_votes(row: dict) -> tuple[int, list[str]]:
    sv = side_values(row)
    width = finite(row.get("m_width_ratio"))
    half_life = finite(row.get("m_half_life_min"))
    peak = finite(row.get("peak_abs_z"))
    outside = int(row.get("outside_sec") or 10**9)
    votes: list[str] = []
    if outside <= 30:
        votes.append("fresh_reentry")
    if np.isfinite(peak) and peak <= 2.05:
        votes.append("mild_tail")
    if np.isfinite(sv["side_slope"]) and sv["side_slope"] <= 70 and np.isfinite(width) and width <= 2.2:
        votes.append("no_trend_pressure")
    if np.isfinite(sv["side_flow"]) and sv["side_flow"] <= 0:
        votes.append("flow_rejects_breakout")
    if (
        np.isfinite(sv["inside_abs_z"])
        and sv["inside_abs_z"] <= 1.94
        and np.isfinite(half_life)
        and half_life <= 20
    ):
        votes.append("deep_fast_reentry")
    return len(votes), votes


def rule_allows(row: dict, spec: RuleSpec) -> tuple[bool, str]:
    if not side_allowed(row, spec.side_mode):
        return False, "side_disabled"
    sv = side_values(row)
    outside = int(row.get("outside_sec") or 10**9)
    peak = finite(row.get("peak_abs_z"))
    width = finite(row.get("m_width_ratio"))
    half_life = finite(row.get("m_half_life_min"))
    bandwalk = finite(row.get("m_bandwalk10"))

    checks: list[tuple[bool, str]] = []
    if spec.max_outside_sec is not None:
        checks.append((outside <= spec.max_outside_sec, "outside"))
    if spec.max_peak_abs_z is not None:
        checks.append((np.isfinite(peak) and peak <= spec.max_peak_abs_z, "peak_abs_z"))
    if spec.max_side_slope_bps is not None:
        checks.append((np.isfinite(sv["side_slope"]) and sv["side_slope"] <= spec.max_side_slope_bps, "side_slope"))
    if spec.max_width_ratio is not None:
        checks.append((np.isfinite(width) and width <= spec.max_width_ratio, "width_high"))
    if spec.min_width_ratio is not None:
        checks.append((np.isfinite(width) and width >= spec.min_width_ratio, "width_low"))
    if spec.max_side_flow is not None:
        checks.append((np.isfinite(sv["side_flow"]) and sv["side_flow"] <= spec.max_side_flow, "side_flow"))
    if spec.max_inside_abs_z is not None:
        checks.append((np.isfinite(sv["inside_abs_z"]) and sv["inside_abs_z"] <= spec.max_inside_abs_z, "inside_abs_z"))
    if spec.max_half_life_min is not None:
        checks.append((np.isfinite(half_life) and half_life <= spec.max_half_life_min, "half_life"))
    if spec.max_bandwalk10 is not None:
        checks.append((np.isfinite(bandwalk) and bandwalk <= spec.max_bandwalk10, "bandwalk"))
    if spec.require_ob_not_continuation and bool(row.get("ob_available")):
        ob_ok = True
        if np.isfinite(sv["side_ob_imb"]):
            ob_ok = ob_ok and sv["side_ob_imb"] <= 0.10
        if np.isfinite(sv["side_ob_micro"]):
            ob_ok = ob_ok and sv["side_ob_micro"] <= 0.001
        checks.append((ob_ok, "orderbook_continuation"))

    votes_n, votes = consensus_votes(row)
    if spec.min_consensus_votes:
        checks.append((votes_n >= spec.min_consensus_votes, "consensus"))

    failed = [name for ok, name in checks if not ok]
    if failed:
        return False, ",".join(failed)
    return True, ",".join(votes)


def apply_cooldown(rows: list[dict], cooldown_sec: int = HORIZON_SEC) -> list[dict]:
    out: list[dict] = []
    last_idx = -10**9
    for row in sorted(rows, key=lambda r: int(r["idx"])):
        idx = int(row["idx"])
        if idx - last_idx < cooldown_sec:
            continue
        out.append(row)
        last_idx = idx
    return out


def apply_daily_risk_stop(
    rows: list[dict],
    *,
    daily_stop_u: float = -2.0,
    loss_streak_limit: int = 2,
    cooldown_sec: int = HORIZON_SEC,
) -> tuple[list[dict], dict]:
    accepted: list[dict] = []
    pending: list[dict] = []
    settled_ptr = 0
    last_idx = -10**9
    day_pnl: dict[str, float] = {}
    day_loss_streak: dict[str, int] = {}
    halted_days: set[str] = set()
    skipped = {"cooldown": 0, "daily_stop": 0, "loss_streak": 0}

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
        if idx - last_idx < cooldown_sec:
            skipped["cooldown"] += 1
            continue
        if day in halted_days:
            if day_pnl.get(day, 0.0) <= daily_stop_u:
                skipped["daily_stop"] += 1
            else:
                skipped["loss_streak"] += 1
            continue
        out = dict(row)
        out["risk_gate"] = "daily_stop_after_settlement"
        out["day_closed_pnl_before"] = round(day_pnl.get(day, 0.0), 4)
        out["day_loss_streak_before"] = int(day_loss_streak.get(day, 0))
        accepted.append(out)
        pending.append(out)
        last_idx = idx
    return accepted, {
        "params": {
            "daily_stop_u": daily_stop_u,
            "loss_streak_limit": loss_streak_limit,
            "cooldown_sec": cooldown_sec,
            "uses_only_settled_trades": True,
        },
        "skipped": skipped,
    }


def fitting_risk(report: dict) -> dict:
    s = report["summary"]
    train = report["train_to_0630"]
    recent = report["recent_0701_plus"]
    days = s.get("days", [])
    active_days = len(days)
    losing_days = sum(1 for d in days if float(d.get("pnl", 0.0)) < 0)
    worst_day = min((float(d.get("pnl", 0.0)) for d in days), default=0.0)
    survivor = (
        train["n"] >= 12
        and recent["n"] >= 5
        and train["wr"] >= 60.0
        and recent["wr"] >= 60.0
        and train["pnl"] > 0.0
        and recent["pnl"] > 0.0
    )
    risk = "high"
    if s["n"] >= 30 and recent["n"] >= 8 and losing_days <= max(2, active_days // 3) and survivor:
        risk = "medium"
    if s["n"] >= 60 and recent["n"] >= 15 and losing_days <= max(2, active_days // 4) and survivor:
        risk = "low"
    return {
        "survivor": survivor,
        "risk": risk,
        "active_days": active_days,
        "losing_days": losing_days,
        "worst_day_pnl": round(worst_day, 4),
        "note": "high risk means sample is small, recent split failed, or daily stability is not enough.",
    }


def rule_specs() -> list[RuleSpec]:
    return [
        RuleSpec(
            name="V6_BASE_UPPER_QUALITY",
            side_mode="upper_only",
            priority=50,
            description="V4 upper false-break quality gate, but live-correct cooldown after filters.",
        ),
        RuleSpec(
            name="V6_FRESH_TAIL_CAP",
            side_mode="upper_only",
            priority=10,
            max_outside_sec=30,
            max_peak_abs_z=2.05,
            description="Fast upper-band re-entry; reject very extended tails.",
        ),
        RuleSpec(
            name="V6_SLOPE_WIDTH_REJECT",
            side_mode="upper_only",
            priority=20,
            max_side_slope_bps=70,
            max_width_ratio=2.2,
            description="Fade upper break only when 60m slope and width do not confirm trend continuation.",
        ),
        RuleSpec(
            name="V6_SLOPE_FLOW_REJECT",
            side_mode="upper_only",
            priority=30,
            max_side_slope_bps=70,
            max_side_flow=0.0,
            description="Fade upper break only when trade flow is not supporting the breakout.",
        ),
        RuleSpec(
            name="V6_FAST_DEEP_REENTRY",
            side_mode="upper_only",
            priority=40,
            max_inside_abs_z=1.94,
            max_half_life_min=20,
            description="Require a deeper return inside the band and a mean-reversion half-life compatible with 10m expiry.",
        ),
        RuleSpec(
            name="V6_CONSENSUS_2OF5_UPPER",
            side_mode="upper_only",
            priority=5,
            min_consensus_votes=2,
            description="Upper false break accepted when at least two independent rejection clues agree.",
        ),
        RuleSpec(
            name="V6_CONSENSUS_3OF5_UPPER",
            side_mode="upper_only",
            priority=4,
            min_consensus_votes=3,
            description="Stricter upper false break, at least three rejection clues.",
        ),
        RuleSpec(
            name="V6_CONSENSUS_2OF5_BOTH",
            side_mode="both",
            priority=60,
            min_consensus_votes=2,
            description="Symmetric test, used to check whether lower-band signals generalize.",
        ),
        RuleSpec(
            name="V6_CONSENSUS_2OF5_LOWER",
            side_mode="lower_only",
            priority=70,
            min_consensus_votes=2,
            description="Lower-band counterpart; diagnostic only unless it survives OOS.",
        ),
    ]


def evaluate_rule(base_rows: list[dict], spec: RuleSpec) -> tuple[list[dict], list[dict]]:
    filtered: list[dict] = []
    shadow: list[dict] = []
    for row in base_rows:
        ok, why = rule_allows(row, spec)
        if not side_allowed(row, spec.side_mode):
            continue
        candidate = dict(row)
        candidate["module"] = spec.name
        candidate["rule_name"] = spec.name
        candidate["regime"] = "focused_false_break_reversion"
        candidate["reason"] = spec.description
        votes_n, votes = consensus_votes(row)
        candidate["consensus_votes"] = votes_n
        candidate["consensus_vote_names"] = ",".join(votes)
        candidate["rule_filter_detail"] = why
        shadow.append(candidate)
        if ok:
            filtered.append(candidate)
    return apply_cooldown(filtered), shadow


def adaptive_shadow_selector(rule_rows: dict[str, list[dict]], specs: list[RuleSpec]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    for spec in specs:
        if spec.side_mode != "upper_only" or spec.name == "V6_BASE_UPPER_QUALITY":
            continue
        rows.extend(rule_rows[spec.name])
    priority = {spec.name: spec.priority for spec in specs}
    rows = sorted(rows, key=lambda r: (int(r["idx"]), priority.get(str(r.get("rule_name")), 999)))

    accepted: list[dict] = []
    pending_shadow: list[dict] = []
    hist: dict[str, list[bool]] = {}
    settled_ptr = 0
    last_idx = -10**9
    skipped = {"cooldown": 0, "rule_not_ready": 0, "rule_weak": 0}

    def settle_until(idx: int) -> None:
        nonlocal settled_ptr
        pending_shadow.sort(key=lambda r: int(r["settle_idx"]))
        while settled_ptr < len(pending_shadow) and int(pending_shadow[settled_ptr]["settle_idx"]) <= idx:
            row = pending_shadow[settled_ptr]
            settled_ptr += 1
            hist.setdefault(str(row["rule_name"]), []).append(bool(row["won"]))

    seen_shadow: set[tuple[str, int]] = set()
    for row in rows:
        idx = int(row["idx"])
        settle_until(idx)
        key = (str(row["rule_name"]), idx)
        if key not in seen_shadow:
            pending_shadow.append(row)
            seen_shadow.add(key)

        if idx - last_idx < HORIZON_SEC:
            skipped["cooldown"] += 1
            continue
        rule_hist = hist.get(str(row["rule_name"]), [])
        if len(rule_hist) < 8:
            skipped["rule_not_ready"] += 1
            continue
        tail = rule_hist[-16:]
        wr = sum(tail) / len(tail) * 100.0
        pnl = sum(payout(w) for w in tail)
        if wr < 60.0 or pnl <= 0.0:
            skipped["rule_weak"] += 1
            continue
        out = dict(row)
        out["module"] = "V6_ADAPTIVE_SHADOW_SELECTOR"
        out["risk_gate"] = "rule_shadow_oos_gate"
        out["rule_hist_n"] = len(rule_hist)
        out["rule_tail_wr"] = round(wr, 2)
        out["rule_tail_pnl"] = round(pnl, 4)
        accepted.append(out)
        last_idx = idx
    return accepted, {
        "params": {
            "min_rule_shadow_trades": 8,
            "lookback_rule_shadow_trades": 16,
            "min_rule_tail_wr": 60.0,
            "min_rule_tail_pnl": 0.0,
            "cooldown_sec": HORIZON_SEC,
            "learns_from_shadow_candidates": True,
        },
        "skipped": skipped,
    }


def count_base_rows(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "upper": 0, "lower": 0}
    upper = sum(1 for r in rows if finite(r.get("breakout_side")) > 0)
    lower = sum(1 for r in rows if finite(r.get("breakout_side")) < 0)
    return {"n": len(rows), "upper": upper, "lower": lower}


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
    raw_candidates = v1.generate_reversion_rows(
        bars,
        features,
        lookback_sec=180 * 60,
        second_context=ctx,
        reentry_z=1.96,
        max_outside_sec=900,
        state_filter="none",
        ob_filter="none",
        cooldown_sec=0,
    )
    base_both: list[dict] = []
    base_upper: list[dict] = []
    base_lower: list[dict] = []
    for row in raw_candidates:
        both = annotate_base_quality(row, "both")
        if both:
            base_both.append(both)
        upper = annotate_base_quality(row, "upper_only")
        if upper:
            base_upper.append(upper)
        lower = annotate_base_quality(row, "lower_only")
        if lower:
            base_lower.append(lower)

    specs = rule_specs()
    all_rule_rows: dict[str, list[dict]] = {}
    all_shadow_rows: dict[str, list[dict]] = {}
    rule_reports: list[dict] = []
    selected_trade_rows: list[dict] = []

    for spec in specs:
        base = base_both if spec.side_mode == "both" else base_upper if spec.side_mode == "upper_only" else base_lower
        rows, shadow = evaluate_rule(base, spec)
        stopped, stop_meta = apply_daily_risk_stop(rows)
        all_rule_rows[spec.name] = rows
        all_shadow_rows[spec.name] = shadow
        report = split_report(rows)
        stopped_report = split_report(stopped)
        risk = fitting_risk(report)
        rule_reports.append(
            {
                **asdict(spec),
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
                "daily_stop_n": stopped_report["summary"]["n"],
                "daily_stop_wr": stopped_report["summary"]["wr"],
                "daily_stop_pnl": stopped_report["summary"]["pnl"],
                "daily_stop_max_dd": stopped_report["summary"]["max_dd"],
                "survivor": risk["survivor"],
                "fit_risk": risk["risk"],
                "active_days": risk["active_days"],
                "losing_days": risk["losing_days"],
                "worst_day_pnl": risk["worst_day_pnl"],
            }
        )
        for row in rows:
            selected_trade_rows.append(row)

    adaptive_rows, adaptive_meta = adaptive_shadow_selector(all_rule_rows, specs)
    adaptive_stopped, adaptive_stop_meta = apply_daily_risk_stop(adaptive_rows)
    v4_upper_old = v4.generate_upper_reversion_rows(bars, features, ctx)

    rule_table = pd.DataFrame(rule_reports).sort_values(
        ["survivor", "recent_pnl", "train_pnl", "n"],
        ascending=[False, False, False, False],
    )
    OUT_RULES.parent.mkdir(parents=True, exist_ok=True)
    rule_table.to_csv(OUT_RULES, index=False, encoding="utf-8-sig")
    trades_df = pd.DataFrame(selected_trade_rows + adaptive_rows)
    trades_df.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")

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
            "lookback_sec": 180 * 60,
            "expiry_sec": HORIZON_SEC,
            "base_quality": asdict(build_base_quality_cfg("both")),
            "anti_overfit": [
                "Rules are declared before this V6 run from market logic and prior feature diagnostics; recent split is reported separately.",
                "Cooldown is applied after rule filters, matching live behavior: rejected candidates do not block later trades.",
                "Daily risk stop uses only trades whose 10-minute expiry has already settled.",
                "Adaptive selector learns from expired shadow candidates only; it is diagnostic unless it also survives recent split.",
            ],
        },
        "base_counts": {
            "raw_reentry_candidates": count_base_rows(raw_candidates),
            "base_quality_both": count_base_rows(base_both),
            "base_quality_upper": count_base_rows(base_upper),
            "base_quality_lower": count_base_rows(base_lower),
        },
        "v4_old_upper_lockout": split_report(v4_upper_old),
        "rules": {
            spec.name: {
                "spec": asdict(spec),
                "trade_lockout": split_report(all_rule_rows[spec.name]),
                "daily_stop": split_report(apply_daily_risk_stop(all_rule_rows[spec.name])[0]),
                "fit_check": fitting_risk(split_report(all_rule_rows[spec.name])),
            }
            for spec in specs
        },
        "adaptive_shadow_selector": {
            "trade_lockout": split_report(adaptive_rows),
            "daily_stop": split_report(adaptive_stopped),
            "gate": adaptive_meta,
            "daily_stop_gate": adaptive_stop_meta,
            "fit_check": fitting_risk(split_report(adaptive_rows)),
        },
        "rule_table": rule_table.to_dict("records"),
        "outputs": {"json": str(OUT_JSON), "trades_csv": str(OUT_TRADES), "rule_report_csv": str(OUT_RULES)},
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    result = run()
    print(
        json.dumps(
            {
                "data": {k: result["data"][k] for k in ("rows_dense", "rows_observed", "observed_pct", "first", "last")},
                "base_counts": result["base_counts"],
                "v4_old_upper_lockout": result["v4_old_upper_lockout"]["summary"],
                "top_rules": result["rule_table"][:10],
                "adaptive_shadow_selector": result["adaptive_shadow_selector"]["trade_lockout"]["summary"],
                "outputs": result["outputs"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
