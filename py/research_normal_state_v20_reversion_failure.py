from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_second_normal_drawdown_router import (  # noqa: E402
    build_candidates,
    clean,
    max_drawdown,
    max_loss_streak,
    payout,
)
from second_backtest.data import load_second_bars  # noqa: E402


DATA_ANCHOR = ROOT / "tmp" / "server_second_shards_scan" / "btcusdt_1s_trades.csv"
CANDIDATE_CSV = ROOT / "tmp" / "second_normal_drawdown_router_candidates.csv"
OUT_JSON = ROOT / "tmp" / "normal_state_v20_reversion_failure.json"
OUT_TRADES = ROOT / "tmp" / "normal_state_v20_reversion_failure_trades.csv"
OUT_DAILY = ROOT / "tmp" / "normal_state_v20_reversion_failure_daily.csv"
OUT_BUCKETS = ROOT / "tmp" / "normal_state_v20_reversion_failure_buckets.csv"

RECENT_CUTOFF = "2026-06-29"


@dataclass(frozen=True)
class StateParams:
    name: str
    min_observed_pct: float = 88.0
    r10_cap: float = 42.0
    down_r10_cap: float = 35.0
    mid_route_sigma_cap: float = 20.0
    global_gap_sec: int = 600
    loss_cool_count: int = 2
    loss_cool_sec: int = 3600
    rolling_loss_window: int = 0
    rolling_loss_count: int = 0
    rolling_loss_cool_sec: int = 0
    trend_edge300_min: float = 90.0
    trend_edge120_min: float = 45.0
    trend_ret300_min: float = 3.0
    trend_ret180_min: float = 4.0
    trend_flow60_min: float = 0.05
    trend_eff180_min: float = 0.20
    transition_r10_min: float = 35.0
    transition_sigma_lo: float = 18.0
    transition_sigma_hi: float = 22.0
    transition_edge300_min: float = 60.0
    transition_min_edge: float = 0.10
    transition_max_adverse_flow60: float = 0.08
    transition_r10_cap: float = 40.0
    block_trend_walk: bool = True
    strict_transition: bool = True


PARAMS = [
    StateParams(
        name="reference_balanced_dd21",
        trend_edge300_min=10**9,
        trend_edge120_min=10**9,
        block_trend_walk=False,
        strict_transition=False,
    ),
    StateParams(name="v20_state_machine_base"),
    StateParams(
        name="v20_state_machine_quality",
        trend_edge300_min=70.0,
        trend_edge120_min=35.0,
        transition_r10_cap=38.0,
        transition_min_edge=0.12,
        transition_max_adverse_flow60=0.04,
    ),
    StateParams(
        name="v20_state_machine_capacity",
        min_observed_pct=85.0,
        r10_cap=45.0,
        down_r10_cap=38.0,
        transition_r10_cap=42.0,
        transition_min_edge=0.08,
        transition_max_adverse_flow60=0.10,
    ),
    StateParams(
        name="v21_reference_loss_density_3of6_2h",
        trend_edge300_min=10**9,
        trend_edge120_min=10**9,
        block_trend_walk=False,
        strict_transition=False,
        rolling_loss_window=6,
        rolling_loss_count=3,
        rolling_loss_cool_sec=7200,
    ),
    StateParams(
        name="v21_reference_loss_density_4of8_2h",
        trend_edge300_min=10**9,
        trend_edge120_min=10**9,
        block_trend_walk=False,
        strict_transition=False,
        rolling_loss_window=8,
        rolling_loss_count=4,
        rolling_loss_cool_sec=7200,
    ),
    StateParams(
        name="v21_reference_loss_density_4of8_4h",
        trend_edge300_min=10**9,
        trend_edge120_min=10**9,
        block_trend_walk=False,
        strict_transition=False,
        rolling_loss_window=8,
        rolling_loss_count=4,
        rolling_loss_cool_sec=14400,
    ),
    StateParams(
        name="v21_reference_loss_density_3of6_6h",
        trend_edge300_min=10**9,
        trend_edge120_min=10**9,
        block_trend_walk=False,
        strict_transition=False,
        rolling_loss_window=6,
        rolling_loss_count=3,
        rolling_loss_cool_sec=21600,
    ),
    StateParams(
        name="v21_reference_loss_density_3of6_8h",
        trend_edge300_min=10**9,
        trend_edge120_min=10**9,
        block_trend_walk=False,
        strict_transition=False,
        rolling_loss_window=6,
        rolling_loss_count=3,
        rolling_loss_cool_sec=28800,
    ),
    StateParams(
        name="v21_reference_loss_density_5of10_6h",
        trend_edge300_min=10**9,
        trend_edge120_min=10**9,
        block_trend_walk=False,
        strict_transition=False,
        rolling_loss_window=10,
        rolling_loss_count=5,
        rolling_loss_cool_sec=21600,
    ),
    StateParams(
        name="v21_reference_loss_density_5of10_4h",
        trend_edge300_min=10**9,
        trend_edge120_min=10**9,
        block_trend_walk=False,
        strict_transition=False,
        rolling_loss_window=10,
        rolling_loss_count=5,
        rolling_loss_cool_sec=14400,
    ),
]


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
                    "winRate": round(gwins / len(items) * 100.0, 2) if items else 0.0,
                    "pnl": round(gpnl, 4),
                    "maxDrawdownU": max_drawdown([payout(bool(row["won"])) for row in items]),
                    "maxLoss": max_loss_streak(items),
                }
            )
    return {
        "trades": n,
        "wins": int(wins),
        "winRate": round(wins / n * 100.0, 2) if n else 0.0,
        "pnl": round(sum(pnls), 4),
        "maxDrawdownU": max_drawdown(pnls),
        "maxLoss": max_loss_streak(rows),
        "activeDays": len(by_day),
        "tradesPerActiveDay": round(n / len(by_day), 2) if by_day else 0.0,
        "losingDays": sum(1 for d in by_day if float(d["pnl"]) < 0),
        "worstDay": min(by_day, key=lambda d: float(d["pnl"])) if by_day else None,
        "byDay": by_day,
    }


def _rolling_sum(values: np.ndarray, window: int, min_periods: int = 1) -> np.ndarray:
    return pd.Series(values).rolling(window, min_periods=min_periods).sum().to_numpy(float)


def _rolling_mean(values: np.ndarray, window: int, min_periods: int = 1) -> np.ndarray:
    return pd.Series(values).rolling(window, min_periods=min_periods).mean().to_numpy(float)


def _rolling_std(values: np.ndarray, window: int, min_periods: int = 1) -> np.ndarray:
    return pd.Series(values).rolling(window, min_periods=min_periods).std(ddof=1).to_numpy(float)


def _rolling_max(values: np.ndarray, window: int, min_periods: int = 1) -> np.ndarray:
    return pd.Series(values).rolling(window, min_periods=min_periods).max().to_numpy(float)


def _rolling_min(values: np.ndarray, window: int, min_periods: int = 1) -> np.ndarray:
    return pd.Series(values).rolling(window, min_periods=min_periods).min().to_numpy(float)


def _ret_bps(close: np.ndarray, window: int) -> np.ndarray:
    out = np.full(len(close), np.nan, dtype=float)
    if window <= 0 or len(close) <= window:
        return out
    out[window:] = (close[window:] / close[:-window] - 1.0) * 10000.0
    return out


def _future_observed_pct(observed: np.ndarray, horizon: int) -> np.ndarray:
    values = observed.astype(float)
    csum = np.concatenate([[0.0], np.cumsum(values)])
    out = np.full(len(values), np.nan, dtype=float)
    end = np.arange(len(values)) + horizon + 1
    ok = end <= len(values)
    idx = np.arange(len(values))[ok]
    out[idx] = (csum[end[ok]] - csum[idx + 1]) / horizon * 100.0
    return out


def build_feature_arrays(bars: pd.DataFrame) -> dict[str, np.ndarray]:
    close = bars["close"].to_numpy(float)
    observed = bars["observed"].to_numpy(bool) if "observed" in bars else np.ones(len(close), dtype=bool)
    buy = bars["buy_qty"].to_numpy(float) if "buy_qty" in bars else np.zeros(len(close), dtype=float)
    sell = bars["sell_qty"].to_numpy(float) if "sell_qty" in bars else np.zeros(len(close), dtype=float)

    logp = np.log(close)
    ret1 = np.diff(logp, prepend=np.nan) * 10000.0
    abs_ret1 = np.abs(ret1)

    mean600 = _rolling_mean(close, 600, 120)
    std600 = _rolling_std(close, 600, 120)
    z600 = (close - mean600) / np.maximum(std600, 1e-12)
    high600 = _rolling_max(close, 600, 120)
    low600 = _rolling_min(close, 600, 120)

    vol60 = _rolling_sum(buy + sell, 60, 1)
    flow60 = _rolling_sum(buy - sell, 60, 1) / np.maximum(vol60, 1e-12)
    vol180 = _rolling_sum(buy + sell, 180, 1)
    flow180 = _rolling_sum(buy - sell, 180, 1) / np.maximum(vol180, 1e-12)

    path180 = _rolling_sum(abs_ret1, 180, 30)
    path300 = _rolling_sum(abs_ret1, 300, 60)

    return {
        "close": close,
        "observed": observed,
        "futureObserved600Pct": _future_observed_pct(observed, 600),
        "ret60": _ret_bps(close, 60),
        "ret180": _ret_bps(close, 180),
        "ret300": _ret_bps(close, 300),
        "ret600": _ret_bps(close, 600),
        "z600": z600,
        "edgeUp120": _rolling_sum((z600 > 1.0).astype(float), 120, 30),
        "edgeDown120": _rolling_sum((z600 < -1.0).astype(float), 120, 30),
        "edgeUp300": _rolling_sum((z600 > 1.0).astype(float), 300, 60),
        "edgeDown300": _rolling_sum((z600 < -1.0).astype(float), 300, 60),
        "distHigh600Bps": (high600 - close) / np.maximum(close, 1e-12) * 10000.0,
        "distLow600Bps": (close - low600) / np.maximum(close, 1e-12) * 10000.0,
        "flow60": flow60,
        "flow180": flow180,
        "eff180": _ret_bps(close, 180) / np.maximum(path180, 1e-12),
        "eff300": _ret_bps(close, 300) / np.maximum(path300, 1e-12),
    }


def attach_state_features(candidates: list[dict[str, Any]], arrays: dict[str, np.ndarray]) -> list[dict[str, Any]]:
    out = []
    n = len(arrays["close"])
    for row in candidates:
        idx = int(row["idx"])
        if idx < 0 or idx >= n:
            continue
        signal = str(row.get("signal"))
        side = 1.0 if signal == "DOWN" else -1.0
        if signal == "DOWN":
            edge120 = arrays["edgeUp120"][idx]
            edge300 = arrays["edgeUp300"][idx]
            dist_edge = arrays["distHigh600Bps"][idx]
        else:
            edge120 = arrays["edgeDown120"][idx]
            edge300 = arrays["edgeDown300"][idx]
            dist_edge = arrays["distLow600Bps"][idx]
        item = dict(row)
        item.update(
            {
                "futureObserved600Pct": _finite_round(arrays["futureObserved600Pct"][idx]),
                "adverseRet60Bps": _finite_round(side * arrays["ret60"][idx]),
                "adverseRet180Bps": _finite_round(side * arrays["ret180"][idx]),
                "adverseRet300Bps": _finite_round(side * arrays["ret300"][idx]),
                "adverseRet600Bps": _finite_round(side * arrays["ret600"][idx]),
                "edgePersist120": _finite_round(edge120),
                "edgePersist300": _finite_round(edge300),
                "distFromEdge600Bps": _finite_round(dist_edge),
                "adverseFlow60": _finite_round(side * arrays["flow60"][idx]),
                "adverseFlow180": _finite_round(side * arrays["flow180"][idx]),
                "adverseEff180": _finite_round(side * arrays["eff180"][idx]),
                "adverseEff300": _finite_round(side * arrays["eff300"][idx]),
                "absSignalEdge": _finite_round(abs(float(row.get("p_up", 0.5)) - 0.5)),
            }
        )
        out.append(item)
    return out


def _finite_round(value: Any, digits: int = 6) -> float | None:
    try:
        value = float(value)
    except Exception:
        return None
    if not math.isfinite(value):
        return None
    return round(value, digits)


def classify_state(row: dict[str, Any], params: StateParams) -> tuple[str, str]:
    edge300 = float(row.get("edgePersist300") or 0.0)
    edge120 = float(row.get("edgePersist120") or 0.0)
    ret300 = float(row.get("adverseRet300Bps") or 0.0)
    ret180 = float(row.get("adverseRet180Bps") or 0.0)
    flow60 = float(row.get("adverseFlow60") or 0.0)
    eff180 = float(row.get("adverseEff180") or 0.0)
    r10 = float(row.get("r10") or 0.0)
    route_sigma = float(row.get("routeSigma") or 0.0)

    persistent_walk = edge300 >= params.trend_edge300_min and ret300 >= params.trend_ret300_min
    fast_walk = edge120 >= params.trend_edge120_min and ret180 >= params.trend_ret180_min and flow60 >= params.trend_flow60_min
    efficient_walk = ret180 >= params.trend_ret180_min and eff180 >= params.trend_eff180_min and flow60 >= params.trend_flow60_min
    if persistent_walk or fast_walk or efficient_walk:
        return "trend_walk", "edge_continuation"

    transition_sigma = params.transition_sigma_lo <= route_sigma < params.transition_sigma_hi and r10 >= params.transition_r10_min
    transition_edge = edge300 >= params.transition_edge300_min
    if transition_sigma or transition_edge:
        return "transition", "sigma_r10" if transition_sigma else "edge_building"

    return "normal_reversion", "normal"


def role_order(route_sigma: float) -> list[str]:
    if route_sigma < 9.0:
        return ["low", "mid", "high"]
    if route_sigma >= 16.0:
        return ["high", "mid", "low"]
    if route_sigma < 22.0:
        return ["mid", "high", "low"]
    return ["high", "mid", "low"]


def select_policy(candidates: list[dict[str, Any]], params: StateParams, allowed_days: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_idx: dict[int, list[dict[str, Any]]] = {}
    for row in candidates:
        day = str(row.get("day"))
        if day not in allowed_days:
            continue
        by_idx.setdefault(int(row["idx"]), []).append(row)

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    last_idx = -10**12
    loss_streak = 0
    cool_until = -10**12
    rolling_cool_until = -10**12
    recent_outcomes: list[bool] = []

    for idx in sorted(by_idx):
        rows = by_idx[idx]
        route_sigma = float(rows[0].get("routeSigma") or 0.0)
        if idx - last_idx < params.global_gap_sec:
            rejected.append({"idx": idx, "reason": "gap"})
            continue
        if idx < cool_until:
            rejected.append({"idx": idx, "reason": "loss_cooldown"})
            continue
        if idx < rolling_cool_until:
            rejected.append({"idx": idx, "reason": "rolling_loss_density_cooldown"})
            continue

        selected = None
        selected_state = None
        selected_reason = None
        for role in role_order(route_sigma):
            role_rows = [row for row in rows if row.get("role") == role]
            if not role_rows:
                continue
            candidate = sorted(role_rows, key=lambda r: float(r.get("absSignalEdge") or 0.0), reverse=True)[0]
            ok, reason = policy_allows(candidate, params)
            if not ok:
                rejected.append({"idx": idx, "reason": reason, "role": role, "signal": candidate.get("signal")})
                continue
            selected = candidate
            selected_state, selected_reason = classify_state(candidate, params)
            break
        if selected is None:
            continue

        row = dict(selected)
        row["policy"] = params.name
        row["marketState"], row["marketStateReason"] = selected_state, selected_reason
        accepted.append(row)
        last_idx = idx

        if bool(row["won"]):
            loss_streak = 0
        else:
            loss_streak += 1
            if params.loss_cool_count and loss_streak >= params.loss_cool_count:
                cool_until = idx + int(params.loss_cool_sec)
                loss_streak = 0
        if params.rolling_loss_window > 0 and params.rolling_loss_count > 0:
            recent_outcomes.append(bool(row["won"]))
            recent_outcomes = recent_outcomes[-int(params.rolling_loss_window) :]
            losses = sum(1 for won in recent_outcomes if not won)
            if len(recent_outcomes) >= int(params.rolling_loss_window) and losses >= int(params.rolling_loss_count):
                rolling_cool_until = idx + int(params.rolling_loss_cool_sec)
                recent_outcomes = []
    return accepted, rejected


def policy_allows(row: dict[str, Any], params: StateParams) -> tuple[bool, str]:
    if float(row.get("observed600Pct") or 0.0) < params.min_observed_pct:
        return False, "entry_observed_low"
    if float(row.get("observedLookbackPct") or 0.0) < params.min_observed_pct:
        return False, "lookback_observed_low"
    future_obs = row.get("futureObserved600Pct")
    if future_obs is not None and float(future_obs) < params.min_observed_pct:
        return False, "settle_window_observed_low"
    if float(row.get("r10") or 0.0) > params.r10_cap:
        return False, "r10_cap"
    if str(row.get("signal")) == "DOWN" and float(row.get("r10") or 0.0) > params.down_r10_cap:
        return False, "down_r10_cap"
    if str(row.get("role")) == "mid" and float(row.get("routeSigma") or 0.0) >= params.mid_route_sigma_cap:
        return False, "mid_sigma_cap"

    state, state_reason = classify_state(row, params)
    if params.block_trend_walk and state == "trend_walk":
        return False, f"trend_walk:{state_reason}"
    if params.strict_transition and state == "transition":
        if float(row.get("r10") or 0.0) > params.transition_r10_cap:
            return False, "transition_r10_cap"
        if float(row.get("absSignalEdge") or 0.0) < params.transition_min_edge:
            return False, "transition_signal_edge"
        if float(row.get("adverseFlow60") or 0.0) > params.transition_max_adverse_flow60:
            return False, "transition_adverse_flow"
    return True, "pass"


def bucket_report(rows: list[dict[str, Any]], params: StateParams) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        state, reason = classify_state(row, params)
        key = f"{state}:{reason}"
        buckets.setdefault(key, []).append(row)
    out = []
    for key, items in sorted(buckets.items()):
        summary = summarize(items)
        out.append(
            {
                "bucket": key,
                "trades": summary["trades"],
                "winRate": summary["winRate"],
                "pnl": summary["pnl"],
                "maxDrawdownU": summary["maxDrawdownU"],
                "maxLoss": summary["maxLoss"],
            }
        )
    return out


def healthy_days(bars: pd.DataFrame) -> dict[str, Any]:
    frame = bars.copy()
    frame["day"] = frame.index.strftime("%Y-%m-%d")
    rows = []
    for day, group in frame.groupby("day", sort=True):
        seconds = len(group)
        observed = int(group["observed"].sum()) if "observed" in group else seconds
        coverage = observed / max(seconds, 1) * 100.0
        fullish = seconds >= 80_000
        rows.append(
            {
                "day": day,
                "seconds": int(seconds),
                "observed": int(observed),
                "coveragePct": round(coverage, 4),
                "fullish": bool(fullish),
                "healthyForDailyStats": bool(fullish and coverage >= 88.0),
            }
        )
    allowed = {row["day"] for row in rows if row["healthyForDailyStats"]}
    return {"rows": rows, "allowedDays": sorted(allowed)}


def load_or_build_candidates(bars: pd.DataFrame) -> list[dict[str, Any]]:
    if CANDIDATE_CSV.exists():
        return pd.read_csv(CANDIDATE_CSV).to_dict("records")
    candidates = build_candidates(bars)
    pd.DataFrame(candidates).to_csv(CANDIDATE_CSV, index=False, encoding="utf-8-sig")
    return candidates


def run() -> dict[str, Any]:
    bars = load_second_bars(DATA_ANCHOR, include_shards=True)
    day_health = healthy_days(bars)
    allowed_days = set(day_health["allowedDays"])
    candidates = load_or_build_candidates(bars)
    arrays = build_feature_arrays(bars)
    enriched = attach_state_features(candidates, arrays)

    results = []
    all_daily = []
    all_bucket_rows = []
    selected_for_csv: list[dict[str, Any]] = []
    for params in PARAMS:
        rows, rejected = select_policy(enriched, params, allowed_days)
        if params.name == "v21_reference_loss_density_3of6_8h":
            selected_for_csv = rows
        train = [row for row in rows if str(row["day"]) < RECENT_CUTOFF]
        recent = [row for row in rows if str(row["day"]) >= RECENT_CUTOFF]
        by_state = {state: summarize([row for row in rows if row.get("marketState") == state]) for state in ("normal_reversion", "transition", "trend_walk")}
        bucket_rows = bucket_report(
            [
                row
                for row in enriched
                if str(row.get("day")) in allowed_days
                and float(row.get("observed600Pct") or 0.0) >= params.min_observed_pct
                and float(row.get("observedLookbackPct") or 0.0) >= params.min_observed_pct
                and (row.get("futureObserved600Pct") is None or float(row.get("futureObserved600Pct") or 0.0) >= params.min_observed_pct)
            ],
            params,
        )
        for item in bucket_rows:
            all_bucket_rows.append({"policy": params.name, **item})
        summary = summarize(rows)
        for day_row in summary["byDay"]:
            all_daily.append({"policy": params.name, **day_row})
        results.append(
            {
                "policy": params.__dict__,
                "total": summary,
                "train": summarize(train),
                "recent": summarize(recent),
                "byMarketState": by_state,
                "rejectReasons": pd.Series([row["reason"] for row in rejected]).value_counts().head(20).to_dict() if rejected else {},
            }
        )

    if selected_for_csv:
        pd.DataFrame(selected_for_csv).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    pd.DataFrame(all_daily).to_csv(OUT_DAILY, index=False, encoding="utf-8-sig")
    pd.DataFrame(all_bucket_rows).to_csv(OUT_BUCKETS, index=False, encoding="utf-8-sig")

    output = {
        "data": {
            "anchor": str(DATA_ANCHOR),
            "rows": int(len(bars)),
            "start": bars.index.min().isoformat(),
            "end": bars.index.max().isoformat(),
            "candidateCount": int(len(candidates)),
            "enrichedCandidateCount": int(len(enriched)),
            "dayHealth": day_health,
            "recentCutoff": RECENT_CUTOFF,
        },
        "results": results,
        "files": {
            "json": str(OUT_JSON),
            "trades": str(OUT_TRADES),
            "daily": str(OUT_DAILY),
            "buckets": str(OUT_BUCKETS),
        },
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    result = run()
    print(json.dumps(clean(result["data"]), ensure_ascii=False, indent=2))
    for item in result["results"]:
        print(item["policy"]["name"], json.dumps(item["total"], ensure_ascii=False))
        print("  train ", json.dumps(item["train"], ensure_ascii=False))
        print("  recent", json.dumps(item["recent"], ensure_ascii=False))
        print("  reject", json.dumps(item["rejectReasons"], ensure_ascii=False))
