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

from second_backtest.data import load_second_bars
from second_backtest.strategies import SecondNormalConfig, generate_normal_signals


DATA_ANCHOR = ROOT / "tmp" / "server_second_shards_scan" / "btcusdt_1s_trades.csv"
OUT_JSON = ROOT / "tmp" / "second_normal_drawdown_router_v2.json"
OUT_TRADES = ROOT / "tmp" / "second_normal_drawdown_router_v2_trades.csv"
OUT_CANDIDATES = ROOT / "tmp" / "second_normal_drawdown_router_candidates.csv"

WIN_PAY = 4.0
LOSS_PAY = -5.0
RECENT_CUTOFF = "2026-06-29"


@dataclass(frozen=True)
class Branch:
    role: str
    name: str
    cfg: SecondNormalConfig


BRANCHES = [
    Branch(
        "low",
        "LOW_L4200_T25_S4_18_DYN",
        SecondNormalConfig(
            strategy_id="LOW_L4200_T25_S4_18_DYN",
            lookback_sec=4200,
            horizon_sec=600,
            signal_gap_sec=0,
            tail_pct=0.25,
            sigma_min_bps=4.0,
            sigma_max_bps=18.0,
            second_filter="none",
            zone_filter="dynamic_v3",
            amount=5.0,
        ),
    ),
    Branch(
        "mid",
        "MID_L4200_T25_S10_25_DYN",
        SecondNormalConfig(
            strategy_id="MID_L4200_T25_S10_25_DYN",
            lookback_sec=4200,
            horizon_sec=600,
            signal_gap_sec=0,
            tail_pct=0.25,
            sigma_min_bps=10.0,
            sigma_max_bps=25.0,
            second_filter="none",
            zone_filter="dynamic_v3",
            amount=5.0,
        ),
    ),
    Branch(
        "high",
        "HIGH_L2700_T25_S14_35_FLOW_DYN",
        SecondNormalConfig(
            strategy_id="HIGH_L2700_T25_S14_35_FLOW_DYN",
            lookback_sec=2700,
            horizon_sec=600,
            signal_gap_sec=0,
            tail_pct=0.25,
            sigma_min_bps=14.0,
            sigma_max_bps=35.0,
            second_filter="flow_reversal",
            zone_filter="dynamic_v3",
            amount=5.0,
        ),
    ),
]


def payout(won: bool) -> float:
    return WIN_PAY if bool(won) else LOSS_PAY


def max_drawdown(values: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for value in values:
        equity += float(value)
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return round(worst, 4)


def max_loss_streak(rows: list[dict[str, Any]]) -> int:
    cur = 0
    worst = 0
    for row in rows:
        if row["won"]:
            cur = 0
        else:
            cur += 1
            worst = max(worst, cur)
    return worst


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(rows, key=lambda r: int(r["idx"]))
    n = len(rows)
    wins = sum(1 for row in rows if row["won"])
    pnls = [payout(row["won"]) for row in rows]
    by_day = []
    for day, group in pd.DataFrame(rows).groupby("day", sort=True) if rows else []:
        items = group.to_dict("records")
        gpnl = sum(payout(row["won"]) for row in items)
        gwins = sum(1 for row in items if row["won"])
        by_day.append(
            {
                "day": str(day),
                "trades": int(len(items)),
                "winRate": round(gwins / len(items) * 100.0, 2) if items else 0.0,
                "pnl": round(gpnl, 4),
                "maxDrawdownU": max_drawdown([payout(row["won"]) for row in items]),
                "maxLoss": max_loss_streak(items),
            }
        )
    worst_day = min(by_day, key=lambda d: float(d["pnl"])) if by_day else None
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
        "worstDay": worst_day,
        "byDay": by_day,
    }


def rolling_sigma_bps(close: np.ndarray, window: int, horizon: int = 600) -> np.ndarray:
    logp = np.log(close)
    lr = pd.Series(np.diff(logp, prepend=np.nan))
    sigma = lr.rolling(window, min_periods=max(60, window // 4)).std(ddof=1).to_numpy()
    return np.sqrt(horizon) * sigma * 10000.0


def rolling_range_bps(close: np.ndarray, window: int = 600) -> np.ndarray:
    series = pd.Series(close)
    hi = series.rolling(window, min_periods=max(60, window // 4)).max().to_numpy()
    lo = series.rolling(window, min_periods=max(60, window // 4)).min().to_numpy()
    out = np.full(len(close), np.nan, dtype=float)
    np.divide(hi - lo, close, out=out, where=close > 0)
    return out * 10000.0


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, tuple):
        return [clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        value = float(value)
        return value if math.isfinite(value) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def build_candidates(bars: pd.DataFrame) -> list[dict[str, Any]]:
    close = bars["close"].to_numpy(float)
    observed = bars["observed"].astype(float).to_numpy() if "observed" in bars else np.ones(len(bars), dtype=float)
    route_sigma = rolling_sigma_bps(close, 4200)
    r10 = rolling_range_bps(close, 600)
    obs600 = pd.Series(observed).rolling(600, min_periods=1).mean().to_numpy() * 100.0
    obs2700 = pd.Series(observed).rolling(2700, min_periods=1).mean().to_numpy() * 100.0
    obs4200 = pd.Series(observed).rolling(4200, min_periods=1).mean().to_numpy() * 100.0
    candidates: list[dict[str, Any]] = []
    for branch in BRANCHES:
        signals = generate_normal_signals(bars, branch.cfg, apply_config_gap=False)
        for sig in signals:
            idx = int(sig["idx"])
            if idx < 0 or idx >= len(close):
                continue
            rs = float(route_sigma[idx])
            rr = float(r10[idx])
            if not math.isfinite(rs) or not math.isfinite(rr):
                continue
            row = dict(sig)
            row.update(
                {
                    "role": branch.role,
                    "branch": branch.name,
                    "routeSigma": round(rs, 6),
                    "r10": round(rr, 6),
                    "observed600Pct": round(float(obs600[idx]), 6),
                    "observedLookbackPct": round(float(obs2700[idx] if branch.cfg.lookback_sec == 2700 else obs4200[idx]), 6),
                    "day": str(pd.Timestamp(sig["time"]).date()),
                    "timeStr": pd.Timestamp(sig["time"]).isoformat(),
                    "pnl": payout(bool(sig["won"])),
                }
            )
            candidates.append(row)
    return sorted(candidates, key=lambda r: (int(r["idx"]), r["role"]))


def role_order(route_sigma: float, *, low_hi: float, mid_hi: float, high_lo: float) -> list[str]:
    if route_sigma < low_hi:
        return ["low", "mid", "high"]
    if route_sigma >= high_lo:
        return ["high", "mid", "low"]
    if route_sigma < mid_hi:
        return ["mid", "high", "low"]
    return ["high", "mid", "low"]


def select_router(
    candidates: list[dict[str, Any]],
    *,
    low_hi: float = 9.0,
    mid_hi: float = 22.0,
    high_lo: float = 16.0,
    r10_cap: float = 45.0,
    mid_route_sigma_cap: float | None = None,
    down_r10_cap: float | None = None,
    block_down_sigma_band: tuple[float, float] | None = None,
    branch_loss_cool_count: int = 0,
    branch_loss_cool_sec: int = 0,
    global_loss_cool_count: int = 0,
    global_loss_cool_sec: int = 0,
    global_gap_sec: int = 600,
    min_observed_600_pct: float = 0.0,
    min_observed_lookback_pct: float = 0.0,
    allowed_days: set[str] | None = None,
) -> list[dict[str, Any]]:
    by_idx: dict[int, list[dict[str, Any]]] = {}
    for row in candidates:
        by_idx.setdefault(int(row["idx"]), []).append(row)

    accepted: list[dict[str, Any]] = []
    global_losses = 0
    global_cool_until = -10**12
    branch_losses = {"low": 0, "mid": 0, "high": 0}
    branch_cool_until = {"low": -10**12, "mid": -10**12, "high": -10**12}
    last_idx = -10**12

    for idx in sorted(by_idx):
        if idx - last_idx < global_gap_sec:
            continue
        if idx < global_cool_until:
            continue
        rows = by_idx[idx]
        route_sigma = float(rows[0]["routeSigma"])
        if allowed_days is not None and rows[0]["day"] not in allowed_days:
            continue
        selected = None
        for role in role_order(route_sigma, low_hi=low_hi, mid_hi=mid_hi, high_lo=high_lo):
            if idx < branch_cool_until[role]:
                continue
            role_rows = [row for row in rows if row["role"] == role]
            if not role_rows:
                continue
            candidate = sorted(role_rows, key=lambda r: abs(float(r.get("p_up", 0.5)) - 0.5), reverse=True)[0]
            if float(candidate.get("observed600Pct", 0.0)) < min_observed_600_pct:
                continue
            if float(candidate.get("observedLookbackPct", 0.0)) < min_observed_lookback_pct:
                continue
            if float(candidate["r10"]) > r10_cap:
                continue
            if down_r10_cap is not None and candidate["signal"] == "DOWN" and float(candidate["r10"]) > down_r10_cap:
                continue
            if mid_route_sigma_cap is not None and role == "mid" and float(candidate["routeSigma"]) >= mid_route_sigma_cap:
                continue
            if block_down_sigma_band is not None and candidate["signal"] == "DOWN":
                lo, hi = block_down_sigma_band
                if lo <= float(candidate["routeSigma"]) < hi:
                    continue
            selected = candidate
            break
        if selected is None:
            continue

        accepted.append(selected)
        last_idx = idx
        role = selected["role"]
        if selected["won"]:
            global_losses = 0
            branch_losses[role] = 0
        else:
            global_losses += 1
            branch_losses[role] += 1
            if global_loss_cool_count and global_losses >= global_loss_cool_count:
                global_cool_until = idx + int(global_loss_cool_sec)
                global_losses = 0
            if branch_loss_cool_count and branch_losses[role] >= branch_loss_cool_count:
                branch_cool_until[role] = idx + int(branch_loss_cool_sec)
                branch_losses[role] = 0
    return accepted


def run() -> dict[str, Any]:
    bars = load_second_bars(DATA_ANCHOR, include_shards=True)
    if OUT_CANDIDATES.exists():
        candidates = pd.read_csv(OUT_CANDIDATES).to_dict("records")
    else:
        candidates = build_candidates(bars)
        pd.DataFrame(candidates).to_csv(OUT_CANDIDATES, index=False, encoding="utf-8-sig")
    valid_days = {
        "2026-06-14",
        "2026-06-15",
        "2026-06-16",
        "2026-06-17",
        "2026-06-18",
        "2026-06-19",
        "2026-06-20",
        "2026-06-23",
        "2026-06-25",
        "2026-06-27",
        "2026-06-28",
        "2026-06-29",
        "2026-06-30",
        "2026-07-01",
        "2026-07-02",
        "2026-07-03",
        "2026-07-04",
    }
    policies = [
        {
            "name": "baseline_valid_obs85",
            "r10_cap": 45.0,
            "mid_route_sigma_cap": None,
            "down_r10_cap": None,
            "block_down_sigma_band": None,
            "branch_loss_cool_count": 0,
            "branch_loss_cool_sec": 0,
            "global_loss_cool_count": 0,
            "global_loss_cool_sec": 0,
            "min_observed_pct": 85.0,
        },
        {
            "name": "tuned_no_risk",
            "r10_cap": 42.0,
            "mid_route_sigma_cap": 20.0,
            "down_r10_cap": 35.0,
            "block_down_sigma_band": None,
            "branch_loss_cool_count": 0,
            "branch_loss_cool_sec": 0,
            "global_loss_cool_count": 0,
            "global_loss_cool_sec": 0,
            "min_observed_pct": 85.0,
        },
        {
            "name": "balanced_dd21",
            "r10_cap": 42.0,
            "mid_route_sigma_cap": 20.0,
            "down_r10_cap": 35.0,
            "block_down_sigma_band": None,
            "branch_loss_cool_count": 0,
            "branch_loss_cool_sec": 0,
            "global_loss_cool_count": 2,
            "global_loss_cool_sec": 3600,
            "min_observed_pct": 88.0,
        },
        {
            "name": "strict_dd19_lowfreq",
            "r10_cap": 42.0,
            "mid_route_sigma_cap": 20.0,
            "down_r10_cap": 35.0,
            "block_down_sigma_band": None,
            "branch_loss_cool_count": 0,
            "branch_loss_cool_sec": 0,
            "global_loss_cool_count": 2,
            "global_loss_cool_sec": 3600,
            "min_observed_pct": 93.0,
        },
    ]
    results = []
    accepted_by_policy: dict[str, list[dict[str, Any]]] = {}
    for policy in policies:
        rows = select_router(
            candidates,
            r10_cap=policy["r10_cap"],
            mid_route_sigma_cap=policy["mid_route_sigma_cap"],
            down_r10_cap=policy["down_r10_cap"],
            block_down_sigma_band=policy["block_down_sigma_band"],
            branch_loss_cool_count=policy["branch_loss_cool_count"],
            branch_loss_cool_sec=policy["branch_loss_cool_sec"],
            global_loss_cool_count=policy["global_loss_cool_count"],
            global_loss_cool_sec=policy["global_loss_cool_sec"],
            allowed_days=valid_days,
            min_observed_600_pct=policy["min_observed_pct"],
            min_observed_lookback_pct=policy["min_observed_pct"],
        )
        accepted_by_policy[policy["name"]] = rows
        train = [row for row in rows if row["day"] < RECENT_CUTOFF]
        recent = [row for row in rows if row["day"] >= RECENT_CUTOFF]
        by_role = {role: summarize([row for row in rows if row["role"] == role]) for role in ("low", "mid", "high")}
        results.append(
            {
                "policy": policy,
                "total": summarize(rows),
                "train": summarize(train),
                "recent": summarize(recent),
                "byRole": by_role,
            }
        )

    best_name = "balanced_dd21"
    pd.DataFrame(accepted_by_policy[best_name]).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    output = {
        "data": {
            "anchor": str(DATA_ANCHOR),
            "rows": int(len(bars)),
            "start": bars.index.min().isoformat(),
            "end": bars.index.max().isoformat(),
            "candidateCount": len(candidates),
            "candidateCsv": str(OUT_CANDIDATES),
        },
        "results": results,
        "selectedPolicy": best_name,
        "selectedTradesCsv": str(OUT_TRADES),
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
