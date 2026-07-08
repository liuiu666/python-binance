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

import research_normal_state_v18_meta_gate as v18


OUT_JSON = ROOT / "tmp" / "normal_state_v19_filter_fix.json"
OUT_POLICIES = ROOT / "tmp" / "normal_state_v19_filter_fix_policies.csv"
OUT_TRADES = ROOT / "tmp" / "normal_state_v19_filter_fix_trades.csv"
OUT_BLOCKED = ROOT / "tmp" / "normal_state_v19_filter_fix_blocked.csv"

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
    pool: str
    min_gap_sec: int
    max_concurrent: int
    veto: str


POLICIES = [
    Policy("baseline_upper_v15_gap60", "upper_v15", 60, 1, "none"),
    Policy("baseline_upper_capacity_gap60", "upper_capacity", 60, 1, "none"),
    Policy("v19_upper_v15_ob_confirm_veto_g60", "upper_v15", 60, 1, "ob_confirm_weak"),
    Policy("v19_upper_capacity_ob_confirm_veto_g60", "upper_capacity", 60, 1, "ob_confirm_weak"),
    Policy("v19_upper_v15_ob_confirm_veto_g60_mc2", "upper_v15", 60, 2, "ob_confirm_weak"),
    Policy("v19_upper_capacity_ob_confirm_veto_g60_mc2", "upper_capacity", 60, 2, "ob_confirm_weak"),
    Policy("v19_upper_v15_ob_only_veto_g60", "upper_v15", 60, 1, "ob_weak"),
    Policy("v19_upper_capacity_ob_only_veto_g60", "upper_capacity", 60, 1, "ob_weak"),
    Policy("v19_upper_v15_price_confirm_veto_g60", "upper_v15", 60, 1, "price_confirm_weak"),
    Policy("v19_upper_capacity_price_confirm_veto_g60", "upper_capacity", 60, 1, "price_confirm_weak"),
    Policy("v19_upper_v15_ob_or_price_veto_g60", "upper_v15", 60, 1, "ob_or_price_weak"),
    Policy("v19_upper_capacity_ob_or_price_veto_g60", "upper_capacity", 60, 1, "ob_or_price_weak"),
]


def payout(won: bool) -> float:
    return WIN_PAY if bool(won) else LOSS_PAY


def finite(value: object) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return out if math.isfinite(out) else float("nan")


def clean_json_value(value):
    if isinstance(value, dict):
        return {str(k): clean_json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_json_value(v) for v in value]
    if isinstance(value, tuple):
        return [clean_json_value(v) for v in value]
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        value = float(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if pd.isna(value) if not isinstance(value, (str, bytes, bool, type(None))) else False:
        return None
    return value


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
    pnl = round(sum(payout(x) for x in wons), 4)
    worst = min(days, key=lambda d: float(d["pnl"])) if days else {"day": "", "pnl": 0.0}
    return {
        "n": int(len(ordered)),
        "wins": wins,
        "wr": round(wins / len(ordered) * 100.0, 2),
        "pnl": pnl,
        "ev": round(pnl / len(ordered), 5),
        "max_dd": max_drawdown(wons),
        "wilson_low": wilson_low(wins, len(ordered)),
        "active_days": len(days),
        "avg_per_active_day": round(len(ordered) / len(days), 3) if days else 0.0,
        "avg_per_calendar_day": round(len(ordered) / calendar_days, 3) if calendar_days else 0.0,
        "losing_days": sum(1 for d in days if float(d["pnl"]) < 0.0),
        "worst_day": str(worst["day"]),
        "worst_day_pnl": round(float(worst["pnl"]), 4),
        "days": days,
    }


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


def load_candidates() -> pd.DataFrame:
    if not v18.OUT_CANDIDATES.exists():
        v18.run()
    df = pd.read_csv(v18.OUT_CANDIDATES)
    if df.empty:
        return df
    df = df.sort_values(["pool", "idx"]).reset_index(drop=True)
    return df


def is_ob_weak(row: pd.Series) -> bool:
    available = finite(row.get("ob_available_f"))
    if not np.isfinite(available) or available < 0.5:
        return False
    side_imb = finite(row.get("ob_side_imb20"))
    side_micro = finite(row.get("ob_side_micro_bps"))
    weak_imb = np.isfinite(side_imb) and side_imb > -0.35
    weak_micro = np.isfinite(side_micro) and side_micro > -0.0035
    return bool(weak_imb or weak_micro)


def is_confirm_weak(row: pd.Series) -> bool:
    adverse = finite(row.get("confirm_adverse_bps"))
    return np.isfinite(adverse) and -1.4 < adverse < 1.0


def is_price_weak(row: pd.Series) -> bool:
    width = finite(row.get("m_width_ratio"))
    sigma10 = finite(row.get("sigma10_bps"))
    bandwalk = finite(row.get("m_bandwalk10"))
    wide_low_vol = np.isfinite(width) and np.isfinite(sigma10) and width > 2.2 and sigma10 < 18.0
    early_low_vol = np.isfinite(bandwalk) and np.isfinite(sigma10) and bandwalk <= 3.0 and sigma10 < 15.0
    return bool(wide_low_vol or early_low_vol)


def veto_reason(row: pd.Series, name: str) -> str | None:
    if name == "none":
        return None
    ob_weak = is_ob_weak(row)
    confirm_weak = is_confirm_weak(row)
    price_weak = is_price_weak(row)
    if name == "ob_confirm_weak" and ob_weak and confirm_weak:
        return "ob_confirm_weak"
    if name == "ob_weak" and ob_weak:
        return "ob_weak"
    if name == "price_confirm_weak" and price_weak and confirm_weak:
        return "price_confirm_weak"
    if name == "ob_or_price_weak" and ((ob_weak and confirm_weak) or (price_weak and confirm_weak)):
        return "ob_or_price_weak"
    return None


def select_policy(candidates: pd.DataFrame, policy: Policy) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    accepted: list[dict] = []
    blocked: list[dict] = []
    open_rows: list[dict] = []
    last_idx = -10**9
    skipped = {"veto": 0, "gap": 0, "concurrency": 0}

    for _, row in candidates[candidates["pool"] == policy.pool].sort_values("idx").iterrows():
        idx = int(row["idx"])
        open_rows = [r for r in open_rows if int(r["settle_idx"]) > idx]
        reason = veto_reason(row, policy.veto)
        if reason is not None:
            out = row.to_dict()
            out["policy"] = policy.key
            out["veto_reason"] = reason
            blocked.append(out)
            skipped["veto"] += 1
            continue
        if idx - last_idx < policy.min_gap_sec:
            skipped["gap"] += 1
            continue
        if len(open_rows) >= policy.max_concurrent:
            skipped["concurrency"] += 1
            continue
        out = row.to_dict()
        out["policy"] = policy.key
        out["veto_reason"] = ""
        out["open_positions_before"] = len(open_rows)
        accepted.append(out)
        open_rows.append(out)
        last_idx = idx

    return pd.DataFrame(accepted), pd.DataFrame(blocked), skipped


def split_summaries(df: pd.DataFrame, first_day: str, last_day: str) -> dict:
    train = df[df["day_cn"] <= TRAIN_CUTOFF] if not df.empty else df
    valid = df[(df["day_cn"] >= VALID_START) & (df["day_cn"] <= VALID_END)] if not df.empty else df
    latest = df[df["day_cn"] == LATEST_DAY] if not df.empty else df
    return {
        "summary": summarize(df, first_day=first_day, last_day=last_day),
        "train": summarize(train, first_day=first_day, last_day=TRAIN_CUTOFF),
        "valid": summarize(valid, first_day=VALID_START, last_day=VALID_END),
        "latest": summarize(latest, first_day=LATEST_DAY, last_day=LATEST_DAY),
    }


def report_policy(policy: Policy, accepted: pd.DataFrame, blocked: pd.DataFrame, skipped: dict, first_day: str, last_day: str) -> dict:
    parts = split_summaries(accepted, first_day, last_day)
    blocked_summary = summarize(blocked, first_day=first_day, last_day=last_day)
    blocked_latest_df = blocked[blocked["day_cn"] == LATEST_DAY] if not blocked.empty else blocked
    blocked_latest = summarize(blocked_latest_df, first_day=LATEST_DAY, last_day=LATEST_DAY)
    s = parts["summary"]
    train = parts["train"]
    valid = parts["valid"]
    latest = parts["latest"]
    flags = []
    if s["n"] < 30:
        flags.append("sample_under_30")
    if s["n"] < 50:
        flags.append("sample_under_50")
    if train["n"] < 15 or train["wr"] < 60.0 or train["pnl"] <= 0:
        flags.append("train_weak")
    if valid["n"] < 5 or valid["wr"] < 60.0 or valid["pnl"] <= 0:
        flags.append("valid_weak")
    if latest["n"] > 0 and (latest["wr"] < BREAKEVEN_WR or latest["pnl"] < 0):
        flags.append("latest_weak")
    if latest["n"] == 0 and blocked_latest["n"] == 0:
        flags.append("no_latest_day_trade")
    if latest["n"] == 0 and blocked_latest["n"] > 0 and blocked_latest["wr"] >= BREAKEVEN_WR:
        flags.append("latest_veto_may_kill_good_trade")
    if s["max_dd"] < -3.0:
        flags.append("max_dd_worse_than_3u")
    if s["losing_days"] > max(2, s["active_days"] // 3):
        flags.append("too_many_losing_days")
    if blocked_summary["n"] >= 3 and blocked_summary["wr"] >= BREAKEVEN_WR:
        flags.append("veto_may_kill_good_trades")
    return {
        "policy": policy.key,
        "pool": policy.pool,
        "veto": policy.veto,
        "min_gap_sec": policy.min_gap_sec,
        "max_concurrent_limit": policy.max_concurrent,
        "max_concurrent_seen": max_concurrent(accepted),
        "n": s["n"],
        "wr": s["wr"],
        "pnl": s["pnl"],
        "ev": s["ev"],
        "max_dd": s["max_dd"],
        "wilson_low": s["wilson_low"],
        "active_days": s["active_days"],
        "avg_per_active_day": s["avg_per_active_day"],
        "avg_per_calendar_day": s["avg_per_calendar_day"],
        "losing_days": s["losing_days"],
        "worst_day": s["worst_day"],
        "worst_day_pnl": s["worst_day_pnl"],
        "train_n": train["n"],
        "train_wr": train["wr"],
        "train_pnl": train["pnl"],
        "valid_n": valid["n"],
        "valid_wr": valid["wr"],
        "valid_pnl": valid["pnl"],
        "latest_n": latest["n"],
        "latest_wr": latest["wr"],
        "latest_pnl": latest["pnl"],
        "latest_blocked_n": blocked_latest["n"],
        "latest_blocked_wr": blocked_latest["wr"],
        "latest_blocked_pnl": blocked_latest["pnl"],
        "blocked_n": blocked_summary["n"],
        "blocked_wr": blocked_summary["wr"],
        "blocked_pnl": blocked_summary["pnl"],
        "down_n": int((accepted["signal"] == "DOWN").sum()) if not accepted.empty else 0,
        "up_n": int((accepted["signal"] == "UP").sum()) if not accepted.empty else 0,
        "risk_flags": ";".join(flags),
        "skipped": json.dumps(skipped, ensure_ascii=False, sort_keys=True),
        "days": s["days"],
        "blocked_days": blocked_summary["days"],
    }


def infer_day_range(df: pd.DataFrame) -> tuple[str, str]:
    if df.empty:
        return "", ""
    return str(df["day_cn"].min()), str(df["day_cn"].max())


def run() -> dict:
    candidates = load_candidates()
    first_day, last_day = infer_day_range(candidates)
    policy_rows = []
    trade_dfs = []
    blocked_dfs = []
    for policy in POLICIES:
        accepted, blocked, skipped = select_policy(candidates, policy)
        policy_rows.append(report_policy(policy, accepted, blocked, skipped, first_day, last_day))
        if not accepted.empty:
            trade_dfs.append(accepted)
        if not blocked.empty:
            blocked_dfs.append(blocked)

    table = pd.DataFrame(policy_rows)
    if not table.empty:
        table = table.sort_values(["valid_pnl", "latest_pnl", "pnl", "n"], ascending=[False, False, False, False])
        table = table.astype(object).where(pd.notna(table), None)
    trades = pd.concat(trade_dfs, ignore_index=True) if trade_dfs else pd.DataFrame()
    blocked_all = pd.concat(blocked_dfs, ignore_index=True) if blocked_dfs else pd.DataFrame()

    OUT_POLICIES.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(OUT_POLICIES, index=False, encoding="utf-8-sig")
    trades.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    blocked_all.to_csv(OUT_BLOCKED, index=False, encoding="utf-8-sig")

    clean = table[
        ~table["risk_flags"].str.contains(
            "sample_under_30|train_weak|valid_weak|latest_weak|latest_veto_may_kill_good_trade|max_dd_worse_than_3u|too_many_losing_days|veto_may_kill_good_trades",
            regex=True,
            na=False,
        )
    ] if not table.empty else table
    recommended = clean.iloc[0].to_dict() if not clean.empty else {}

    result = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "data": {
            "candidate_rows": int(len(candidates)),
            "first_day_cn": first_day,
            "last_day_cn": last_day,
            "pools": {str(k): int(v) for k, v in candidates.groupby("pool").size().to_dict().items()},
        },
        "method": {
            "problem_fixed": "Do not use a weak meta probability as a hard filter. V19 only vetoes explicit weak-confirmation states.",
            "primary_veto": {
                "name": "ob_confirm_weak",
                "logic": "skip if orderbook is available but side-normalized imbalance/microprice does not reject the breakout, and the 5-second delayed confirmation is not clearly favorable.",
                "causal_inputs": ["ob_side_imb20", "ob_side_micro_bps", "confirm_adverse_bps"],
            },
            "comparison_vetoes": ["ob_weak", "price_confirm_weak", "ob_or_price_weak"],
            "settlement": "10-minute binary option settlement, same candidate pool as V18/V15.",
        },
        "policy_table": table.to_dict("records") if not table.empty else [],
        "recommended": recommended,
        "outputs": {
            "json": str(OUT_JSON),
            "policies_csv": str(OUT_POLICIES),
            "trades_csv": str(OUT_TRADES),
            "blocked_csv": str(OUT_BLOCKED),
        },
    }
    OUT_JSON.write_text(json.dumps(clean_json_value(result), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return result


if __name__ == "__main__":
    output = run()
    print(
        json.dumps(
            clean_json_value(
                {
                    "data": output["data"],
                    "top": output["policy_table"][:20],
                    "recommended": output["recommended"],
                    "outputs": output["outputs"],
                }
            ),
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
    )
