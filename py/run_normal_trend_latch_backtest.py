"""Backtest the production normal/trend order-book latch engine."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from normal_trend_latch_core import (  # noqa: E402
    NormalTrendLatchEngine,
    RouterRules,
    build_router_features,
    passive_book_valid,
)
from run_liquidity_v2_backtest import read_orderbook  # noqa: E402
from second_backtest.data import load_second_bars  # noqa: E402


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


def load_config(path: Path, strategy_id: str):
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    cfg = raw.get("strategies", raw).get(strategy_id)
    if not isinstance(cfg, dict):
        raise KeyError(f"strategy not found: {strategy_id}")
    return cfg


def metrics(rows, hours, amount=5.0, payout=0.8):
    equity = peak = drawdown = 0.0
    wins = 0
    for row in rows:
        wins += int(row["won"])
        equity += amount * payout if row["won"] else -amount
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    count = len(rows)
    return {
        "trades": count,
        "wins": wins,
        "winRate": round(wins / count * 100.0, 2) if count else 0.0,
        "pnlU": round(equity, 2),
        "maxDrawdownU": round(drawdown, 2),
        "tradesPerDay": round(count / max(hours, 1e-9) * 24.0, 2),
    }


def run(args):
    cfg = load_config(Path(args.prod_config), args.strategy_id)
    cfg = {**cfg, "router_execution_phase": args.phase}
    rules = RouterRules.from_config(cfg)
    bars = load_second_bars(Path(args.seconds), include_shards=not args.no_shards)
    orderbook = read_orderbook(Path(args.orderbook), bars.index, rules.base.orderbook_max_age_sec)
    data = bars.join(orderbook, how="left")
    data = data[~data.index.duplicated(keep="last")].sort_index()
    start = pd.Timestamp(args.start) if args.start else data.index.min()
    end = pd.Timestamp(args.end) if args.end else data.index.max()
    warmup_start = start - pd.Timedelta(seconds=3700)
    work = data[(data.index >= warmup_start) & (data.index < end + pd.Timedelta(seconds=rules.base.horizon_sec + 5))]
    features = build_router_features(work, rules)
    engine = NormalTrendLatchEngine(cfg)
    close = work["close"].to_numpy(float)
    first_idx = int(work.index.searchsorted(start))
    last_idx = min(len(work) - rules.base.horizon_sec, int(work.index.searchsorted(end)))
    rows = []
    for idx in range(first_idx, last_idx):
        previous_emit_time = engine.last_emit_time
        result = engine.step(work.index[idx], features.iloc[idx])
        emitted = result.get("signal")
        if not emitted:
            continue
        execution_idx = idx + max(0, int(args.operational_delay_sec))
        if execution_idx + rules.base.horizon_sec >= len(work):
            continue
        if not passive_book_valid(features.iloc[execution_idx], emitted["signal"], rules.base):
            engine.last_emit_time = previous_emit_time
            continue
        engine.last_emit_time = work.index[execution_idx]
        sign = 1.0 if emitted["signal"] == "UP" else -1.0
        entry = float(close[execution_idx])
        settle = float(close[execution_idx + rules.base.horizon_sec])
        signed_bps = (settle / entry - 1.0) * 10000.0 * sign
        rows.append({
            "signal_time": work.index[idx],
            "time": work.index[execution_idx],
            "settle_time": work.index[execution_idx + rules.base.horizon_sec],
            "kind": emitted["kind"],
            "band": emitted["band"],
            "signal": emitted["signal"],
            "reason": emitted["reason"],
            "delay_sec": emitted["delay_sec"],
            "entry": entry,
            "settle": settle,
            "signed_outcome_bps": signed_bps,
            "won": bool(signed_bps > 0.0),
        })
    hours = max(0.0, (min(end, work.index.max()) - start).total_seconds() / 3600.0)
    output = {
        "strategyId": args.strategy_id,
        "modelType": cfg.get("model_type"),
        "start": start,
        "end": end,
        "hours": round(hours, 4),
        "phase": args.phase,
        "operationalDelaySec": args.operational_delay_sec,
        "overall": metrics(rows, hours, args.amount, args.payout_rate),
        "byKind": {
            kind: metrics([row for row in rows if row["kind"] == kind], hours, args.amount, args.payout_rate)
            for kind in ("normal", "trend")
        },
    }
    Path(args.out).write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(clean(rows)).to_csv(args.trades_out, index=False, encoding="utf-8-sig")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", required=True)
    parser.add_argument("--orderbook", required=True)
    parser.add_argument("--prod-config", default=str(ROOT / "data" / "prod_config.json"))
    parser.add_argument("--strategy-id", default="BTC_10min_NORMAL_LIQ_OB_V2_QUALITY")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--phase", type=int, default=0)
    parser.add_argument("--operational-delay-sec", type=int, default=0)
    parser.add_argument("--no-shards", action="store_true")
    parser.add_argument("--amount", type=float, default=5.0)
    parser.add_argument("--payout-rate", type=float, default=0.8)
    parser.add_argument("--out", default=str(ROOT / "tmp" / "normal_trend_latch_backtest.json"))
    parser.add_argument("--trades-out", default=str(ROOT / "tmp" / "normal_trend_latch_backtest_trades.csv"))
    args = parser.parse_args()
    print(json.dumps(clean(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
