from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_normal_state_v20_reversion_failure import attach_state_features, build_feature_arrays  # noqa: E402
from research_v21_good_regime_score import (  # noqa: E402
    DATA_ANCHOR,
    apply_market_state,
    base_candidate_allowed,
    day_health,
    finite_float,
    load_or_build_candidates,
    role_order,
    summarize,
)
from second_backtest.data import load_second_bars  # noqa: E402


OUT_JSON = ROOT / "tmp" / "v21_branch_state_veto_backtest.json"
OUT_TRADES = ROOT / "tmp" / "v21_branch_state_veto_trades.csv"
OUT_DAILY = ROOT / "tmp" / "v21_branch_state_veto_daily.csv"
OUT_VETO_BUCKETS = ROOT / "tmp" / "v21_branch_state_veto_buckets.csv"

GOOD_START = "2026-06-29"
GOOD_END = "2026-07-03"
LATEST_DAYS = {"2026-07-05", "2026-07-06"}


@dataclass(frozen=True)
class VetoPolicy:
    name: str
    vetoes: tuple[str, ...] = ()
    fallback_next_branch: bool = False


POLICIES = [
    VetoPolicy("baseline_v21_live_like"),
    VetoPolicy("veto_low_up_skip", ("low_up",), False),
    VetoPolicy("veto_low_up_fallback", ("low_up",), True),
    VetoPolicy("veto_low_up_transition_up_skip", ("low_up", "transition_up"), False),
    VetoPolicy("veto_low_up_transition_up_fallback", ("low_up", "transition_up"), True),
    VetoPolicy("veto_low_up_transition_up_high_trend_down_skip", ("low_up", "transition_up", "high_trend_down"), False),
    VetoPolicy("veto_low_up_transition_up_high_trend_down_fallback", ("low_up", "transition_up", "high_trend_down"), True),
]


def payout(won: bool) -> float:
    return 4.0 if bool(won) else -5.0


def veto_reason(row: dict[str, Any], vetoes: tuple[str, ...]) -> str | None:
    role = str(row.get("role") or "")
    signal = str(row.get("signal") or "")
    state = str(row.get("marketState") or "")
    if "low_up" in vetoes and role == "low" and signal == "UP":
        return "low_up"
    if "transition_up" in vetoes and state == "transition" and signal == "UP":
        return "transition_up"
    if "high_trend_down" in vetoes and role == "high" and state == "trend_walk" and signal == "DOWN":
        return "high_trend_down"
    return None


def choose_candidate(rows: list[dict[str, Any]], policy: VetoPolicy) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    route_sigma = finite_float(rows[0].get("routeSigma"))
    rejected = []
    for role in role_order(route_sigma):
        role_rows = [row for row in rows if str(row.get("role")) == role]
        if not role_rows:
            continue
        candidate = sorted(role_rows, key=lambda item: abs(finite_float(item.get("p_up"), 0.5) - 0.5), reverse=True)[0]
        ok, reason = base_candidate_allowed(candidate)
        if not ok:
            rejected.append({"reason": reason, "role": role})
            continue
        veto = veto_reason(candidate, policy.vetoes)
        if veto:
            rejected.append({"reason": f"veto_{veto}", "role": role, "signal": candidate.get("signal")})
            if policy.fallback_next_branch:
                continue
            return None, rejected
        return candidate, rejected
    return None, rejected


def select_policy(candidates: list[dict[str, Any]], policy: VetoPolicy) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_idx: dict[int, list[dict[str, Any]]] = {}
    for row in candidates:
        by_idx.setdefault(int(row["idx"]), []).append(row)

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    last_idx = -10**12
    cool_until = -10**12
    loss_streak = 0
    rolling: list[bool] = []

    for idx in sorted(by_idx):
        if idx - last_idx < 600:
            rejected.append({"idx": idx, "reason": "gap"})
            continue
        if idx < cool_until:
            rejected.append({"idx": idx, "reason": "loss_density_cooldown"})
            continue

        selected, local_rejected = choose_candidate(by_idx[idx], policy)
        for item in local_rejected:
            rejected.append({"idx": idx, **item})
        if selected is None:
            continue

        row = dict(selected)
        row["policy"] = policy.name
        accepted.append(row)
        last_idx = idx

        if bool(row["won"]):
            loss_streak = 0
        else:
            loss_streak += 1
            if loss_streak >= 2:
                cool_until = max(cool_until, idx + 3_600)
                loss_streak = 0

        rolling.append(bool(row["won"]))
        while len(rolling) > 6:
            rolling.pop(0)
        losses = sum(1 for won in rolling if not won)
        if len(rolling) >= 4 and losses >= 3:
            cool_until = max(cool_until, idx + 28_800)
            rolling = []

    return accepted, rejected


def split_summary(rows: list[dict[str, Any]], complete90_days: set[str]) -> dict[str, Any]:
    return {
        "all": summarize(rows),
        "complete90": summarize([row for row in rows if str(row.get("day")) in complete90_days]),
        "beforeGood": summarize([row for row in rows if str(row.get("day")) < GOOD_START]),
        "good_0629_0703": summarize([row for row in rows if GOOD_START <= str(row.get("day")) <= GOOD_END]),
        "latest_0705_0706": summarize([row for row in rows if str(row.get("day")) in LATEST_DAYS]),
    }


def veto_bucket_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    if not rows:
        return out
    frame = pd.DataFrame(rows)
    frame["wouldVetoLowUp"] = (frame["role"].astype(str) == "low") & (frame["signal"].astype(str) == "UP")
    frame["wouldVetoTransitionUp"] = (frame["marketState"].astype(str) == "transition") & (frame["signal"].astype(str) == "UP")
    frame["wouldVetoHighTrendDown"] = (
        (frame["role"].astype(str) == "high")
        & (frame["marketState"].astype(str) == "trend_walk")
        & (frame["signal"].astype(str) == "DOWN")
    )
    for column in ("wouldVetoLowUp", "wouldVetoTransitionUp", "wouldVetoHighTrendDown", "role", "marketState", "signal"):
        for value, group in frame.groupby(column, sort=True):
            summary = summarize(group.to_dict("records"))
            out.append(
                {
                    "bucketType": column,
                    "bucket": str(value),
                    "trades": summary["trades"],
                    "winRate": summary["winRate"],
                    "pnl": summary["pnl"],
                    "maxDrawdownU": summary["maxDrawdownU"],
                }
            )
    return out


def run() -> dict[str, Any]:
    bars = load_second_bars(DATA_ANCHOR, include_shards=True)
    health = day_health(bars)
    complete90_days = set(health["complete90Days"])

    candidates = load_or_build_candidates(bars)
    arrays = build_feature_arrays(bars)
    enriched = attach_state_features(candidates, arrays)
    enriched = apply_market_state(enriched)

    results = []
    all_trades = []
    all_daily = []
    for policy in POLICIES:
        rows, rejected = select_policy(enriched, policy)
        all_trades.extend(rows)
        summary = split_summary(rows, complete90_days)
        for day_row in summary["all"]["byDay"]:
            all_daily.append({"policy": policy.name, **day_row})
        results.append(
            {
                "policy": policy.__dict__,
                "summary": summary,
                "rejectReasons": pd.Series([row["reason"] for row in rejected]).value_counts().head(20).to_dict() if rejected else {},
            }
        )

    baseline = [row for row in all_trades if row.get("policy") == "baseline_v21_live_like"]
    pd.DataFrame(all_trades).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    pd.DataFrame(all_daily).to_csv(OUT_DAILY, index=False, encoding="utf-8-sig")
    pd.DataFrame(veto_bucket_rows(baseline)).to_csv(OUT_VETO_BUCKETS, index=False, encoding="utf-8-sig")

    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "data": {
            "anchor": str(DATA_ANCHOR),
            "bars": {
                "rows": int(len(bars)),
                "start": bars.index.min().isoformat(),
                "end": bars.index.max().isoformat(),
                "observedRows": int(bars["observed"].sum()) if "observed" in bars else int(len(bars)),
            },
            "candidateCount": int(len(candidates)),
            "health": health,
        },
        "results": results,
        "files": {
            "json": str(OUT_JSON),
            "trades": str(OUT_TRADES),
            "daily": str(OUT_DAILY),
            "vetoBuckets": str(OUT_VETO_BUCKETS),
        },
    }
    OUT_JSON.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    result = run()
    print(json.dumps(result["data"], ensure_ascii=False, indent=2))
    print("policy,all_trades,all_wr,all_pnl,all_dd,good_trades,good_wr,good_pnl,latest_trades,latest_wr,latest_pnl,complete90_trades,complete90_wr,complete90_pnl,complete90_dd")
    for item in result["results"]:
        parts = [item["policy"]["name"]]
        for key in ("all", "good_0629_0703", "latest_0705_0706", "complete90"):
            summary = item["summary"][key]
            if key in ("all", "complete90"):
                parts.extend([summary["trades"], summary["winRate"], summary["pnl"], summary["maxDrawdownU"]])
            else:
                parts.extend([summary["trades"], summary["winRate"], summary["pnl"]])
        print(",".join(str(part) for part in parts))
