from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_normal_state_v1 as v1
import research_normal_state_v3 as v3
import research_normal_state_v6 as v6
import research_normal_state_v7_confirm_reentry as v7
import research_normal_state_v17_high_freq_stability as v17


OUT_JSON = ROOT / "tmp" / "normal_state_v18_meta_gate.json"
OUT_POLICIES = ROOT / "tmp" / "normal_state_v18_meta_gate_policies.csv"
OUT_TRADES = ROOT / "tmp" / "normal_state_v18_meta_gate_trades.csv"
OUT_CANDIDATES = ROOT / "tmp" / "normal_state_v18_meta_gate_candidates.csv"

WIN_PAY = 0.8
LOSS_PAY = -1.0
BREAKEVEN_WR = abs(LOSS_PAY) / (WIN_PAY + abs(LOSS_PAY)) * 100.0
TRAIN_CUTOFF = "2026-06-30"
VALID_START = "2026-07-01"
VALID_END = "2026-07-03"
LATEST_DAY = "2026-07-04"


@dataclass(frozen=True)
class CandidatePool:
    key: str
    side_mode: str
    state_gate: str


@dataclass(frozen=True)
class MetaPolicy:
    key: str
    pool: str
    threshold: float
    min_gap_sec: int
    max_concurrent: int
    daily_stop_u: float | None = -2.0
    loss_streak_limit: int | None = 2


POOLS = [
    CandidatePool("upper_v15", "upper_only", "v15"),
    CandidatePool("upper_capacity", "upper_only", "capacity"),
    CandidatePool("both_v15", "both", "v15"),
    CandidatePool("both_capacity", "both", "capacity"),
]

THRESHOLDS = [0.50, 0.55, 0.60, 0.62, 0.65, 0.70]
MIN_GAPS = [60, 120, 300, 600]
MAX_CONCURRENCY = [1, 2]

FEATURE_COLUMNS = [
    "side",
    "z",
    "peak_abs_z",
    "outside_sec",
    "sigma10_bps",
    "flow60",
    "side_flow",
    "m_cover2_120",
    "m_width_ratio",
    "m_slope60_bps",
    "side_slope",
    "m_bandwalk10",
    "m_half_life_min",
    "ob_available_f",
    "ob_side_imb20",
    "ob_side_micro_bps",
    "ob_spread_bps",
    "confirm_adverse_bps",
    "consensus_votes",
    "vote_fresh_reentry",
    "vote_mild_tail",
    "vote_no_trend_pressure",
    "vote_flow_rejects_breakout",
    "vote_deep_fast_reentry",
    "edge_score",
    "ob_rejects_breakout_f",
    "trend_expansion_score",
]


def payout(won: bool) -> float:
    return WIN_PAY if bool(won) else LOSS_PAY


def finite(value: object) -> float:
    return v6.finite(value)


def clean_json_value(value):
    if isinstance(value, dict):
        return {str(k): clean_json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_json_value(v) for v in value]
    if isinstance(value, tuple):
        return [clean_json_value(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
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
        "calendar_days": int(calendar_days),
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


def state_gate_allows(row: dict, gate: str) -> bool:
    bandwalk = finite(row.get("m_bandwalk10"))
    sigma10 = finite(row.get("sigma10_bps"))
    if not np.isfinite(bandwalk):
        return False
    if gate == "capacity":
        return bandwalk < 6.0
    if gate == "v15":
        return (3.0 <= bandwalk < 6.0) or (bandwalk < 3.0 and np.isfinite(sigma10) and sigma10 > 18.0)
    raise ValueError(f"unknown state gate: {gate}")


def side_feature_values(row: dict) -> dict:
    side = finite(row.get("breakout_side"))
    return {
        "side": side,
        "side_flow": side * finite(row.get("flow60")),
        "side_slope": side * finite(row.get("m_slope60_bps")),
        "ob_side_imb20": side * finite(row.get("ob_imb20")),
        "ob_side_micro_bps": side * finite(row.get("ob_micro_bps")),
    }


def vote_set(row: dict) -> set[str]:
    raw = str(row.get("consensus_vote_names") or "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def trend_expansion_score(row: dict) -> float:
    sv = side_feature_values(row)
    width = finite(row.get("m_width_ratio"))
    bandwalk = finite(row.get("m_bandwalk10"))
    sigma10 = finite(row.get("sigma10_bps"))
    score = 0.0
    if np.isfinite(sv["side_slope"]) and sv["side_slope"] > 70:
        score += 1.0
    if np.isfinite(width) and width > 2.2:
        score += 1.0
    if np.isfinite(bandwalk) and bandwalk >= 6:
        score += 1.0
    if np.isfinite(sigma10) and sigma10 > 35:
        score += 1.0
    if np.isfinite(sv["side_flow"]) and sv["side_flow"] > 0:
        score += 1.0
    return score


def ob_rejects_breakout(row: dict) -> bool:
    return bool(v17.ob_rejects_breakout(row))


def edge_score(row: dict) -> float:
    return float(v17.edge_score(row))


def build_feature_row(row: dict) -> dict:
    sv = side_feature_values(row)
    votes = vote_set(row)
    out = {
        "side": sv["side"],
        "z": finite(row.get("z")),
        "peak_abs_z": finite(row.get("peak_abs_z")),
        "outside_sec": finite(row.get("outside_sec")),
        "sigma10_bps": finite(row.get("sigma10_bps")),
        "flow60": finite(row.get("flow60")),
        "side_flow": sv["side_flow"],
        "m_cover2_120": finite(row.get("m_cover2_120")),
        "m_width_ratio": finite(row.get("m_width_ratio")),
        "m_slope60_bps": finite(row.get("m_slope60_bps")),
        "side_slope": sv["side_slope"],
        "m_bandwalk10": finite(row.get("m_bandwalk10")),
        "m_half_life_min": finite(row.get("m_half_life_min")),
        "ob_available_f": 1.0 if bool(row.get("ob_available")) else 0.0,
        "ob_side_imb20": sv["ob_side_imb20"],
        "ob_side_micro_bps": sv["ob_side_micro_bps"],
        "ob_spread_bps": finite(row.get("ob_spread_bps")),
        "confirm_adverse_bps": finite(row.get("confirm_adverse_bps")),
        "consensus_votes": finite(row.get("consensus_votes")),
        "edge_score": edge_score(row),
        "ob_rejects_breakout_f": 1.0 if ob_rejects_breakout(row) else 0.0,
        "trend_expansion_score": trend_expansion_score(row),
    }
    for name in ["fresh_reentry", "mild_tail", "no_trend_pressure", "flow_rejects_breakout", "deep_fast_reentry"]:
        out[f"vote_{name}"] = 1.0 if name in votes else 0.0
    return out


def rule_for_side(side_mode: str) -> v6.RuleSpec:
    name = "V6_CONSENSUS_2OF5_UPPER" if side_mode == "upper_only" else "V6_CONSENSUS_2OF5_BOTH"
    spec = next((s for s in v6.rule_specs() if s.name == name), None)
    if spec is None:
        raise RuntimeError(f"{name} not found")
    return spec


def prepare_candidates(bars: pd.DataFrame, features: pd.DataFrame, ctx: pd.DataFrame, pool: CandidatePool) -> pd.DataFrame:
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
    spec = rule_for_side(pool.side_mode)
    rows = []
    for row in raw:
        annotated = v6.annotate_base_quality(row, pool.side_mode)
        if not annotated:
            continue
        ok, detail = v6.rule_allows(annotated, spec)
        if not ok:
            continue
        if not state_gate_allows(annotated, pool.state_gate):
            continue
        votes_n, votes = v6.consensus_votes(annotated)
        item = dict(annotated)
        item["pool"] = pool.key
        item["side_mode"] = pool.side_mode
        item["state_gate"] = pool.state_gate
        item["source_rule"] = spec.name
        item["rule_filter_detail"] = detail
        item["consensus_votes"] = votes_n
        item["consensus_vote_names"] = ",".join(votes)
        rows.append(item)
    confirmed, confirm_meta = v7.apply_confirmation(rows, bars, delay_sec=5, max_adverse_bps=5.0, cooldown_sec=0)
    for row in confirmed:
        row.update(build_feature_row(row))
        row["p_win"] = np.nan
        row["model_ready"] = False
        row["model_train_n"] = 0
        row["model_train_wr"] = np.nan
    df = pd.DataFrame(confirmed)
    if not df.empty:
        df = df.sort_values("idx").reset_index(drop=True)
    df.attrs["confirm_meta"] = confirm_meta
    return df


def make_model() -> Pipeline:
    return Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            (
                "clf",
                LogisticRegression(
                    C=0.5,
                    penalty="l2",
                    solver="liblinear",
                    class_weight="balanced",
                    max_iter=1000,
                    random_state=42,
                ),
            ),
        ]
    )


def add_walkforward_predictions(df: pd.DataFrame, *, min_train_n: int = 30) -> tuple[pd.DataFrame, list[dict]]:
    if df.empty:
        return df, []
    out = df.copy()
    diagnostics: list[dict] = []
    days = sorted(out["day_cn"].dropna().unique().tolist())
    for day in days:
        train_mask = out["day_cn"] < day
        pred_mask = out["day_cn"] == day
        train = out[train_mask].copy()
        pred = out[pred_mask].copy()
        if len(train) < min_train_n or train["won"].nunique() < 2 or pred.empty:
            diagnostics.append({"day": day, "ready": False, "train_n": int(len(train)), "pred_n": int(len(pred))})
            continue
        X = train[FEATURE_COLUMNS].astype(float)
        y = train["won"].astype(int)
        model = make_model()
        model.fit(X, y)
        p = model.predict_proba(pred[FEATURE_COLUMNS].astype(float))[:, 1]
        idxs = pred.index.to_list()
        out.loc[idxs, "p_win"] = p
        out.loc[idxs, "model_ready"] = True
        out.loc[idxs, "model_train_n"] = int(len(train))
        out.loc[idxs, "model_train_wr"] = round(float(y.mean() * 100.0), 2)
        clf = model.named_steps["clf"]
        coefs = dict(zip(FEATURE_COLUMNS, clf.coef_[0]))
        strongest = sorted(coefs.items(), key=lambda item: abs(item[1]), reverse=True)[:8]
        diagnostics.append(
            {
                "day": day,
                "ready": True,
                "train_n": int(len(train)),
                "train_wr": round(float(y.mean() * 100.0), 2),
                "pred_n": int(len(pred)),
                "p_min": round(float(np.min(p)), 4),
                "p_median": round(float(np.median(p)), 4),
                "p_max": round(float(np.max(p)), 4),
                "top_coef": [{"feature": name, "coef": round(float(coef), 5)} for name, coef in strongest],
            }
        )
    return out, diagnostics


def select_policy(df: pd.DataFrame, policy: MetaPolicy) -> tuple[pd.DataFrame, dict]:
    accepted: list[dict] = []
    open_rows: list[dict] = []
    last_idx = -10**9
    day_pnl: dict[str, float] = {}
    day_loss_streak: dict[str, int] = {}
    halted_days: set[str] = set()
    skipped = {
        "not_ready": 0,
        "threshold": 0,
        "gap": 0,
        "concurrency": 0,
        "daily_stop": 0,
        "loss_streak": 0,
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

    for _, row in df.sort_values("idx").iterrows():
        idx = int(row["idx"])
        day = str(row["day_cn"])
        settle_until(idx)
        if not bool(row.get("model_ready")):
            skipped["not_ready"] += 1
            continue
        if not np.isfinite(float(row.get("p_win", np.nan))) or float(row.get("p_win")) < policy.threshold:
            skipped["threshold"] += 1
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
        out = row.to_dict()
        out["policy"] = policy.key
        out["threshold"] = policy.threshold
        out["min_gap_sec"] = policy.min_gap_sec
        out["max_concurrent_limit"] = policy.max_concurrent
        out["open_positions_before"] = len(open_rows)
        out["day_closed_pnl_before"] = round(day_pnl.get(day, 0.0), 4)
        out["day_loss_streak_before"] = int(day_loss_streak.get(day, 0))
        accepted.append(out)
        open_rows.append(out)
        last_idx = idx
    return pd.DataFrame(accepted), skipped


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


def report_policy(policy: MetaPolicy, df: pd.DataFrame, skipped: dict, first_day: str, last_day: str) -> dict:
    parts = split_summaries(df, first_day, last_day)
    s = parts["summary"]
    train = parts["train"]
    valid = parts["valid"]
    latest = parts["latest"]
    flags = []
    if s["n"] < 30:
        flags.append("sample_under_30")
    if s["n"] < 50:
        flags.append("sample_under_50")
    if train["n"] < 10 or train["wr"] < 58.0 or train["pnl"] <= 0:
        flags.append("train_weak")
    if valid["n"] < 5 or valid["wr"] < 60.0 or valid["pnl"] <= 0:
        flags.append("valid_weak")
    if latest["n"] > 0 and (latest["wr"] < BREAKEVEN_WR or latest["pnl"] < 0):
        flags.append("latest_weak")
    if latest["n"] == 0:
        flags.append("no_latest_day_trade")
    if s["max_dd"] < -3.0:
        flags.append("max_dd_worse_than_3u")
    if s["losing_days"] > max(2, s["active_days"] // 3):
        flags.append("too_many_losing_days")
    if max_concurrent(df) > 1:
        flags.append("overlap")
    return {
        "policy": policy.key,
        "pool": policy.pool,
        "threshold": policy.threshold,
        "min_gap_sec": policy.min_gap_sec,
        "max_concurrent_limit": policy.max_concurrent,
        "max_concurrent_seen": max_concurrent(df),
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
        "down_n": int((df["signal"] == "DOWN").sum()) if not df.empty else 0,
        "up_n": int((df["signal"] == "UP").sum()) if not df.empty else 0,
        "avg_p_win": round(float(df["p_win"].mean()), 4) if not df.empty and "p_win" in df else 0.0,
        "risk_flags": ";".join(flags),
        "skipped": json.dumps(skipped, ensure_ascii=False, sort_keys=True),
        "days": s["days"],
    }


def baseline_policy(df: pd.DataFrame, pool_key: str, min_gap_sec: int, max_concurrent_limit: int = 1) -> pd.DataFrame:
    accepted = []
    open_rows = []
    last_idx = -10**9
    for _, row in df.sort_values("idx").iterrows():
        idx = int(row["idx"])
        open_rows = [r for r in open_rows if int(r["settle_idx"]) > idx]
        if idx - last_idx < min_gap_sec:
            continue
        if len(open_rows) >= max_concurrent_limit:
            continue
        out = row.to_dict()
        out["policy"] = f"baseline_{pool_key}_gap{min_gap_sec}"
        accepted.append(out)
        open_rows.append(out)
        last_idx = idx
    return pd.DataFrame(accepted)


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
    first_day = bars.index.min().tz_convert("Asia/Shanghai").strftime("%Y-%m-%d")
    last_day = bars.index.max().tz_convert("Asia/Shanghai").strftime("%Y-%m-%d")

    candidate_frames = []
    diagnostics: dict[str, list[dict]] = {}
    for pool in POOLS:
        df = prepare_candidates(bars, features, ctx, pool)
        predicted, diag = add_walkforward_predictions(df)
        diagnostics[pool.key] = diag
        candidate_frames.append(predicted)
    candidates = pd.concat(candidate_frames, ignore_index=True) if candidate_frames else pd.DataFrame()

    policy_rows = []
    trade_dfs = []

    # Baselines show the raw candidate-pool behavior before the meta gate.
    for pool in POOLS:
        pool_df = candidates[candidates["pool"] == pool.key].copy()
        for gap in [60, 600]:
            base_df = baseline_policy(pool_df, pool.key, gap, max_concurrent_limit=1)
            pol = MetaPolicy(f"baseline_{pool.key}_gap{gap}", pool.key, 0.0, gap, 1, None, None)
            policy_rows.append(report_policy(pol, base_df, {"baseline": 0}, first_day, last_day))
            if not base_df.empty:
                trade_dfs.append(base_df)

    for pool in POOLS:
        pool_df = candidates[candidates["pool"] == pool.key].copy()
        for threshold in THRESHOLDS:
            for gap in MIN_GAPS:
                for max_concurrent in MAX_CONCURRENCY:
                    policy = MetaPolicy(
                        key=f"v18_{pool.key}_p{int(threshold * 100)}_g{gap}_mc{max_concurrent}",
                        pool=pool.key,
                        threshold=threshold,
                        min_gap_sec=gap,
                        max_concurrent=max_concurrent,
                    )
                    selected, skipped = select_policy(pool_df, policy)
                    policy_rows.append(report_policy(policy, selected, skipped, first_day, last_day))
                    if not selected.empty:
                        trade_dfs.append(selected)

    table = pd.DataFrame(policy_rows)
    if not table.empty:
        table = table.sort_values(
            ["valid_pnl", "latest_pnl", "pnl", "n"],
            ascending=[False, False, False, False],
        )
        table = table.astype(object).where(pd.notna(table), None)
    trades = pd.concat(trade_dfs, ignore_index=True) if trade_dfs else pd.DataFrame()

    clean = table[
        ~table["risk_flags"].str.contains(
            "train_weak|valid_weak|latest_weak|max_dd_worse_than_3u|too_many_losing_days",
            regex=True,
            na=False,
        )
    ] if not table.empty else table
    higher_than_v15 = clean[clean["n"] > 31] if not clean.empty else clean
    recommended = higher_than_v15.iloc[0].to_dict() if not higher_than_v15.empty else {}

    OUT_POLICIES.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(OUT_POLICIES, index=False, encoding="utf-8-sig")
    trades.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    candidates.to_csv(OUT_CANDIDATES, index=False, encoding="utf-8-sig")

    candidate_summary = {}
    for pool in POOLS:
        pool_df = candidates[candidates["pool"] == pool.key].copy()
        ready_df = pool_df[pool_df["model_ready"].fillna(False).astype(bool)].copy()
        candidate_summary[pool.key] = {
            "n": int(len(pool_df)),
            "ready_n": int(len(ready_df)),
            "wr": summarize(pool_df, first_day=first_day, last_day=last_day)["wr"],
            "ready_wr": summarize(ready_df, first_day=first_day, last_day=last_day)["wr"],
            "p_win_min": round(float(ready_df["p_win"].min()), 4) if not ready_df.empty else 0.0,
            "p_win_median": round(float(ready_df["p_win"].median()), 4) if not ready_df.empty else 0.0,
            "p_win_max": round(float(ready_df["p_win"].max()), 4) if not ready_df.empty else 0.0,
        }

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
            "candidate": "V15/V17 normal false-break candidates, D5/A5 delayed entry, 10-minute settlement.",
            "meta_label": "won=True if the candidate wins after 10 minutes. The model predicts p_win.",
            "walk_forward": "For each trading day, train only on earlier days. No same-day/future labels are used.",
            "model": "L2 logistic regression with median imputation, standardization, and balanced class weights.",
            "feature_columns": FEATURE_COLUMNS,
        },
        "candidate_summary": candidate_summary,
        "model_diagnostics": diagnostics,
        "policy_table": table.to_dict("records") if not table.empty else [],
        "recommended_higher_frequency": recommended,
        "outputs": {
            "json": str(OUT_JSON),
            "policies_csv": str(OUT_POLICIES),
            "trades_csv": str(OUT_TRADES),
            "candidates_csv": str(OUT_CANDIDATES),
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
                    "candidate_summary": output["candidate_summary"],
                    "top": output["policy_table"][:20],
                    "recommended_higher_frequency": output["recommended_higher_frequency"],
                    "outputs": output["outputs"],
                }
            ),
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
    )
