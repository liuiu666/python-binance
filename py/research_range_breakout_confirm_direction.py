from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from second_backtest.data import audit_second_csv, load_second_bars
from second_backtest.execution import execute_signals
from second_backtest.metrics import compact_metrics, payout_for_horizon, split_metrics
from second_backtest.strategies import (
    SecondRangeBreakoutConfirmConfig,
    generate_range_breakout_confirm_signals,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT / "tmp" / "latest_server_pull_20260622" / "btcusdt_1s_trades_latest_tail120k.csv"
DEFAULT_OUT = ROOT / "tmp" / "latest_server_pull_20260622" / "range_breakout_confirm_direction_research.json"


def _side_metrics(trades: list[dict], start: pd.Timestamp, end: pd.Timestamp, amount: float) -> dict:
    out = {}
    for side in ("UP", "DOWN"):
        rows = [row for row in trades if row.get("signal") == side]
        out[side] = compact_metrics(split_metrics(rows, start, end, amount=amount, payout_rate=payout_for_horizon(600)))["all"]
    return out


def _score(metrics: dict) -> float:
    all_m = metrics["all"]
    before = metrics["beforeLast24h"]
    last = metrics["last24h"]
    if all_m["trades"] < 8 or all_m["winRate"] is None:
        return -999999.0
    daily = metrics.get("byUtcDay") or []
    bad_days = sum(1 for day in daily if day["trades"] >= 3 and (day["winRate"] or 0) < 55)
    last_penalty = 60.0 if last["trades"] >= 3 and (last["winRate"] or 0) < 55 else 0.0
    before_penalty = 40.0 if before["trades"] >= 5 and (before["winRate"] or 0) < 55 else 0.0
    return round(
        all_m["pnl"]
        + all_m["winRate"] * 2.5
        + min(all_m["tradesPerDay"], 12) * 3.0
        - all_m["maxLoss"] * 22.0
        - bad_days * 25.0
        - last_penalty
        - before_penalty,
        4,
    )


def _run_case(bars: pd.DataFrame, cfg: SecondRangeBreakoutConfirmConfig) -> dict:
    raw = generate_range_breakout_confirm_signals(bars, cfg, apply_config_gap=True)
    executed, rejected = execute_signals(
        raw,
        per_strategy_lock=True,
        global_lock_sec=0,
        cooldown_sec=cfg.horizon_sec,
        use_horizon_as_lock=True,
    )
    metrics = split_metrics(
        executed,
        bars.index.min(),
        bars.index.max(),
        amount=cfg.amount,
        payout_rate=payout_for_horizon(cfg.horizon_sec),
    )
    compact = compact_metrics(metrics)
    return {
        "params": {
            "lookbackSec": cfg.lookback_sec,
            "zEntry": cfg.z_entry,
            "confirmSec": cfg.confirm_sec,
            "holdZ": cfg.hold_z,
            "minHoldRatio": cfg.min_hold_ratio,
            "preSlopeSec": cfg.pre_slope_sec,
            "confirmSlopeSec": cfg.confirm_slope_sec,
            "minPreSlopeBps": cfg.min_pre_slope_bps,
            "minConfirmSlopeBps": cfg.min_confirm_slope_bps,
            "minFlowImbalance": cfg.min_flow_imbalance,
            "minConfirmFlowImbalance": cfg.min_confirm_flow_imbalance,
            "minVolumeRatio": cfg.min_volume_ratio,
            "minVolatilityRatio": cfg.min_volatility_ratio,
            "maxAgeBeyondSec": cfg.max_age_beyond_sec,
            "signalGapSec": cfg.signal_gap_sec,
        },
        "score": _score(compact),
        "rawSignals": len(raw),
        "executed": len(executed),
        "rejected": len(rejected),
        "metrics": compact,
        "bySide": _side_metrics(executed, bars.index.min(), bars.index.max(), cfg.amount),
        "sampleTrades": [
            {
                "time": row["time"].isoformat(),
                "signal": row["signal"],
                "won": bool(row["won"]),
                "entry": round(float(row["entry"]), 2),
                "settle": round(float(row["settle"]), 2),
                "breakZ": row.get("break_z"),
                "confirmZ": row.get("confirm_z"),
                "holdRatio": row.get("hold_ratio"),
                "preSlopeBps": row.get("pre_slope_bps"),
                "confirmSlopeBps": row.get("confirm_slope_bps"),
                "confirmFlow": row.get("confirm_flow_imbalance"),
            }
            for row in executed[-12:]
        ],
    }


def _grid() -> list[SecondRangeBreakoutConfirmConfig]:
    out = []
    for lookback in (1800, 3600):
        for z_entry in (2.0, 2.2):
            for confirm_sec in (60, 120):
                for hold_z in (1.0,):
                    for min_hold in (0.75, 0.90):
                        for min_confirm_slope in (2.0, 4.0):
                            for min_confirm_flow in (0.08,):
                                out.append(
                                    SecondRangeBreakoutConfirmConfig(
                                        strategy_id=(
                                            "RANGE_BREAKOUT_CONFIRM_"
                                            f"{lookback}_{z_entry}_{confirm_sec}_{hold_z}_{min_hold}_"
                                            f"{min_confirm_slope}_{min_confirm_flow}"
                                        ),
                                        lookback_sec=lookback,
                                        horizon_sec=600,
                                        signal_gap_sec=600,
                                        z_entry=z_entry,
                                        confirm_sec=confirm_sec,
                                        hold_z=hold_z,
                                        min_hold_ratio=min_hold,
                                        pre_slope_sec=300,
                                        confirm_slope_sec=min(confirm_sec, 120),
                                        min_pre_slope_bps=8.0,
                                        min_confirm_slope_bps=min_confirm_slope,
                                        min_flow_imbalance=0.12,
                                        min_confirm_flow_imbalance=min_confirm_flow,
                                        min_volume_ratio=0.45,
                                        min_volatility_ratio=0.55,
                                        max_age_beyond_sec=180,
                                        amount=15,
                                    )
                                )
    return out


def build_report(args: argparse.Namespace) -> dict:
    csv = Path(args.csv)
    bars = load_second_bars(csv, include_shards=False)
    cases = [_run_case(bars, cfg) for cfg in _grid()]
    ranked = sorted(
        cases,
        key=lambda item: (
            item["score"],
            item["metrics"]["all"]["pnl"],
            item["metrics"]["all"]["winRate"] or 0,
            -item["metrics"]["all"]["maxLoss"],
        ),
        reverse=True,
    )
    return {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "source": str(csv),
        "sample": {
            "start": bars.index.min().isoformat(),
            "end": bars.index.max().isoformat(),
            "hours": round((bars.index.max() - bars.index.min()).total_seconds() / 3600.0, 2),
            "rows": int(len(bars)),
            "observedPct": round(float(bars["observed"].mean() * 100), 2),
            "audit": audit_second_csv(csv),
        },
        "method": (
            "Confirmed direction strategy: detect a break outside rolling normal range, "
            "wait for a causal confirmation window, require most seconds to hold outside "
            "the old range plus aligned confirmation slope and taker-flow, then settle 10 minutes later."
        ),
        "rankedTop": ranked[:40],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = build_report(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report["sample"], ensure_ascii=False))
    for item in report["rankedTop"][:15]:
        metrics = item["metrics"]["all"]
        print(json.dumps({"params": item["params"], "score": item["score"], "all": metrics, "bySide": item["bySide"]}, ensure_ascii=False))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
