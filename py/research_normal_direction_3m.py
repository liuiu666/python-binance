from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from second_backtest.data import audit_second_csv, load_second_bars
from second_backtest.execution import execute_signals
from second_backtest.metrics import compact_metrics, payout_for_horizon, split_metrics
from second_backtest.strategies import (
    SecondNormalDirection3mConfig,
    generate_normal_direction_3m_signals,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT / "tmp" / "latest_server_pull_20260622" / "btcusdt_1s_trades_latest_tail120k.csv"
DEFAULT_OUT = ROOT / "tmp" / "latest_server_pull_20260622" / "normal_direction_3m_research.json"


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
    if all_m["trades"] < 10 or all_m["winRate"] is None:
        return -999999.0
    daily = metrics.get("byUtcDay") or []
    bad_days = sum(1 for day in daily if day["trades"] >= 3 and (day["winRate"] or 0) < 55)
    last_penalty = 50.0 if last["trades"] >= 3 and (last["winRate"] or 0) < 55 else 0.0
    before_penalty = 30.0 if before["trades"] >= 5 and (before["winRate"] or 0) < 55 else 0.0
    return round(
        all_m["pnl"]
        + all_m["winRate"] * 2.2
        + min(all_m["tradesPerDay"], 18) * 2.0
        - all_m["maxLoss"] * 20.0
        - bad_days * 20.0
        - last_penalty
        - before_penalty,
        4,
    )


def _run_case(bars: pd.DataFrame, cfg: SecondNormalDirection3mConfig) -> dict:
    raw = generate_normal_direction_3m_signals(bars, cfg, apply_config_gap=True)
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
            "confirmSec": cfg.confirm_sec,
            "probEdge": cfg.prob_edge,
            "minSigmaBps": cfg.min_sigma_bps,
            "maxSigmaBps": cfg.max_sigma_bps,
            "minRangeBps": cfg.min_range_bps,
            "maxRangeBps": cfg.max_range_bps,
            "minConfirmMoveBps": cfg.min_confirm_move_bps,
            "minConfirmSlopeBps": cfg.min_confirm_slope_bps,
            "minConfirmFlowImbalance": cfg.min_confirm_flow_imbalance,
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
                "pUp": row.get("p_up"),
                "z": row.get("z_score"),
                "rangeBps": row.get("range_bps"),
                "sigma10Bps": row.get("sigma10_bps"),
                "confirmDelaySec": row.get("confirm_delay_sec"),
                "confirmMoveBps": row.get("confirm_move_bps"),
                "confirmSlopeBps": row.get("confirm_slope_bps"),
                "confirmFlow": row.get("confirm_flow_imbalance"),
            }
            for row in executed[-12:]
        ],
    }


def _grid() -> list[SecondNormalDirection3mConfig]:
    out = []
    for prob_edge in (0.58, 0.60):
        for min_sigma in (8.0,):
            for max_sigma in (35.0,):
                for min_range in (12.0,):
                    for min_move in (3.0,):
                        for min_slope in (2.0,):
                            for min_flow in (0.05, 0.08):
                                out.append(
                                    SecondNormalDirection3mConfig(
                                        strategy_id=(
                                            "NORMAL_DIRECTION_3M_"
                                            f"{prob_edge}_{min_sigma}_{max_sigma}_"
                                            f"{min_range}_{min_move}_{min_slope}_{min_flow}"
                                        ),
                                        lookback_sec=600,
                                        horizon_sec=600,
                                        confirm_sec=180,
                                        signal_gap_sec=600,
                                        prob_edge=prob_edge,
                                        min_sigma_bps=min_sigma,
                                        max_sigma_bps=max_sigma,
                                        min_range_bps=min_range,
                                        max_range_bps=80.0,
                                        min_confirm_move_bps=min_move,
                                        min_confirm_slope_bps=min_slope,
                                        min_confirm_flow_imbalance=min_flow,
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
            "Normal direction strategy: estimate mean/sigma from the last 10 minutes of second returns, "
            "predict 10-minute UP/DOWN when normal probability has enough edge, then require confirmation "
            "within 3 minutes by aligned move, slope, and taker-flow before entry."
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
