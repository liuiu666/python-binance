"""Replay the production normal/trend order-book latch strategy.

This runner intentionally imports the same feature builder and stateful engine
used by ``signal_btc.py``.  It is separate from the older stateless liquidity
research runner so historical reports cannot silently mix the two strategies.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from normal_trend_latch_core import (  # noqa: E402
    NormalTrendLatchEngine,
    RouterRules,
    build_router_features,
)
from backtest_io import read_orderbook  # noqa: E402
from second_backtest.data import load_second_bars  # noqa: E402


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return value


def utc(value: str | pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def price_at_or_after(
    close: pd.Series,
    target: pd.Timestamp,
    max_age_sec: int = 3,
) -> tuple[pd.Timestamp, float] | None:
    index = close.index
    position = int(index.searchsorted(target))
    if position >= len(index):
        return None
    timestamp = pd.Timestamp(index[position])
    if (timestamp - target).total_seconds() > max_age_sec:
        return None
    return timestamp, float(close.iloc[position])


def load_config(path: Path, strategy_id: str) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    cfg = raw.get(strategy_id)
    if not isinstance(cfg, dict):
        cfg = raw.get("strategies", {}).get(strategy_id)
    if not isinstance(cfg, dict):
        raise KeyError(f"strategy not found: {strategy_id}")
    return cfg


def metrics(rows: list[dict[str, Any]], hours: float, amount: float, payout_rate: float) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: str(row["entry_time"]))
    equity = peak = max_drawdown = 0.0
    wins = loss_streak = max_loss_streak = 0
    for row in ordered:
        won = bool(row["won"])
        wins += int(won)
        equity += amount * payout_rate if won else -amount
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        loss_streak = 0 if won else loss_streak + 1
        max_loss_streak = max(max_loss_streak, loss_streak)
    count = len(ordered)
    return {
        "trades": count,
        "wins": wins,
        "winRate": round(wins / count * 100.0, 2) if count else 0.0,
        "pnlU": round(equity, 4),
        "maxDrawdownU": round(max_drawdown, 4),
        "maxLossStreak": max_loss_streak,
        "tradesPerDay": round(count / max(hours, 1e-9) * 24.0, 2),
    }


def replay(
    data: pd.DataFrame,
    cfg: dict[str, Any],
    *,
    execution_delay_sec: int,
    start: pd.Timestamp | None,
    end: pd.Timestamp | None,
) -> tuple[list[dict[str, Any]], dict[str, int], RouterRules]:
    rules = RouterRules.from_config(cfg)
    features = build_router_features(data, rules)
    engine = NormalTrendLatchEngine(cfg)
    close = data["close"].astype(float)
    warmup = max(
        rules.base.normal_window_sec,
        rules.base.center_slope_sec,
        rules.base.retest_sec,
        3600,
    ) + 10
    diagnostics = {
        "featureRows": int(len(features)),
        "warmupRows": int(warmup),
        "emitted": 0,
        "outsideWindow": 0,
        "entryRejected": 0,
    }
    rows: list[dict[str, Any]] = []
    for index in range(warmup, len(features)):
        timestamp = pd.Timestamp(features.index[index])
        result = engine.step(timestamp, features.iloc[index], allow_emit=True)
        emitted = result.get("signal")
        if not emitted:
            continue
        diagnostics["emitted"] += 1
        emitted_time = utc(emitted["time"])
        if start is not None and emitted_time < start:
            diagnostics["outsideWindow"] += 1
            continue
        if end is not None and emitted_time >= end:
            diagnostics["outsideWindow"] += 1
            continue
        entry_target = emitted_time + pd.Timedelta(seconds=execution_delay_sec)
        entry = price_at_or_after(close, entry_target)
        if entry is None:
            diagnostics["entryRejected"] += 1
            continue
        settle = price_at_or_after(
            close,
            entry_target + pd.Timedelta(seconds=rules.base.horizon_sec),
        )
        if settle is None:
            diagnostics["entryRejected"] += 1
            continue
        entry_time, entry_price = entry
        settle_time, settle_price = settle
        signal = str(emitted["signal"])
        sign = 1.0 if signal == "UP" else -1.0
        signed_outcome_bps = (settle_price / entry_price - 1.0) * 10000.0 * sign
        row = features.iloc[index]
        rows.append(
            {
                "signal_time": emitted_time,
                "entry_time": entry_time,
                "settle_time": settle_time,
                "execution_delay_sec": execution_delay_sec,
                "signal": signal,
                "kind": emitted.get("kind"),
                "reason": emitted.get("reason"),
                "latch_delay_sec": emitted.get("delay_sec"),
                "entry": entry_price,
                "settle": settle_price,
                "signed_outcome_bps": signed_outcome_bps,
                "won": bool(signed_outcome_bps > 0.0),
                "state": row.get("state"),
                "band": row.get("sigma_bps"),
                "z": row.get("z"),
                "inside1_ratio": row.get("inside1_ratio"),
                "center_slope_bps": row.get("center_slope_bps"),
                "sigma_expand": row.get("sigma_expand"),
                "ret_600s_bps": row.get("ret_600s_bps"),
                "flow_120_mean": row.get("flow_120_mean"),
                "imbalance_60_mean": row.get("imbalance_60_mean"),
            }
        )
    return rows, diagnostics, rules


def run(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_config(Path(args.prod_config), args.strategy_id)
    rules = RouterRules.from_config(cfg)
    bars = load_second_bars(Path(args.seconds), include_shards=not args.no_shards)
    orderbook = read_orderbook(Path(args.orderbook), bars.index, rules.base.orderbook_max_age_sec)
    data = bars.join(orderbook, how="left")
    data = data[~data.index.duplicated(keep="last")].sort_index()
    start = utc(args.start) if args.start else None
    end = utc(args.end) if args.end else None
    rows, diagnostics, replay_rules = replay(
        data,
        cfg,
        execution_delay_sec=args.execution_delay_sec,
        start=start,
        end=end,
    )
    first = data.index.min()
    last = data.index.max()
    hours = (last - first).total_seconds() / 3600.0 if first is not None and last is not None else 0.0
    report = {
        "strategyId": args.strategy_id,
        "method": "Causal replay of NormalTrendLatchEngine and build_router_features shared with signal_btc.py.",
        "seconds": str(Path(args.seconds).resolve()),
        "orderbook": str(Path(args.orderbook).resolve()),
        "dataStart": first,
        "dataEnd": last,
        "hours": round(hours, 4),
        "window": {"start": start, "end": end},
        "execution": {
            "delaySec": args.execution_delay_sec,
            "amountU": args.amount,
            "payoutRate": args.payout_rate,
            "scanIntervalSec": replay_rules.execution_interval_sec,
            "latchSec": replay_rules.latch_sec,
            "minGapSec": replay_rules.base.min_gap_sec,
        },
        "rules": asdict(replay_rules),
        "diagnostics": diagnostics,
        "overall": metrics(rows, hours, args.amount, args.payout_rate),
        "trades": rows,
        "note": "This report must not be compared with old stateless liquidity_v2 backtests; the production latch engine is the source of truth.",
    }
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    Path(args.trades_out).write_text(
        pd.DataFrame(clean(rows)).to_csv(index=False),
        encoding="utf-8-sig",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", required=True)
    parser.add_argument("--orderbook", required=True)
    parser.add_argument("--prod-config", default=str(ROOT / "data" / "prod_config.json"))
    parser.add_argument("--strategy-id", default="BTC_10min_NORMAL_LIQ_OB_V2_QUALITY")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--execution-delay-sec", type=int, default=2)
    parser.add_argument("--amount", type=float, default=5.0)
    parser.add_argument("--payout-rate", type=float, default=0.8)
    parser.add_argument("--no-shards", action="store_true")
    parser.add_argument("--out", default=str(ROOT / "tmp" / "normal_trend_latch_backtest.json"))
    parser.add_argument("--trades-out", default=str(ROOT / "tmp" / "normal_trend_latch_trades.csv"))
    args = parser.parse_args()
    report = run(args)
    print(
        json.dumps(
            clean(
                {
                    "strategyId": report["strategyId"],
                    "method": report["method"],
                    "diagnostics": report["diagnostics"],
                    "overall": report["overall"],
                    "trades": report["trades"],
                }
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
