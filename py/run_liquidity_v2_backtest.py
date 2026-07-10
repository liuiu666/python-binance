"""Backtest the live normal/liquidity V2 strategy with its shared rule core."""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from liquidity_v2_core import (  # noqa: E402
    LiquidityV2Rules,
    build_features,
    evaluate_candidate,
    normal_ready,
    veto_owns_window,
)
from second_backtest.data import load_second_bars  # noqa: E402


def load_strategy_config(path: Path, strategy_id: str | None) -> tuple[str, dict]:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    strategies = raw.get("strategies", raw)
    if strategy_id:
        cfg = strategies.get(strategy_id)
        if not isinstance(cfg, dict):
            raise KeyError(f"strategy not found: {strategy_id}")
        return strategy_id, cfg
    for key, cfg in strategies.items():
        if isinstance(cfg, dict) and cfg.get("model_type") == "second_normal_liquidity_orderbook_v1":
            return str(key), cfg
    raise KeyError("no second_normal_liquidity_orderbook_v1 strategy in config")


def load_scan_times(path: Path) -> set[pd.Timestamp]:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, list):
        raise ValueError("scan-times file must contain a JSON list")
    values = []
    for item in raw:
        value = item.get("time") if isinstance(item, dict) else item
        timestamp = pd.to_datetime(value, utc=True, errors="coerce")
        if not pd.isna(timestamp):
            values.append(pd.Timestamp(timestamp))
    return set(values)


def read_orderbook(path: Path, target_index: pd.DatetimeIndex, max_age_sec: int) -> pd.DataFrame:
    usecols = {
        "timestamp", "mid", "spread_bps", "bid_qty_20", "ask_qty_20",
        "imbalance_5", "imbalance_20", "microprice_edge_bps",
        "bid_wall_qty", "ask_wall_qty",
    }
    raw = pd.read_csv(path, usecols=lambda col: col in usecols)
    ts = pd.to_datetime(raw["timestamp"], utc=True, errors="coerce").dt.floor("s")
    valid = ts.notna()
    raw = raw.loc[valid].reset_index(drop=True)
    ts = ts.loc[valid].reset_index(drop=True)
    cols = [
        "mid", "spread_bps", "bid_qty_20", "ask_qty_20", "imbalance_5",
        "imbalance_20", "microprice_edge_bps", "bid_wall_qty", "ask_wall_qty",
    ]
    ob = pd.DataFrame(index=ts.to_numpy())
    for col in cols:
        ob[col] = pd.to_numeric(raw[col], errors="coerce").to_numpy(float) if col in raw.columns else np.nan
    ob["ob_ts_ms"] = (ts.astype("int64") // 1_000_000).to_numpy()
    ob = ob[~ob.index.duplicated(keep="last")].sort_index()
    aligned = ob.reindex(target_index, method="ffill", limit=max(1, int(max_age_sec)))
    target_ms = pd.Series(target_index.astype("int64") // 1_000_000, index=target_index)
    aligned["ob_age_sec"] = (target_ms - aligned["ob_ts_ms"]) / 1000.0
    aligned["ob_available"] = aligned["mid"].notna() & aligned["ob_age_sec"].notna() & (aligned["ob_age_sec"] <= float(max_age_sec))
    return aligned


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
    for row in rows:
        pnl = amount * payout_rate if row["won"] else -amount
        wins += int(row["won"])
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        loss_streak = 0 if row["won"] else loss_streak + 1
        max_loss_streak = max(max_loss_streak, loss_streak)
    count = len(rows)
    return {
        "trades": count,
        "wins": wins,
        "winRate": round(wins / count * 100.0, 2) if count else 0.0,
        "pnlU": round(equity, 4),
        "maxDrawdownU": round(max_drawdown, 4),
        "maxLossStreak": max_loss_streak,
        "tradesPerDay": round(count / max(hours, 1e-9) * 24.0, 2),
    }


def run(args) -> dict:
    strategy_id, cfg = load_strategy_config(Path(args.prod_config), args.strategy_id)
    rules = LiquidityV2Rules.from_config(cfg)
    bars = load_second_bars(Path(args.seconds), include_shards=not args.no_shards)
    orderbook = read_orderbook(Path(args.orderbook), bars.index, rules.orderbook_max_age_sec)
    data = bars.join(orderbook, how="left")
    data = data[~data.index.duplicated(keep="last")].sort_index()
    requested_start = pd.Timestamp(args.start) if args.start else None
    requested_end = pd.Timestamp(args.end) if args.end else None
    scan_times = load_scan_times(Path(args.scan_times)) if args.scan_times else None
    evaluation_mask = pd.Series(True, index=data.index)
    if requested_start is not None:
        evaluation_mask &= data.index >= requested_start
    if requested_end is not None:
        evaluation_mask &= data.index < requested_end
    evaluation_data = data.loc[evaluation_mask]
    features = build_features(data, rules)
    close = data["close"].to_numpy(float)
    warmup = max(
        rules.normal_window_sec,
        rules.center_slope_sec,
        rules.retest_sec,
        3600 if rules.trend_space_enabled else 900,
    ) + 10
    limit = len(data) - rules.horizon_sec
    last_emit_idx = -10**12
    trades = []
    vetoes = []
    raw_candidates = 0
    for idx in range(warmup, max(warmup, limit)):
        signal_time = data.index[idx]
        if requested_end is not None and signal_time >= requested_end:
            break
        if scan_times is not None and signal_time not in scan_times:
            continue
        in_evaluation = requested_start is None or signal_time >= requested_start
        if idx - last_emit_idx < rules.min_gap_sec:
            continue
        row = features.iloc[idx]
        if not bool(data["ob_available"].iloc[idx]) or not normal_ready(row, rules):
            continue
        decision = evaluate_candidate(row, rules)
        if in_evaluation and decision["raw_signal"]:
            raw_candidates += 1
        if decision["status"] == "wait":
            continue
        if decision["status"] == "veto":
            if veto_owns_window(decision["reason"]):
                last_emit_idx = idx
            if in_evaluation:
                vetoes.append({
                    "time": data.index[idx],
                    "reason": decision["reason"],
                    "blocked_signal": decision.get("blocked_signal"),
                    "raw_signal": decision.get("raw_signal"),
                })
            continue
        signal = decision["signal"]
        last_emit_idx = idx
        if not in_evaluation:
            continue
        entry = float(close[idx])
        settle = float(close[idx + rules.horizon_sec])
        won = settle > entry if signal == "UP" else settle < entry
        trades.append({
            "time": data.index[idx],
            "settle_time": data.index[idx + rules.horizon_sec],
            "signal": signal,
            "reason": decision["reason"],
            "raw_signal": decision["raw_signal"],
            "raw_reason": decision["raw_reason"],
            "bidwall_trap": bool(decision["bidwall_trap"]),
            "entry": entry,
            "settle": settle,
            "won": bool(won),
        })
    hours = (
        (evaluation_data.index.max() - evaluation_data.index.min()).total_seconds() / 3600.0
        if len(evaluation_data)
        else 0.0
    )
    result = {
        "strategyId": strategy_id,
        "modelType": cfg.get("model_type"),
        "seconds": str(Path(args.seconds).resolve()),
        "orderbook": str(Path(args.orderbook).resolve()),
        "start": evaluation_data.index.min().isoformat() if len(evaluation_data) else None,
        "end": evaluation_data.index.max().isoformat() if len(evaluation_data) else None,
        "rows": int(len(evaluation_data)),
        "hours": round(hours, 4),
        "scanMode": "server_audit" if scan_times is not None else "every_second",
        "scanTimes": len(scan_times) if scan_times is not None else None,
        "rules": asdict(rules),
        "rawCandidates": raw_candidates,
        "vetoes": len(vetoes),
        "vetoReasons": pd.Series([row["reason"] for row in vetoes]).value_counts().to_dict() if vetoes else {},
        "overall": summarize(trades, hours, args.amount, args.payout_rate),
    }
    Path(args.out).write_text(json.dumps(clean(result), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(clean(trades)).to_csv(args.trades_out, index=False, encoding="utf-8-sig")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", required=True)
    parser.add_argument("--orderbook", required=True)
    parser.add_argument("--prod-config", default=str(ROOT / "data" / "prod_config.json"))
    parser.add_argument("--strategy-id")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--scan-times", help="JSON list of live scan timestamps for exact server replay.")
    parser.add_argument("--no-shards", action="store_true", help="Read only the main seconds CSV.")
    parser.add_argument("--amount", type=float, default=5.0)
    parser.add_argument("--payout-rate", type=float, default=0.8)
    parser.add_argument("--out", default=str(ROOT / "tmp" / "liquidity_v2_backtest.json"))
    parser.add_argument("--trades-out", default=str(ROOT / "tmp" / "liquidity_v2_backtest_trades.csv"))
    args = parser.parse_args()
    print(json.dumps(clean(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
