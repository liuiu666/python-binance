"""Research a low-volatility supplement for the production V2 latch strategy.

This script does not change live trading logic. It replays the current V2 latch
engine, then adds a separate low-volatility edge/reclaim branch for periods
where V2 mostly waits because the normal sigma is below the production floor.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from liquidity_v2_core import LiquidityV2Rules  # noqa: E402
from normal_trend_latch_core import (  # noqa: E402
    NormalTrendLatchEngine,
    RouterRules,
    build_router_features,
)
from backtest_io import read_orderbook  # noqa: E402
from run_normal_trend_latch_backtest import load_config  # noqa: E402
from second_backtest.data import load_second_bars  # noqa: E402


@dataclass(frozen=True)
class LowVolSupplementRules:
    min_sigma_bps: float = 2.4
    max_sigma_bps: float = 6.5
    min_inside1_ratio: float = 0.50
    min_observed_pct: float = 90.0
    max_center_slope_bps: float = 8.0
    max_sigma_expand: float = 1.75
    max_abs_ret600_bps: float = 22.0
    z_edge: float = 0.80
    z_reclaim: float = 0.72
    retest_sec: int = 120
    imbalance_min: float = 0.06
    micro_min_bps: float = 0.0007
    wall_ratio_min: float = 0.90
    flow_guard: float = 0.22
    min_gap_sec: int = 600
    orderbook_max_age_sec: int = 3


VARIANTS = {
    "balanced": LowVolSupplementRules(),
    "loose": LowVolSupplementRules(
        min_sigma_bps=1.8,
        min_inside1_ratio=0.45,
        max_center_slope_bps=10.0,
        max_abs_ret600_bps=28.0,
        z_edge=0.72,
        z_reclaim=0.78,
        imbalance_min=0.045,
        micro_min_bps=0.0005,
        flow_guard=0.28,
    ),
    "strict": LowVolSupplementRules(
        min_sigma_bps=2.8,
        min_inside1_ratio=0.58,
        max_center_slope_bps=6.0,
        max_sigma_expand=1.55,
        max_abs_ret600_bps=16.0,
        z_edge=0.88,
        z_reclaim=0.65,
        imbalance_min=0.08,
        micro_min_bps=0.001,
        wall_ratio_min=1.0,
        flow_guard=0.16,
    ),
    "inside_guarded": LowVolSupplementRules(
        min_sigma_bps=2.4,
        max_sigma_bps=6.5,
        min_inside1_ratio=0.60,
        max_center_slope_bps=8.0,
        max_abs_ret600_bps=22.0,
        z_edge=0.80,
        z_reclaim=0.72,
        imbalance_min=0.06,
        micro_min_bps=0.0007,
        wall_ratio_min=0.90,
        flow_guard=0.22,
    ),
    "slope_flow_guarded": LowVolSupplementRules(
        min_sigma_bps=2.4,
        max_sigma_bps=6.5,
        min_inside1_ratio=0.50,
        max_center_slope_bps=6.0,
        max_abs_ret600_bps=15.0,
        z_edge=0.80,
        z_reclaim=0.72,
        imbalance_min=0.06,
        micro_min_bps=0.0007,
        wall_ratio_min=0.90,
        flow_guard=0.05,
    ),
    "inside_flow_guarded": LowVolSupplementRules(
        min_sigma_bps=2.4,
        max_sigma_bps=6.5,
        min_inside1_ratio=0.60,
        max_center_slope_bps=8.0,
        max_abs_ret600_bps=22.0,
        z_edge=0.80,
        z_reclaim=0.72,
        imbalance_min=0.06,
        micro_min_bps=0.0007,
        wall_ratio_min=0.90,
        flow_guard=0.05,
    ),
}


def clean(value):
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def summarize(rows: list[dict], hours: float, amount: float, payout_rate: float) -> dict:
    equity = peak = max_drawdown = 0.0
    wins = loss_streak = max_loss_streak = 0
    signed = []
    for row in rows:
        won = bool(row["won"])
        pnl = amount * payout_rate if won else -amount
        wins += int(won)
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        loss_streak = 0 if won else loss_streak + 1
        max_loss_streak = max(max_loss_streak, loss_streak)
        if row.get("signed_outcome_bps") is not None:
            signed.append(float(row["signed_outcome_bps"]))
    count = len(rows)
    return {
        "trades": count,
        "wins": wins,
        "winRate": round(wins / count * 100.0, 2) if count else 0.0,
        "pnlU": round(equity, 2),
        "maxDrawdownU": round(max_drawdown, 2),
        "maxLossStreak": max_loss_streak,
        "tradesPerDay": round(count / max(hours, 1e-9) * 24.0, 2),
        "avgSignedBps": round(float(np.mean(signed)), 4) if signed else None,
        "medianSignedBps": round(float(np.median(signed)), 4) if signed else None,
    }


def direction_book_ok(row: pd.Series, signal: str, rules: LowVolSupplementRules) -> bool:
    sign = 1.0 if signal == "UP" else -1.0
    bid = float(row.get("bid_qty_20", np.nan))
    ask = float(row.get("ask_qty_20", np.nan))
    imbalance = sign * float(row.get("imbalance_20", np.nan))
    micro = sign * float(row.get("micro_bps", np.nan))
    flow = sign * float(row.get("flow_60", np.nan))
    supporting = bid if signal == "UP" else ask
    opposing = ask if signal == "UP" else bid
    values = [bid, ask, imbalance, micro, flow, supporting, opposing]
    return bool(
        np.isfinite(values).all()
        and imbalance >= rules.imbalance_min
        and micro >= rules.micro_min_bps
        and flow >= -rules.flow_guard
        and supporting >= max(1e-9, opposing * rules.wall_ratio_min)
    )


def lowvol_base_ready(row: pd.Series, rules: LowVolSupplementRules) -> bool:
    checks = [
        row.get("z"),
        row.get("inside1_ratio"),
        row.get("observed_pct"),
        row.get("center_slope_bps"),
        row.get("sigma_bps"),
        row.get("sigma_expand"),
        row.get("ret_600s_bps"),
        row.get("ob_age_sec"),
    ]
    if any(not np.isfinite(float(value)) for value in checks):
        return False
    return bool(
        float(row["observed_pct"]) >= rules.min_observed_pct
        and bool(row.get("ob_available", False))
        and float(row["ob_age_sec"]) <= rules.orderbook_max_age_sec
        and rules.min_sigma_bps <= float(row["sigma_bps"]) <= rules.max_sigma_bps
        and float(row["inside1_ratio"]) >= rules.min_inside1_ratio
        and abs(float(row["center_slope_bps"])) <= rules.max_center_slope_bps
        and float(row["sigma_expand"]) <= rules.max_sigma_expand
        and abs(float(row["ret_600s_bps"])) <= rules.max_abs_ret600_bps
    )


def lowvol_signal(row: pd.Series, rules: LowVolSupplementRules) -> tuple[str | None, str | None]:
    if not lowvol_base_ready(row, rules):
        return None, None
    z = float(row["z"])
    z_max = float(row.get("z_max_retest", np.nan))
    z_min = float(row.get("z_min_retest", np.nan))
    upper_reclaim = np.isfinite(z_max) and z_max >= rules.z_edge and 0.0 <= z <= rules.z_reclaim
    lower_reclaim = np.isfinite(z_min) and z_min <= -rules.z_edge and -rules.z_reclaim <= z <= 0.0
    if upper_reclaim and direction_book_ok(row, "DOWN", rules):
        return "DOWN", "lowvol_upper_reclaim_fade"
    if lower_reclaim and direction_book_ok(row, "UP", rules):
        return "UP", "lowvol_lower_reclaim_fade"
    return None, None


def trade_row(source: str, timestamp, signal: str, reason: str, close: np.ndarray, idx: int, horizon_sec: int) -> dict:
    sign = 1.0 if signal == "UP" else -1.0
    entry = float(close[idx])
    settle = float(close[idx + horizon_sec])
    signed_bps = (settle / entry - 1.0) * 10000.0 * sign
    return {
        "source": source,
        "time": timestamp,
        "settle_time": timestamp + pd.Timedelta(seconds=horizon_sec),
        "signal": signal,
        "reason": reason,
        "entry": entry,
        "settle": settle,
        "signed_outcome_bps": signed_bps,
        "won": bool(signed_bps > 0.0),
    }


def attach_feature_snapshot(row: dict, feature: pd.Series) -> dict:
    keys = [
        "z", "sigma_bps", "inside1_ratio", "observed_pct", "center_slope_bps",
        "sigma_expand", "flow_60", "imbalance_20", "micro_bps", "ret_300s_bps",
        "ret_600s_bps", "ret_1800s_bps", "pos_600s", "pos_1800s",
        "bid20_chg_30", "bid20_chg_60", "ask20_chg_30", "bid_qty_20",
        "ask_qty_20",
    ]
    for key in keys:
        value = feature.get(key)
        row[key] = None if value is None or not np.isfinite(float(value)) else float(value)
    return row


def replay_v2(cfg: dict, features: pd.DataFrame, data: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp, amount: float, payout_rate: float) -> list[dict]:
    rules = RouterRules.from_config(cfg)
    engine = NormalTrendLatchEngine(cfg)
    close = data["close"].to_numpy(float)
    first_idx = int(data.index.searchsorted(start))
    last_idx = min(len(data) - rules.base.horizon_sec, int(data.index.searchsorted(end)))
    rows = []
    for idx in range(first_idx, last_idx):
        result = engine.step(data.index[idx], features.iloc[idx])
        emitted = result.get("signal")
        if not emitted:
            continue
        if idx + rules.base.horizon_sec >= len(data):
            continue
        row = trade_row("v2", data.index[idx], str(emitted["signal"]), str(emitted["reason"]), close, idx, rules.base.horizon_sec)
        row.update(kind=emitted.get("kind"), band=emitted.get("band"), delay_sec=emitted.get("delay_sec"))
        attach_feature_snapshot(row, features.iloc[idx])
        rows.append(row)
    return rows


def replay_supplement(
    features: pd.DataFrame,
    data: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    horizon_sec: int,
    rules: LowVolSupplementRules,
) -> tuple[list[dict], dict]:
    close = data["close"].to_numpy(float)
    first_idx = int(data.index.searchsorted(start))
    warmup_idx = int(data.index.searchsorted(start - pd.Timedelta(seconds=3700)))
    first_idx = max(first_idx, warmup_idx + 3700)
    last_idx = min(len(data) - horizon_sec, int(data.index.searchsorted(end)))
    last_emit_idx = -10**12
    rows = []
    diagnostics = {
        "baseReady": 0,
        "candidates": 0,
        "upperCandidates": 0,
        "lowerCandidates": 0,
        "bookRejected": 0,
    }
    for idx in range(first_idx, last_idx):
        if idx - last_emit_idx < rules.min_gap_sec:
            continue
        row = features.iloc[idx]
        if not lowvol_base_ready(row, rules):
            continue
        diagnostics["baseReady"] += 1
        z = float(row["z"])
        upper = float(row.get("z_max_retest", np.nan)) >= rules.z_edge and 0.0 <= z <= rules.z_reclaim
        lower = float(row.get("z_min_retest", np.nan)) <= -rules.z_edge and -rules.z_reclaim <= z <= 0.0
        diagnostics["upperCandidates"] += int(bool(upper))
        diagnostics["lowerCandidates"] += int(bool(lower))
        signal, reason = lowvol_signal(row, rules)
        if not signal:
            if upper or lower:
                diagnostics["bookRejected"] += 1
            continue
        diagnostics["candidates"] += 1
        last_emit_idx = idx
        rows.append(attach_feature_snapshot(
            trade_row("lowvol_supplement", data.index[idx], signal, reason, close, idx, horizon_sec),
            row,
        ))
    return rows, diagnostics


def merge_trades(v2_rows: list[dict], supplement_rows: list[dict], gap_sec: int) -> list[dict]:
    merged = []
    last_time = None
    for row in sorted([*v2_rows, *supplement_rows], key=lambda item: item["time"]):
        timestamp = pd.Timestamp(row["time"])
        if last_time is not None and (timestamp - last_time).total_seconds() < gap_sec:
            continue
        merged.append(row)
        last_time = timestamp
    return merged


def run(args):
    cfg = load_config(Path(args.prod_config), args.strategy_id)
    router_rules = RouterRules.from_config(cfg)
    bars = load_second_bars(Path(args.seconds), include_shards=not args.no_shards)
    orderbook = read_orderbook(Path(args.orderbook), bars.index, router_rules.base.orderbook_max_age_sec)
    data = bars.join(orderbook, how="left")
    data = data[~data.index.duplicated(keep="last")].sort_index()
    start = pd.Timestamp(args.start, tz="UTC") if args.start else data.index.min() + pd.Timedelta(seconds=3700)
    end = pd.Timestamp(args.end, tz="UTC") if args.end else data.index.max() - pd.Timedelta(seconds=router_rules.base.horizon_sec + 5)
    work = data[(data.index >= start - pd.Timedelta(seconds=3700)) & (data.index <= end + pd.Timedelta(seconds=router_rules.base.horizon_sec + 5))]
    features = build_router_features(work, router_rules)
    hours = max(0.0, (end - start).total_seconds() / 3600.0)

    v2_rows = replay_v2(cfg, features, work, start, end, args.amount, args.payout_rate)
    result = {
        "strategyId": args.strategy_id,
        "seconds": str(Path(args.seconds).resolve()),
        "orderbook": str(Path(args.orderbook).resolve()),
        "start": start,
        "end": end,
        "hours": round(hours, 4),
        "baselineV2": summarize(v2_rows, hours, args.amount, args.payout_rate),
        "variants": {},
    }
    trade_outputs = []
    for variant_name, supplement_rules in VARIANTS.items():
        supplement_rows, diagnostics = replay_supplement(
            features,
            work,
            start,
            end,
            router_rules.base.horizon_sec,
            supplement_rules,
        )
        merged = merge_trades(v2_rows, supplement_rows, router_rules.base.min_gap_sec)
        result["variants"][variant_name] = {
            "rules": asdict(supplement_rules),
            "diagnostics": diagnostics,
            "supplementOnly": summarize(supplement_rows, hours, args.amount, args.payout_rate),
            "merged": summarize(merged, hours, args.amount, args.payout_rate),
        }
        for row in merged:
            trade_outputs.append({**row, "variant": variant_name})

    Path(args.out).write_text(json.dumps(clean(result), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(clean(trade_outputs)).to_csv(args.trades_out, index=False, encoding="utf-8-sig")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", required=True)
    parser.add_argument("--orderbook", required=True)
    parser.add_argument("--prod-config", default=str(ROOT / "data" / "prod_config.json"))
    parser.add_argument("--strategy-id", default="BTC_10min_NORMAL_LIQ_OB_V2_QUALITY")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--no-shards", action="store_true")
    parser.add_argument("--amount", type=float, default=5.0)
    parser.add_argument("--payout-rate", type=float, default=0.8)
    parser.add_argument("--out", default=str(ROOT / "tmp" / "v2_lowvol_supplement_research.json"))
    parser.add_argument("--trades-out", default=str(ROOT / "tmp" / "v2_lowvol_supplement_trades.csv"))
    args = parser.parse_args()
    print(json.dumps(clean(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
