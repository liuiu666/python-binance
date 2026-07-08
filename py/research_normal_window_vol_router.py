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

from research_v21_good_regime_score import DATA_ANCHOR, day_health  # noqa: E402
from second_backtest.data import load_second_bars  # noqa: E402
from second_backtest.strategies import (  # noqa: E402
    SecondNormalConfig,
    _observed_pct_array,
    _rolling_range_bps,
    _rolling_sigma_bps,
    generate_normal_signals,
)


OUT_JSON = ROOT / "tmp" / "normal_window_vol_router_research.json"
OUT_CANDIDATES = ROOT / "tmp" / "normal_window_vol_router_candidates.csv"
OUT_DAILY = ROOT / "tmp" / "normal_window_vol_router_daily.csv"
OUT_BUCKETS = ROOT / "tmp" / "normal_window_vol_router_buckets.csv"
OUT_TRADES = ROOT / "tmp" / "normal_window_vol_router_trades.csv"

WINDOWS = (600, 900, 1200, 1800, 2700, 3600, 4200, 5400, 7200)
TAIL_PCT = 0.25
HORIZON_SEC = 600
GAP_SEC = 600
TRAIN_END_DAY = "2026-06-28"
TEST_START_DAY = "2026-06-29"
LATEST_DAYS = {"2026-07-05", "2026-07-06"}

PRESET_DYNAMIC_POLICIES = (
    (
        "dynamic_grid_best_veto",
        {
            "ultra_low_<9": 4200,
            "low_9_12": 3600,
            "mid_12_16": 4200,
            "high_16_22": 2700,
            "extreme_22_plus": 3600,
        },
        True,
        "Best stability-first grid result across restrained volatility/length combinations.",
    ),
    (
        "dynamic_grid_higher_freq_veto",
        {
            "ultra_low_<9": 4200,
            "low_9_12": 3600,
            "mid_12_16": 3600,
            "high_16_22": 1800,
            "extreme_22_plus": 3600,
        },
        True,
        "Higher-frequency grid result; keeps recent days positive but accepts higher drawdown.",
    ),
    (
        "dynamic_profit_bucket_veto",
        {
            "ultra_low_<9": 4200,
            "low_9_12": 3600,
            "mid_12_16": 900,
            "high_16_22": 1800,
            "extreme_22_plus": 3600,
        },
        True,
        "Uses the best-looking bucket winners; included to expose overfit risk.",
    ),
    (
        "dynamic_stable_bucket_veto",
        {
            "ultra_low_<9": 4200,
            "low_9_12": 3600,
            "mid_12_16": 4200,
            "high_16_22": 3600,
            "extreme_22_plus": 3600,
        },
        True,
        "More conservative route: low volatility stays long, higher volatility uses 3600s.",
    ),
    (
        "dynamic_stable_bucket_no_veto",
        {
            "ultra_low_<9": 4200,
            "low_9_12": 3600,
            "mid_12_16": 4200,
            "high_16_22": 3600,
            "extreme_22_plus": 3600,
        },
        False,
        "Same conservative route without the low-volatility UP veto.",
    ),
    (
        "dynamic_all_3600_veto",
        {
            "ultra_low_<9": 3600,
            "low_9_12": 3600,
            "mid_12_16": 3600,
            "high_16_22": 3600,
            "extreme_22_plus": 3600,
        },
        True,
        "Control group: one 3600s length everywhere, with the low-volatility UP veto.",
    ),
)


def payout(won: bool) -> float:
    return 4.0 if bool(won) else -5.0


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
    for row in sorted(rows, key=lambda item: int(item["idx"])):
        if bool(row["won"]):
            cur = 0
        else:
            cur += 1
            worst = max(worst, cur)
    return worst


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(rows, key=lambda item: int(item["idx"]))
    n = len(rows)
    wins = sum(1 for row in rows if bool(row["won"]))
    pnls = [payout(bool(row["won"])) for row in rows]
    by_day = []
    if rows:
        frame = pd.DataFrame(rows)
        for day, group in frame.groupby("day", sort=True):
            items = group.to_dict("records")
            gwins = sum(1 for row in items if bool(row["won"]))
            gpnl = sum(payout(bool(row["won"])) for row in items)
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
    losing_days = sum(1 for row in by_day if float(row["pnl"]) < 0)
    daily_pnls = [float(row["pnl"]) for row in by_day]
    return {
        "trades": int(n),
        "wins": int(wins),
        "losses": int(n - wins),
        "winRate": round(wins / n * 100.0, 2) if n else 0.0,
        "pnl": round(sum(pnls), 4),
        "maxDrawdownU": max_drawdown(pnls),
        "maxLoss": max_loss_streak(rows),
        "activeDays": int(len(by_day)),
        "tradesPerActiveDay": round(n / len(by_day), 2) if by_day else 0.0,
        "losingDays": int(losing_days),
        "positiveDayRate": round((len(by_day) - losing_days) / len(by_day) * 100.0, 2) if by_day else 0.0,
        "dailyPnlStd": round(float(np.std(daily_pnls, ddof=0)), 4) if daily_pnls else 0.0,
        "worstDay": min(by_day, key=lambda row: float(row["pnl"])) if by_day else None,
        "bestDay": max(by_day, key=lambda row: float(row["pnl"])) if by_day else None,
        "byDay": by_day,
    }


def split_summary(rows: list[dict[str, Any]], complete90_days: set[str]) -> dict[str, Any]:
    return {
        "all": summarize(rows),
        "train": summarize([row for row in rows if str(row.get("day")) <= TRAIN_END_DAY]),
        "test": summarize([row for row in rows if str(row.get("day")) >= TEST_START_DAY]),
        "complete90": summarize([row for row in rows if str(row.get("day")) in complete90_days]),
        "latest_0705_0706": summarize([row for row in rows if str(row.get("day")) in LATEST_DAYS]),
    }


def vol_bucket(route_sigma: float) -> str:
    if not math.isfinite(route_sigma):
        return "unknown"
    if route_sigma < 9.0:
        return "ultra_low_<9"
    if route_sigma < 12.0:
        return "low_9_12"
    if route_sigma < 16.0:
        return "mid_12_16"
    if route_sigma < 22.0:
        return "high_16_22"
    return "extreme_22_plus"


def base_allowed(row: dict[str, Any], *, veto_low_vol_up: bool = False) -> tuple[bool, str]:
    if float(row.get("observed600Pct", 0.0)) < 88.0:
        return False, "entry_observed_low"
    if float(row.get("observedLookbackPct", 0.0)) < 88.0:
        return False, "lookback_observed_low"
    if float(row.get("r10", 0.0)) > 42.0:
        return False, "r10_cap"
    if row.get("signal") == "DOWN" and float(row.get("r10", 0.0)) > 35.0:
        return False, "down_r10_cap"
    if veto_low_vol_up and row.get("signal") == "UP" and str(row.get("volBucket")) in {"ultra_low_<9", "low_9_12"}:
        return False, "low_vol_up_veto"
    return True, "pass"


def execute_rows(rows: list[dict[str, Any]], *, veto_low_vol_up: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_idx: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_idx.setdefault(int(row["idx"]), []).append(row)

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    last_idx = -10**12
    cool_until = -10**12
    loss_streak = 0
    rolling: list[bool] = []

    for idx in sorted(by_idx):
        if idx - last_idx < GAP_SEC:
            rejected.append({"idx": idx, "reason": "gap"})
            continue
        if idx < cool_until:
            rejected.append({"idx": idx, "reason": "loss_density_cooldown"})
            continue

        candidates = sorted(
            by_idx[idx],
            key=lambda item: abs(float(item.get("p_up", 0.5)) - 0.5),
            reverse=True,
        )
        selected = None
        for candidate in candidates:
            ok, reason = base_allowed(candidate, veto_low_vol_up=veto_low_vol_up)
            if not ok:
                rejected.append({"idx": idx, "reason": reason, "lookbackSec": candidate.get("lookback_sec")})
                continue
            selected = candidate
            break
        if selected is None:
            continue

        accepted.append(selected)
        last_idx = idx
        if bool(selected["won"]):
            loss_streak = 0
        else:
            loss_streak += 1
            if loss_streak >= 2:
                cool_until = max(cool_until, idx + 3600)
                loss_streak = 0

        rolling.append(bool(selected["won"]))
        while len(rolling) > 6:
            rolling.pop(0)
        losses = sum(1 for item in rolling if not item)
        if len(rolling) >= 4 and losses >= 3:
            cool_until = max(cool_until, idx + 28_800)
            rolling = []

    return accepted, rejected


def choose_window_by_bucket(train_rows: list[dict[str, Any]]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    frame = pd.DataFrame(train_rows)
    if frame.empty:
        return mapping
    for bucket, bucket_df in frame.groupby("volBucket", sort=True):
        candidates = []
        for length, group in bucket_df.groupby("lookback_sec", sort=True):
            rows = group.to_dict("records")
            summary = summarize(rows)
            if summary["trades"] < 5:
                continue
            score = (
                float(summary["pnl"])
                - 0.75 * float(summary["maxDrawdownU"])
                + 0.25 * float(summary["winRate"])
                - 2.0 * float(summary["losingDays"])
            )
            candidates.append((score, int(length), summary))
        if candidates:
            candidates.sort(key=lambda item: (item[0], item[2]["trades"]), reverse=True)
            mapping[str(bucket)] = candidates[0][1]
    return mapping


def select_dynamic(candidates: list[dict[str, Any]], mapping: dict[str, int], *, name: str, veto_low_vol_up: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected_pool = [
        {**row, "policy": name}
        for row in candidates
        if int(row.get("lookback_sec", 0)) == int(mapping.get(str(row.get("volBucket")), -1))
    ]
    return execute_rows(selected_pool, veto_low_vol_up=veto_low_vol_up)


def build_candidates(bars: pd.DataFrame) -> list[dict[str, Any]]:
    close = bars["close"].to_numpy(float)
    route_sigma = _rolling_sigma_bps(close, 4200, HORIZON_SEC)
    r10 = _rolling_range_bps(close, 600)
    obs600 = _observed_pct_array(bars, 600)
    obs_by_length = {length: _observed_pct_array(bars, length) for length in WINDOWS}

    rows: list[dict[str, Any]] = []
    for length in WINDOWS:
        cfg = SecondNormalConfig(
            strategy_id=f"NORMAL_L{length}_T25_DYN",
            lookback_sec=int(length),
            horizon_sec=HORIZON_SEC,
            signal_gap_sec=0,
            tail_pct=TAIL_PCT,
            second_filter="none",
            zone_filter="dynamic_v3",
            sigma_min_bps=0.0,
            sigma_max_bps=9999.0,
            amount=5.0,
            label=f"normal_l{length}_t25_dyn",
        )
        for sig in generate_normal_signals(bars, cfg, apply_config_gap=False):
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
                    "lookbackSec": int(length),
                    "lookback_sec": int(length),
                    "routeSigma": round(rs, 6),
                    "r10": round(rr, 6),
                    "volBucket": vol_bucket(rs),
                    "observed600Pct": round(float(obs600[idx]), 6),
                    "observedLookbackPct": round(float(obs_by_length[length][idx]), 6),
                    "day": str(pd.Timestamp(sig["time"]).date()),
                    "timeStr": pd.Timestamp(sig["time"]).isoformat(),
                    "pnl": payout(bool(sig["won"])),
                    "policy": f"fixed_L{length}",
                }
            )
            rows.append(row)
    return sorted(rows, key=lambda item: (int(item["idx"]), int(item["lookback_sec"])))


def bucket_rows(rows: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    out = []
    if not rows:
        return out
    frame = pd.DataFrame(rows)
    for columns in (("volBucket",), ("lookback_sec",), ("volBucket", "lookback_sec"), ("volBucket", "signal")):
        grouped = frame.groupby(list(columns), sort=True)
        for key, group in grouped:
            key_values = key if isinstance(key, tuple) else (key,)
            summary = summarize(group.to_dict("records"))
            item = {
                "policy": label,
                "bucketType": "+".join(columns),
                "bucket": "|".join(str(value) for value in key_values),
            }
            for field in ("trades", "winRate", "pnl", "maxDrawdownU", "losingDays", "positiveDayRate", "dailyPnlStd"):
                item[field] = summary[field]
            out.append(item)
    return out


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


def run() -> dict[str, Any]:
    bars = load_second_bars(DATA_ANCHOR, include_shards=True)
    health = day_health(bars)
    complete90_days = set(health["complete90Days"])

    if OUT_CANDIDATES.exists():
        candidates = pd.read_csv(OUT_CANDIDATES).to_dict("records")
    else:
        candidates = build_candidates(bars)
        pd.DataFrame(candidates).to_csv(OUT_CANDIDATES, index=False, encoding="utf-8-sig")

    results = []
    all_trades = []
    all_daily = []
    all_buckets = []

    fixed_train_rows = []
    for length in WINDOWS:
        pool = [{**row, "policy": f"fixed_L{length}"} for row in candidates if int(row.get("lookback_sec", 0)) == int(length)]
        rows, rejected = execute_rows(pool)
        summary = split_summary(rows, complete90_days)
        fixed_train_rows.extend([row for row in rows if str(row.get("day")) <= TRAIN_END_DAY])
        results.append(
            {
                "policy": {"name": f"fixed_L{length}", "lookbackSec": int(length), "type": "fixed"},
                "summary": summary,
                "rejectReasons": pd.Series([row["reason"] for row in rejected]).value_counts().head(12).to_dict() if rejected else {},
            }
        )
        all_trades.extend(rows)
        all_buckets.extend(bucket_rows(rows, f"fixed_L{length}"))
        for day_row in summary["all"]["byDay"]:
            all_daily.append({"policy": f"fixed_L{length}", **day_row})

    mapping = choose_window_by_bucket(fixed_train_rows)
    fallback_mapping = {
        "ultra_low_<9": 4200,
        "low_9_12": 4200,
        "mid_12_16": 4200,
        "high_16_22": 2700,
        "extreme_22_plus": 2700,
    }
    combined_mapping = {**fallback_mapping, **mapping}
    for policy_name, policy_mapping, veto in (
        ("dynamic_train_bucket", combined_mapping, False),
        ("dynamic_train_bucket_lowvol_up_veto", combined_mapping, True),
        ("dynamic_current_v21_lengths", fallback_mapping, True),
    ):
        rows, rejected = select_dynamic(candidates, policy_mapping, name=policy_name, veto_low_vol_up=veto)
        summary = split_summary(rows, complete90_days)
        results.append(
            {
                "policy": {
                    "name": policy_name,
                    "type": "dynamic_by_route_sigma",
                    "mapping": policy_mapping,
                    "vetoLowVolUp": veto,
                },
                "summary": summary,
                "rejectReasons": pd.Series([row["reason"] for row in rejected]).value_counts().head(12).to_dict() if rejected else {},
            }
        )
        all_trades.extend(rows)
        all_buckets.extend(bucket_rows(rows, policy_name))
        for day_row in summary["all"]["byDay"]:
            all_daily.append({"policy": policy_name, **day_row})

    for policy_name, policy_mapping, veto, note in PRESET_DYNAMIC_POLICIES:
        rows, rejected = select_dynamic(candidates, policy_mapping, name=policy_name, veto_low_vol_up=veto)
        summary = split_summary(rows, complete90_days)
        results.append(
            {
                "policy": {
                    "name": policy_name,
                    "type": "dynamic_by_route_sigma_preset",
                    "mapping": policy_mapping,
                    "vetoLowVolUp": veto,
                    "note": note,
                },
                "summary": summary,
                "rejectReasons": pd.Series([row["reason"] for row in rejected]).value_counts().head(12).to_dict() if rejected else {},
            }
        )
        all_trades.extend(rows)
        all_buckets.extend(bucket_rows(rows, policy_name))
        for day_row in summary["all"]["byDay"]:
            all_daily.append({"policy": policy_name, **day_row})

    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "method": {
            "dataAnchor": str(DATA_ANCHOR),
            "windowsSec": list(WINDOWS),
            "tailPct": TAIL_PCT,
            "horizonSec": HORIZON_SEC,
            "trainEndDay": TRAIN_END_DAY,
            "testStartDay": TEST_START_DAY,
            "filters": [
                "dynamic_v3 zone",
                "observed600>=88",
                "observedLookback>=88",
                "r10<=42",
                "DOWN r10<=35",
                "gap 600s",
                "2-loss streak cooldown 1h",
                "3/6 loss density cooldown 8h",
            ],
            "volBuckets": ["ultra_low_<9", "low_9_12", "mid_12_16", "high_16_22", "extreme_22_plus"],
            "trainedMapping": combined_mapping,
        },
        "data": {
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
            "candidates": str(OUT_CANDIDATES),
            "trades": str(OUT_TRADES),
            "daily": str(OUT_DAILY),
            "buckets": str(OUT_BUCKETS),
        },
    }
    pd.DataFrame(all_trades).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    pd.DataFrame(all_daily).to_csv(OUT_DAILY, index=False, encoding="utf-8-sig")
    pd.DataFrame(all_buckets).to_csv(OUT_BUCKETS, index=False, encoding="utf-8-sig")
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    result = run()
    print(json.dumps(clean(result["method"]), ensure_ascii=False, indent=2))
    print("policy,all_trades,all_wr,all_pnl,all_dd,all_losing_days,test_trades,test_wr,test_pnl,test_dd,test_losing_days,latest_trades,latest_wr,latest_pnl,complete90_trades,complete90_wr,complete90_pnl")
    for item in result["results"]:
        name = item["policy"]["name"]
        all_s = item["summary"]["all"]
        test_s = item["summary"]["test"]
        latest_s = item["summary"]["latest_0705_0706"]
        c90_s = item["summary"]["complete90"]
        print(
            ",".join(
                str(part)
                for part in (
                    name,
                    all_s["trades"],
                    all_s["winRate"],
                    all_s["pnl"],
                    all_s["maxDrawdownU"],
                    all_s["losingDays"],
                    test_s["trades"],
                    test_s["winRate"],
                    test_s["pnl"],
                    test_s["maxDrawdownU"],
                    test_s["losingDays"],
                    latest_s["trades"],
                    latest_s["winRate"],
                    latest_s["pnl"],
                    c90_s["trades"],
                    c90_s["winRate"],
                    c90_s["pnl"],
                )
            )
        )
