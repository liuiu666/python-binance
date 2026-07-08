from __future__ import annotations

import json
import math
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_normal_state_v20_reversion_failure import (  # noqa: E402
    StateParams,
    attach_state_features,
    build_feature_arrays,
    classify_state,
)
from research_second_normal_drawdown_router import (  # noqa: E402
    build_candidates,
    clean,
    max_drawdown,
    max_loss_streak,
    payout,
)
from second_backtest.data import audit_second_sources, load_second_bars  # noqa: E402


DATA_ANCHOR = ROOT / "tmp" / "latest_pull_20260706_2130" / "data" / "btcusdt_1s_trades.csv"
CANDIDATE_CSV = ROOT / "tmp" / "latest_pull_20260706_2130" / "v21_router_candidates.csv"
OUT_CANDIDATES = ROOT / "tmp" / "v21_good_regime_score_candidates.csv"
OUT_JSON = ROOT / "tmp" / "v21_good_regime_score_backtest.json"
OUT_DAILY = ROOT / "tmp" / "v21_good_regime_score_daily.csv"
OUT_TRADES = ROOT / "tmp" / "v21_good_regime_score_trades.csv"
OUT_SCORE_BUCKETS = ROOT / "tmp" / "v21_good_regime_score_buckets.csv"

GOOD_START = "2026-06-29"
GOOD_END = "2026-07-03"
LATEST_START = "2026-07-05"
LATEST_END = "2026-07-06"


@dataclass(frozen=True)
class ScorePolicy:
    name: str
    min_score: int | None = None
    use_one_sided_penalty: bool = True
    select_best_score: bool = False
    hard_route_band: tuple[float, float] | None = None
    hard_r10_band: tuple[float, float] | None = None
    block_low_role: bool = False


POLICIES = [
    ScorePolicy("baseline_v21_live_like", min_score=None),
    ScorePolicy("score_ge3_soft", min_score=3),
    ScorePolicy("score_ge4_soft", min_score=4),
    ScorePolicy("score_ge5_soft", min_score=5),
    ScorePolicy("score_ge4_no_one_sided", min_score=4, use_one_sided_penalty=False),
    ScorePolicy("score_ge4_best_candidate", min_score=4, select_best_score=True),
    ScorePolicy("score_ge4_block_low", min_score=4, block_low_role=True),
    ScorePolicy("score_ge4_hard_good_band", min_score=4, hard_route_band=(10.0, 25.0), hard_r10_band=(24.0, 36.0)),
]


def role_order(route_sigma: float) -> list[str]:
    if route_sigma < 9.0:
        return ["low", "mid", "high"]
    if route_sigma >= 16.0:
        return ["high", "mid", "low"]
    if route_sigma < 22.0:
        return ["mid", "high", "low"]
    return ["high", "mid", "low"]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(rows, key=lambda r: int(r["idx"]))
    n = len(rows)
    wins = sum(1 for row in rows if bool(row["won"]))
    pnls = [payout(bool(row["won"])) for row in rows]
    by_day = []
    if rows:
        frame = pd.DataFrame(rows)
        for day, group in frame.groupby("day", sort=True):
            items = group.to_dict("records")
            gpnl = sum(payout(bool(row["won"])) for row in items)
            gwins = sum(1 for row in items if bool(row["won"]))
            by_day.append(
                {
                    "day": str(day),
                    "trades": int(len(items)),
                    "wins": int(gwins),
                    "losses": int(len(items) - gwins),
                    "winRate": round(gwins / len(items) * 100.0, 2) if items else 0.0,
                    "pnl": round(gpnl, 4),
                    "maxDrawdownU": max_drawdown([payout(bool(row["won"])) for row in items]),
                    "maxLoss": max_loss_streak(items),
                }
            )
    return {
        "trades": n,
        "wins": int(wins),
        "losses": int(n - wins),
        "winRate": round(wins / n * 100.0, 2) if n else 0.0,
        "pnl": round(sum(pnls), 4),
        "maxDrawdownU": max_drawdown(pnls),
        "maxLoss": max_loss_streak(rows),
        "activeDays": len(by_day),
        "tradesPerActiveDay": round(n / len(by_day), 2) if by_day else 0.0,
        "losingDays": sum(1 for row in by_day if float(row["pnl"]) < 0),
        "worstDay": min(by_day, key=lambda row: float(row["pnl"])) if by_day else None,
        "byDay": by_day,
    }


def day_health(bars: pd.DataFrame) -> dict[str, Any]:
    frame = bars.copy()
    frame["day"] = frame.index.strftime("%Y-%m-%d")
    rows = []
    for day, group in frame.groupby("day", sort=True):
        seconds = int(len(group))
        observed = int(group["observed"].sum()) if "observed" in group else seconds
        coverage = observed / max(seconds, 1) * 100.0
        rows.append(
            {
                "day": day,
                "seconds": seconds,
                "observed": observed,
                "coveragePct": round(coverage, 4),
                "fullDay": bool(seconds >= 80_000),
                "complete88": bool(seconds >= 80_000 and coverage >= 88.0),
                "complete90": bool(seconds >= 80_000 and coverage >= 90.0),
            }
        )
    return {
        "rows": rows,
        "complete88Days": [row["day"] for row in rows if row["complete88"]],
        "complete90Days": [row["day"] for row in rows if row["complete90"]],
    }


def add_causal_candidate_direction_share(rows: list[dict[str, Any]], window_sec: int = 21_600) -> list[dict[str, Any]]:
    by_idx: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_idx.setdefault(int(row["idx"]), []).append(row)

    history: deque[tuple[int, str]] = deque()
    up_count = 0
    down_count = 0
    enriched = []
    by_object_id: dict[int, dict[str, Any]] = {}

    for idx in sorted(by_idx):
        cutoff = idx - int(window_sec)
        while history and history[0][0] < cutoff:
            _, signal = history.popleft()
            if signal == "UP":
                up_count -= 1
            elif signal == "DOWN":
                down_count -= 1
        total = up_count + down_count
        up_share = up_count / total * 100.0 if total else None
        dominance = max(up_count, down_count) / total * 100.0 if total else None
        dominant_signal = None
        if total:
            dominant_signal = "UP" if up_count >= down_count else "DOWN"

        for row in by_idx[idx]:
            item = dict(row)
            item["recentCandidateWindowSec"] = int(window_sec)
            item["recentCandidateCount"] = int(total)
            item["recentUpSharePct"] = None if up_share is None else round(up_share, 6)
            item["recentDirectionDominancePct"] = None if dominance is None else round(dominance, 6)
            item["recentDominantSignal"] = dominant_signal
            enriched.append(item)
            by_object_id[id(row)] = item

        for row in by_idx[idx]:
            signal = str(row.get("signal"))
            if signal in ("UP", "DOWN"):
                history.append((idx, signal))
                if signal == "UP":
                    up_count += 1
                else:
                    down_count += 1
    return sorted(enriched, key=lambda row: (int(row["idx"]), str(row.get("role"))))


def finite_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return default
    return out if math.isfinite(out) else default


def good_regime_score(row: dict[str, Any], *, use_one_sided_penalty: bool = True) -> tuple[int, list[str]]:
    score = 0
    parts: list[str] = []

    observed = min(finite_float(row.get("observed600Pct")), finite_float(row.get("observedLookbackPct")))
    if observed >= 90.0:
        score += 1
        parts.append("覆盖>=90:+1")
    elif observed < 88.0:
        score -= 2
        parts.append("覆盖<88:-2")

    route_sigma = finite_float(row.get("routeSigma"))
    if 12.0 <= route_sigma <= 20.0:
        score += 2
        parts.append("routeSigma 12-20:+2")
    elif 10.0 <= route_sigma <= 25.0:
        score += 1
        parts.append("routeSigma 10-25:+1")

    r10 = finite_float(row.get("r10"))
    if 28.0 <= r10 <= 36.0:
        score += 2
        parts.append("r10 28-36:+2")
    elif 24.0 <= r10 <= 36.0:
        score += 1
        parts.append("r10 24-36:+1")

    role = str(row.get("role") or "")
    if role in ("high", "mid"):
        score += 1
        parts.append("high/mid:+1")
    elif role == "low":
        score -= 1
        parts.append("low:-1")

    state = str(row.get("marketState") or "")
    state_reason = str(row.get("marketStateReason") or "")
    if state == "normal_reversion":
        score += 2
        parts.append("normal_reversion:+2")
    elif state == "trend_walk" and state_reason == "edge_continuation":
        score += 2
        parts.append("mature edge_continuation:+2")
    elif state == "transition":
        score -= 1
        parts.append(f"transition/{state_reason}:-1")

    if use_one_sided_penalty:
        count = int(finite_float(row.get("recentCandidateCount")))
        dominance = row.get("recentDirectionDominancePct")
        dominance_value = None if dominance is None else finite_float(dominance)
        if count >= 50 and dominance_value is not None:
            if dominance_value >= 95.0:
                score -= 2
                parts.append("最近候选单边>=95%:-2")
            elif dominance_value >= 90.0:
                score -= 1
                parts.append("最近候选单边>=90%:-1")

    return int(score), parts


def apply_market_state(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    params = StateParams(name="good_regime_score_state")
    out = []
    for row in rows:
        state, reason = classify_state(row, params)
        item = dict(row)
        item["marketState"] = state
        item["marketStateReason"] = reason
        out.append(item)
    return out


def base_candidate_allowed(row: dict[str, Any]) -> tuple[bool, str]:
    if finite_float(row.get("observed600Pct")) < 88.0:
        return False, "entry_observed_low"
    if finite_float(row.get("observedLookbackPct")) < 88.0:
        return False, "lookback_observed_low"
    if finite_float(row.get("r10")) > 42.0:
        return False, "r10_cap"
    if str(row.get("signal")) == "DOWN" and finite_float(row.get("r10")) > 35.0:
        return False, "down_r10_cap"
    if str(row.get("role")) == "mid" and finite_float(row.get("routeSigma")) >= 20.0:
        return False, "mid_sigma_cap"
    return True, "pass"


def hard_band_allowed(row: dict[str, Any], policy: ScorePolicy) -> tuple[bool, str]:
    if policy.block_low_role and str(row.get("role")) == "low":
        return False, "score_block_low"
    if policy.hard_route_band is not None:
        lo, hi = policy.hard_route_band
        route_sigma = finite_float(row.get("routeSigma"))
        if not (lo <= route_sigma <= hi):
            return False, "score_route_band"
    if policy.hard_r10_band is not None:
        lo, hi = policy.hard_r10_band
        r10 = finite_float(row.get("r10"))
        if not (lo <= r10 <= hi):
            return False, "score_r10_band"
    return True, "pass"


def choose_candidate(rows: list[dict[str, Any]], policy: ScorePolicy) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    route_sigma = finite_float(rows[0].get("routeSigma"))
    candidates = []
    rejected = []

    for role_rank, role in enumerate(role_order(route_sigma)):
        role_rows = [row for row in rows if str(row.get("role")) == role]
        if not role_rows:
            continue
        row = sorted(role_rows, key=lambda item: abs(finite_float(item.get("p_up"), 0.5) - 0.5), reverse=True)[0]
        ok, reason = base_candidate_allowed(row)
        if not ok:
            rejected.append({"reason": reason, "role": role})
            continue

        score, parts = good_regime_score(row, use_one_sided_penalty=policy.use_one_sided_penalty)
        item = dict(row)
        item["goodRegimeScore"] = int(score)
        item["goodRegimeScoreParts"] = ";".join(parts)
        band_ok, band_reason = hard_band_allowed(item, policy)
        if not band_ok:
            rejected.append({"reason": band_reason, "role": role, "score": score})
            if not policy.select_best_score:
                return None, rejected
            continue
        if policy.min_score is not None and score < policy.min_score:
            rejected.append({"reason": "score_below_threshold", "role": role, "score": score})
            if not policy.select_best_score:
                return None, rejected
            continue

        item["routeRoleRank"] = int(role_rank)
        candidates.append(item)
        if not policy.select_best_score:
            return item, rejected

    if not candidates:
        return None, rejected
    return sorted(
        candidates,
        key=lambda item: (
            int(item.get("goodRegimeScore", -999)),
            abs(finite_float(item.get("p_up"), 0.5) - 0.5),
            -int(item.get("routeRoleRank", 99)),
        ),
        reverse=True,
    )[0], rejected


def select_policy(rows: list[dict[str, Any]], policy: ScorePolicy) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_idx: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_idx.setdefault(int(row["idx"]), []).append(row)

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    last_idx = -10**12
    cool_until = -10**12
    streak = 0
    rolling: list[bool] = []

    for idx in sorted(by_idx):
        if idx - last_idx < 600:
            rejected.append({"idx": idx, "reason": "gap"})
            continue
        if idx < cool_until:
            rejected.append({"idx": idx, "reason": "loss_density_cooldown"})
            continue

        selected, local_rejects = choose_candidate(by_idx[idx], policy)
        for item in local_rejects:
            rejected.append({"idx": idx, **item})
        if selected is None:
            continue

        row = dict(selected)
        row["policy"] = policy.name
        accepted.append(row)
        last_idx = idx

        if bool(row["won"]):
            streak = 0
        else:
            streak += 1
            if streak >= 2:
                cool_until = max(cool_until, idx + 3_600)
                streak = 0

        rolling.append(bool(row["won"]))
        while len(rolling) > 6:
            rolling.pop(0)
        losses = sum(1 for won in rolling if not won)
        if len(rolling) >= 4 and losses >= 3:
            cool_until = max(cool_until, idx + 28_800)
            rolling = []

    return accepted, rejected


def split_summary(rows: list[dict[str, Any]], complete88_days: set[str], complete90_days: set[str]) -> dict[str, Any]:
    return {
        "all": summarize(rows),
        "complete88": summarize([row for row in rows if str(row.get("day")) in complete88_days]),
        "complete90": summarize([row for row in rows if str(row.get("day")) in complete90_days]),
        "beforeGood": summarize([row for row in rows if str(row.get("day")) < GOOD_START]),
        "good_0629_0703": summarize([row for row in rows if GOOD_START <= str(row.get("day")) <= GOOD_END]),
        "latest_0705_0706": summarize([row for row in rows if LATEST_START <= str(row.get("day")) <= LATEST_END]),
    }


def bucket_rows(policy_name: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    if not rows:
        return out
    frame = pd.DataFrame(rows)
    for column in ("goodRegimeScore", "role", "marketState", "signal"):
        if column not in frame.columns:
            continue
        for value, group in frame.groupby(column, dropna=False, sort=True):
            summary = summarize(group.to_dict("records"))
            out.append(
                {
                    "policy": policy_name,
                    "bucketType": column,
                    "bucket": str(value),
                    "trades": summary["trades"],
                    "winRate": summary["winRate"],
                    "pnl": summary["pnl"],
                    "maxDrawdownU": summary["maxDrawdownU"],
                }
            )
    return out


def load_or_build_candidates(bars: pd.DataFrame) -> list[dict[str, Any]]:
    bar_days = set(pd.Series(bars.index.strftime("%Y-%m-%d")).unique())
    if OUT_CANDIDATES.exists():
        frame = pd.read_csv(OUT_CANDIDATES)
        candidate_days = set(frame["day"].astype(str).unique()) if "day" in frame.columns else set()
        if candidate_days and candidate_days.issubset(bar_days) and max(candidate_days) >= max(day for day in bar_days if day):
            return frame.to_dict("records")

    candidates = build_candidates(bars)
    pd.DataFrame(candidates).to_csv(OUT_CANDIDATES, index=False, encoding="utf-8-sig")
    return candidates


def run() -> dict[str, Any]:
    bars = load_second_bars(DATA_ANCHOR, include_shards=True)
    health = day_health(bars)
    complete88 = set(health["complete88Days"])
    complete90 = set(health["complete90Days"])

    candidates = load_or_build_candidates(bars)
    arrays = build_feature_arrays(bars)
    enriched = attach_state_features(candidates, arrays)
    enriched = apply_market_state(enriched)
    enriched = add_causal_candidate_direction_share(enriched)

    results = []
    all_daily = []
    all_trades = []
    all_buckets = []

    for policy in POLICIES:
        rows, rejected = select_policy(enriched, policy)
        all_trades.extend(rows)
        summary = split_summary(rows, complete88, complete90)
        for day_row in summary["all"]["byDay"]:
            all_daily.append({"policy": policy.name, **day_row})
        all_buckets.extend(bucket_rows(policy.name, rows))
        reject_counts = pd.Series([item["reason"] for item in rejected]).value_counts().head(20).to_dict() if rejected else {}
        results.append(
            {
                "policy": policy.__dict__,
                "summary": summary,
                "byRole": {role: summarize([row for row in rows if str(row.get("role")) == role]) for role in ("low", "mid", "high")},
                "byState": {
                    state: summarize([row for row in rows if str(row.get("marketState")) == state])
                    for state in ("normal_reversion", "transition", "trend_walk")
                },
                "rejectReasons": reject_counts,
            }
        )

    pd.DataFrame(all_daily).to_csv(OUT_DAILY, index=False, encoding="utf-8-sig")
    pd.DataFrame(all_trades).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    pd.DataFrame(all_buckets).to_csv(OUT_SCORE_BUCKETS, index=False, encoding="utf-8-sig")

    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "data": {
            "anchor": str(DATA_ANCHOR),
            "sourceCandidateCsv": str(CANDIDATE_CSV),
            "candidateCsv": str(OUT_CANDIDATES),
            "bars": {
                "rows": int(len(bars)),
                "start": bars.index.min().isoformat(),
                "end": bars.index.max().isoformat(),
                "observedRows": int(bars["observed"].sum()) if "observed" in bars else int(len(bars)),
            },
            "audit": audit_second_sources(DATA_ANCHOR, include_shards=True),
            "health": health,
            "candidateCount": int(len(candidates)),
            "enrichedCandidateCount": int(len(enriched)),
            "scoreNote": "Only data available at or before the candidate second is used for score. Outcome is used only for backtest settlement.",
        },
        "results": results,
        "files": {
            "json": str(OUT_JSON),
            "daily": str(OUT_DAILY),
            "trades": str(OUT_TRADES),
            "buckets": str(OUT_SCORE_BUCKETS),
        },
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    result = run()
    print(json.dumps(clean(result["data"]), ensure_ascii=False, indent=2))
    for item in result["results"]:
        s = item["summary"]
        print(
            item["policy"]["name"],
            "all",
            json.dumps(s["all"], ensure_ascii=False),
        )
        print("  good_0629_0703", json.dumps(s["good_0629_0703"], ensure_ascii=False))
        print("  latest_0705_0706", json.dumps(s["latest_0705_0706"], ensure_ascii=False))
        print("  complete90", json.dumps(s["complete90"], ensure_ascii=False))
