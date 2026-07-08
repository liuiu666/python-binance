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
import research_normal_state_v6 as v6
import research_normal_state_v7_confirm_reentry as v7
import research_normal_state_v15_ob_state_gate as v15


OUT_JSON = ROOT / "tmp" / "normal_state_v17_high_freq_stability.json"
OUT_POLICIES = ROOT / "tmp" / "normal_state_v17_high_freq_stability_policies.csv"
OUT_TRADES = ROOT / "tmp" / "normal_state_v17_high_freq_stability_trades.csv"

HORIZON_SEC = 600
WIN_PAY = 0.8
LOSS_PAY = -1.0
TRAIN_CUTOFF = "2026-06-30"
VALID_START = "2026-07-01"
VALID_END = "2026-07-03"
LATEST_DAY = "2026-07-04"
BREAKEVEN_WR = abs(LOSS_PAY) / (WIN_PAY + abs(LOSS_PAY)) * 100.0


@dataclass(frozen=True)
class Policy:
    key: str
    side_mode: str
    state_gate: str
    min_edge_score: float
    min_gap_sec: int
    max_concurrent: int
    daily_stop_u: float | None
    loss_streak_limit: int | None
    require_ob_reject: bool = False
    extra_after_open_score: float | None = None
    require_no_trend_pressure: bool = False
    min_consensus_votes: int = 0
    max_width_ratio: float | None = None


POLICIES = [
    Policy("v15_shadow_current_gap600", "upper_only", "v15", 0.0, 600, 1, None, None),
    Policy("v17_stable_gap300_mc1_score3", "upper_only", "v15", 3.0, 300, 1, -2.0, 2),
    Policy("v17_stable_gap180_mc1_score3", "upper_only", "v15", 3.0, 180, 1, -2.0, 2),
    Policy("v17_hf_gap120_mc2_score3_extra4", "upper_only", "v15", 3.0, 120, 2, -2.0, 2, extra_after_open_score=4.0),
    Policy("v17_hf_gap60_mc2_score3_extra4", "upper_only", "v15", 3.0, 60, 2, -2.0, 2, extra_after_open_score=4.0),
    Policy("v17_hf_gap60_mc2_score4", "upper_only", "v15", 4.0, 60, 2, -2.0, 2),
    Policy("v17_hf_gap60_mc3_score4", "upper_only", "v15", 4.0, 60, 3, -2.0, 2),
    Policy("v17_ob_reject_gap60_mc2", "upper_only", "v15", 3.0, 60, 2, -2.0, 2, require_ob_reject=True),
    Policy("v17_notrend_gap60_mc2_score3", "upper_only", "v15", 3.0, 60, 2, -2.0, 2, require_no_trend_pressure=True),
    Policy("v17_notrend_gap120_mc2_score3", "upper_only", "v15", 3.0, 120, 2, -2.0, 2, require_no_trend_pressure=True),
    Policy("v17_width22_gap60_mc2_score3", "upper_only", "v15", 3.0, 60, 2, -2.0, 2, max_width_ratio=2.2),
    Policy("v17_consensus4_gap60_mc2", "upper_only", "v15", 0.0, 60, 2, -2.0, 2, min_consensus_votes=4),
    Policy("v17_both_gap300_mc1_score4", "both", "v15", 4.0, 300, 1, -2.0, 2),
    Policy("v17_both_gap120_mc2_score4", "both", "v15", 4.0, 120, 2, -2.0, 2, extra_after_open_score=4.5),
]


def payout(won: bool) -> float:
    return WIN_PAY if bool(won) else LOSS_PAY


def finite(value: object) -> float:
    return v6.finite(value)


def wilson_low(wins: int, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    half = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n)) / n) / denom
    return round((center - half) * 100.0, 2)


def max_drawdown(wons: list[bool]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for won in wons:
        equity += payout(won)
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return round(worst, 4)


def summarize(df: pd.DataFrame, *, first_day: str = "", last_day: str = "") -> dict:
    if df.empty:
        return {
            "n": 0,
            "wins": 0,
            "wr": 0.0,
            "pnl": 0.0,
            "ev": 0.0,
            "max_dd": 0.0,
            "wilson_low": 0.0,
            "active_days": 0,
            "calendar_days": 0,
            "avg_per_active_day": 0.0,
            "avg_per_calendar_day": 0.0,
            "losing_days": 0,
            "worst_day": "",
            "worst_day_pnl": 0.0,
            "days": [],
        }
    ordered = df.sort_values("idx")
    wons = [bool(x) for x in ordered["won"].tolist()]
    wins = int(sum(wons))
    days = []
    for day, group in ordered.groupby("day_cn", sort=True):
        gw = [bool(x) for x in group["won"].tolist()]
        gpnl = round(sum(payout(x) for x in gw), 4)
        days.append(
            {
                "day": str(day),
                "n": int(len(group)),
                "wr": round(sum(gw) / len(gw) * 100.0, 2),
                "pnl": gpnl,
                "max_dd": max_drawdown(gw),
            }
        )
    if first_day and last_day:
        calendar_days = len(pd.date_range(first_day, last_day, freq="D"))
    else:
        calendar_days = len(pd.date_range(min(d["day"] for d in days), max(d["day"] for d in days), freq="D"))
    losing_days = sum(1 for d in days if float(d["pnl"]) < 0.0)
    worst = min(days, key=lambda d: float(d["pnl"])) if days else {"day": "", "pnl": 0.0}
    pnl = round(sum(payout(x) for x in wons), 4)
    return {
        "n": int(len(ordered)),
        "wins": wins,
        "wr": round(wins / len(ordered) * 100.0, 2),
        "pnl": pnl,
        "ev": round(pnl / len(ordered), 5),
        "max_dd": max_drawdown(wons),
        "wilson_low": wilson_low(wins, len(ordered)),
        "active_days": len(days),
        "calendar_days": int(calendar_days),
        "avg_per_active_day": round(len(ordered) / len(days), 3) if days else 0.0,
        "avg_per_calendar_day": round(len(ordered) / calendar_days, 3) if calendar_days else 0.0,
        "losing_days": losing_days,
        "worst_day": str(worst["day"]),
        "worst_day_pnl": round(float(worst["pnl"]), 4),
        "days": days,
    }


def state_gate_allows(row: dict, gate: str) -> bool:
    bandwalk = finite(row.get("m_bandwalk10"))
    sigma10 = finite(row.get("sigma10_bps"))
    if not np.isfinite(bandwalk):
        return False
    if gate == "capacity":
        return bandwalk < 6.0
    if gate == "quality":
        return 3.0 <= bandwalk < 6.0
    if gate == "v15":
        return (3.0 <= bandwalk < 6.0) or (bandwalk < 3.0 and np.isfinite(sigma10) and sigma10 > 18.0)
    raise ValueError(f"unknown state gate: {gate}")


def ob_rejects_breakout(row: dict) -> bool:
    if not bool(row.get("ob_available")):
        return False
    side = finite(row.get("breakout_side"))
    imb = finite(row.get("ob_imb20"))
    micro = finite(row.get("ob_micro_bps"))
    checks = []
    if np.isfinite(imb):
        checks.append(side * imb <= 0.05)
    if np.isfinite(micro):
        checks.append(side * micro <= 0.001)
    return bool(checks) and all(checks)


def edge_score(row: dict) -> float:
    score = 0.0
    votes = int(row.get("consensus_votes") or 0)
    if votes >= 3:
        score += 1.0
    if votes >= 4:
        score += 1.0
    bandwalk = finite(row.get("m_bandwalk10"))
    sigma10 = finite(row.get("sigma10_bps"))
    peak = finite(row.get("peak_abs_z"))
    adverse = finite(row.get("confirm_adverse_bps"))
    side_vals = v6.side_values(row)
    if 3.0 <= bandwalk < 6.0:
        score += 1.0
    if np.isfinite(sigma10) and 12.0 <= sigma10 <= 35.0:
        score += 1.0
    if np.isfinite(peak) and peak <= 2.05:
        score += 1.0
    if np.isfinite(adverse) and adverse <= 1.0:
        score += 1.0
    if np.isfinite(side_vals["side_flow"]) and side_vals["side_flow"] <= 0.0:
        score += 1.0
    if ob_rejects_breakout(row):
        score += 1.0
    if np.isfinite(side_vals["side_slope"]) and side_vals["side_slope"] > 70.0:
        score -= 1.0
    return round(float(score), 4)


def rule_for_side(side_mode: str) -> v6.RuleSpec:
    name = "V6_CONSENSUS_2OF5_UPPER" if side_mode == "upper_only" else "V6_CONSENSUS_2OF5_BOTH"
    spec = next((s for s in v6.rule_specs() if s.name == name), None)
    if spec is None:
        raise RuntimeError(f"{name} not found")
    return spec


def prepare_candidates(bars: pd.DataFrame, features: pd.DataFrame, ctx: pd.DataFrame, side_mode: str) -> list[dict]:
    raw = v1.generate_reversion_rows(
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
    spec = rule_for_side(side_mode)
    out = []
    for row in raw:
        annotated = v6.annotate_base_quality(row, side_mode)
        if not annotated:
            continue
        ok, detail = v6.rule_allows(annotated, spec)
        if not ok:
            continue
        votes_n, votes = v6.consensus_votes(annotated)
        item = dict(annotated)
        item["side_mode"] = side_mode
        item["source_rule"] = spec.name
        item["rule_filter_detail"] = detail
        item["consensus_votes"] = votes_n
        item["consensus_vote_names"] = ",".join(votes)
        out.append(item)
    confirmed, _meta = v7.apply_confirmation(out, bars, delay_sec=5, max_adverse_bps=5.0, cooldown_sec=0)
    for row in confirmed:
        row["edge_score"] = edge_score(row)
        row["ob_rejects_breakout"] = ob_rejects_breakout(row)
    return confirmed


def select_policy(rows: list[dict], policy: Policy) -> tuple[pd.DataFrame, dict]:
    accepted: list[dict] = []
    open_rows: list[dict] = []
    last_idx = -10**9
    day_pnl: dict[str, float] = {}
    day_loss_streak: dict[str, int] = {}
    halted_days: set[str] = set()
    skipped = {
        "state_gate": 0,
        "score": 0,
        "ob": 0,
        "gap": 0,
        "concurrency": 0,
        "daily_stop": 0,
        "loss_streak": 0,
        "extra_open_score": 0,
        "trend_pressure": 0,
        "consensus": 0,
        "width": 0,
    }

    def settle_until(idx: int) -> None:
        still_open = []
        for row in open_rows:
            if int(row["settle_idx"]) <= idx:
                day = str(row["day_cn"])
                won = bool(row["won"])
                day_pnl[day] = round(day_pnl.get(day, 0.0) + payout(won), 4)
                day_loss_streak[day] = 0 if won else day_loss_streak.get(day, 0) + 1
                if policy.daily_stop_u is not None and day_pnl[day] <= policy.daily_stop_u:
                    halted_days.add(day)
                if policy.loss_streak_limit is not None and day_loss_streak[day] >= policy.loss_streak_limit:
                    halted_days.add(day)
            else:
                still_open.append(row)
        open_rows[:] = still_open

    for row in sorted(rows, key=lambda r: int(r["idx"])):
        idx = int(row["idx"])
        day = str(row["day_cn"])
        settle_until(idx)
        if not state_gate_allows(row, policy.state_gate):
            skipped["state_gate"] += 1
            continue
        if float(row.get("edge_score") or 0.0) < policy.min_edge_score:
            skipped["score"] += 1
            continue
        if policy.require_ob_reject and not ob_rejects_breakout(row):
            skipped["ob"] += 1
            continue
        if policy.require_no_trend_pressure and "no_trend_pressure" not in str(row.get("consensus_vote_names") or ""):
            skipped["trend_pressure"] += 1
            continue
        if policy.min_consensus_votes and int(row.get("consensus_votes") or 0) < policy.min_consensus_votes:
            skipped["consensus"] += 1
            continue
        width = finite(row.get("m_width_ratio"))
        if policy.max_width_ratio is not None and (not np.isfinite(width) or width > policy.max_width_ratio):
            skipped["width"] += 1
            continue
        if idx - last_idx < policy.min_gap_sec:
            skipped["gap"] += 1
            continue
        if len(open_rows) >= policy.max_concurrent:
            skipped["concurrency"] += 1
            continue
        if day in halted_days:
            if policy.daily_stop_u is not None and day_pnl.get(day, 0.0) <= policy.daily_stop_u:
                skipped["daily_stop"] += 1
            else:
                skipped["loss_streak"] += 1
            continue
        if open_rows and policy.extra_after_open_score is not None and float(row.get("edge_score") or 0.0) < policy.extra_after_open_score:
            skipped["extra_open_score"] += 1
            continue
        out = dict(row)
        out["policy"] = policy.key
        out["open_positions_before"] = len(open_rows)
        out["day_closed_pnl_before"] = round(day_pnl.get(day, 0.0), 4)
        out["day_loss_streak_before"] = int(day_loss_streak.get(day, 0))
        accepted.append(out)
        open_rows.append(out)
        last_idx = idx
    return pd.DataFrame(accepted), skipped


def max_concurrent(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    events: list[tuple[int, int]] = []
    for _, row in df.iterrows():
        events.append((int(row["idx"]), 1))
        events.append((int(row["settle_idx"]), -1))
    active = 0
    peak = 0
    for _, delta in sorted(events):
        active += delta
        peak = max(peak, active)
    return peak


def policy_report(policy: Policy, df: pd.DataFrame, skipped: dict, data_first_day: str, data_last_day: str) -> dict:
    summary = summarize(df, first_day=data_first_day, last_day=data_last_day)
    train = summarize(df[df["day_cn"] <= TRAIN_CUTOFF], first_day=data_first_day, last_day=TRAIN_CUTOFF) if not df.empty else summarize(df)
    valid = summarize(df[(df["day_cn"] >= VALID_START) & (df["day_cn"] <= VALID_END)], first_day=VALID_START, last_day=VALID_END) if not df.empty else summarize(df)
    latest = summarize(df[df["day_cn"] == LATEST_DAY], first_day=LATEST_DAY, last_day=LATEST_DAY) if not df.empty else summarize(df)
    flags = []
    if summary["n"] < 50:
        flags.append("sample_under_50")
    if valid["n"] < 8:
        flags.append("valid_under_8")
    if latest["n"] < 1:
        flags.append("no_latest_day_trade")
    if train["n"] < 20 or train["wr"] < 60.0 or train["pnl"] <= 0:
        flags.append("train_weak")
    if valid["n"] < 5 or valid["wr"] < 60.0 or valid["pnl"] <= 0:
        flags.append("valid_weak")
    if latest["n"] > 0 and (latest["wr"] < BREAKEVEN_WR or latest["pnl"] < 0):
        flags.append("latest_weak")
    if summary["max_dd"] < -3.0:
        flags.append("max_dd_worse_than_3u")
    if summary["losing_days"] > max(2, summary["active_days"] // 3):
        flags.append("too_many_losing_days")
    if max_concurrent(df) > 1:
        flags.append("overlap")
    return {
        "policy": policy.key,
        "side_mode": policy.side_mode,
        "state_gate": policy.state_gate,
        "min_edge_score": policy.min_edge_score,
        "min_gap_sec": policy.min_gap_sec,
        "max_concurrent_limit": policy.max_concurrent,
        "max_concurrent_seen": max_concurrent(df),
        "daily_stop_u": policy.daily_stop_u,
        "loss_streak_limit": policy.loss_streak_limit,
        "require_ob_reject": policy.require_ob_reject,
        "extra_after_open_score": policy.extra_after_open_score,
        "require_no_trend_pressure": policy.require_no_trend_pressure,
        "min_consensus_votes": policy.min_consensus_votes,
        "max_width_ratio": policy.max_width_ratio,
        "n": summary["n"],
        "wr": summary["wr"],
        "pnl": summary["pnl"],
        "ev": summary["ev"],
        "max_dd": summary["max_dd"],
        "wilson_low": summary["wilson_low"],
        "active_days": summary["active_days"],
        "calendar_days": summary["calendar_days"],
        "avg_per_active_day": summary["avg_per_active_day"],
        "avg_per_calendar_day": summary["avg_per_calendar_day"],
        "losing_days": summary["losing_days"],
        "worst_day": summary["worst_day"],
        "worst_day_pnl": summary["worst_day_pnl"],
        "train_n": train["n"],
        "train_wr": train["wr"],
        "train_pnl": train["pnl"],
        "valid_n": valid["n"],
        "valid_wr": valid["wr"],
        "valid_pnl": valid["pnl"],
        "latest_n": latest["n"],
        "latest_wr": latest["wr"],
        "latest_pnl": latest["pnl"],
        "down_n": int((df["signal"] == "DOWN").sum()) if not df.empty else 0,
        "up_n": int((df["signal"] == "UP").sum()) if not df.empty else 0,
        "risk_flags": ";".join(flags),
        "skipped": json.dumps(skipped, ensure_ascii=False, sort_keys=True),
        "days": summary["days"],
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
    candidates_by_side = {
        "upper_only": prepare_candidates(bars, features, ctx, "upper_only"),
        "both": prepare_candidates(bars, features, ctx, "both"),
    }
    first_day = bars.index.min().tz_convert("Asia/Shanghai").strftime("%Y-%m-%d")
    last_day = bars.index.max().tz_convert("Asia/Shanghai").strftime("%Y-%m-%d")
    reports = []
    trade_dfs = []
    for policy in POLICIES:
        candidates = candidates_by_side[policy.side_mode]
        df, skipped = select_policy(candidates, policy)
        reports.append(policy_report(policy, df, skipped, first_day, last_day))
        if not df.empty:
            trade_dfs.append(df)

    table = pd.DataFrame(reports).sort_values(
        ["valid_pnl", "latest_pnl", "pnl", "n"],
        ascending=[False, False, False, False],
    )
    table = table.astype(object).where(pd.notna(table), None)
    trades = pd.concat(trade_dfs, ignore_index=True) if trade_dfs else pd.DataFrame()
    OUT_POLICIES.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(OUT_POLICIES, index=False, encoding="utf-8-sig")
    trades.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")

    clean = table[~table["risk_flags"].str.contains("valid_weak|train_weak|latest_weak|max_dd_worse_than_3u|too_many_losing_days", regex=True, na=False)]
    higher_than_v15 = clean[clean["n"] > int(table.loc[table["policy"] == "v15_shadow_current_gap600", "n"].iloc[0])] if not clean.empty else clean
    recommended = higher_than_v15.iloc[0].to_dict() if not higher_than_v15.empty else {}
    result = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "data": {
            "rows_dense": int(len(bars)),
            "rows_observed": int(bars["observed"].sum()),
            "observed_pct": round(float(bars["observed"].mean() * 100.0), 4),
            "first": bars.index.min().isoformat(),
            "last": bars.index.max().isoformat(),
            "first_day_cn": first_day,
            "last_day_cn": last_day,
            "second_sources_count": len(second_sources),
            "minute_source": minute["minute_source"].iloc[0] if "minute_source" in minute else "",
            "orderbook_sources_count": len(orderbook_sources),
            "orderbook_available_pct": round(float(orderbook["ob_available"].mean() * 100.0), 4),
        },
        "method": {
            "goal": "Increase trade count without accepting unstable overlap losses.",
            "candidate_generation": "Same 180-minute second-level normal-band false-break reentry as V15; D5/A5 delayed entry; 10-minute binary settlement.",
            "high_frequency_controls": [
                "min_gap_sec controls how often a new signal may be accepted.",
                "max_concurrent limits unresolved 10-minute positions.",
                "edge_score is built from pre-existing rejection features: consensus votes, mild bandwalk, normal volatility, mild tail, low adverse confirmation, flow rejection, orderbook rejection.",
                "daily_stop_u and loss_streak_limit only use settled outcomes, so they are live-causal.",
            ],
            "anti_overfit": [
                "Parameters are a small policy grid, not a wide numeric search.",
                "Train <= 2026-06-30, validation 2026-07-01..2026-07-03, and latest 2026-07-04 are reported separately.",
                "A higher-frequency recommendation is rejected if train/validation/latest or drawdown flags fail.",
            ],
        },
        "candidate_counts": {k: len(v) for k, v in candidates_by_side.items()},
        "policy_table": table.to_dict("records"),
        "recommended_higher_frequency": recommended,
        "outputs": {"json": str(OUT_JSON), "policies_csv": str(OUT_POLICIES), "trades_csv": str(OUT_TRADES)},
    }
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return result


if __name__ == "__main__":
    output = run()
    print(
        json.dumps(
            {
                "data": output["data"],
                "candidate_counts": output["candidate_counts"],
                "policy_table": output["policy_table"],
                "recommended_higher_frequency": output["recommended_higher_frequency"],
                "outputs": output["outputs"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
