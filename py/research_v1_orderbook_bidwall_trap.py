from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_v1_orderbook_up_quality as base  # noqa: E402
from research_normal_liquidity_orderbook import (  # noqa: E402
    LiquidityNormalConfig,
    build_features,
    generate_signals,
)


DATASETS = {
    "2026-07-05_2026-07-06": ROOT / "tmp" / "latest_pull_20260706_2130" / "data",
    "2026-07-07_2026-07-08": ROOT / "tmp" / "latest_pull_20260708_204204" / "data",
}
OUT_JSON = ROOT / "tmp" / "v1_orderbook_bidwall_trap_research.json"
OUT_TRADES = ROOT / "tmp" / "v1_orderbook_bidwall_trap_trades.csv"


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        value = float(value)
        return value if math.isfinite(value) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def v1_config() -> LiquidityNormalConfig:
    return LiquidityNormalConfig(
        normal_window_sec=600,
        z_entry=1.2,
        z_reclaim=0.85,
        mode="reclaim",
        retest_sec=120,
        inside_min=0.55,
        observed_min_pct=88.0,
        center_slope_sec=300,
        center_slope_max_bps=8.0,
        sigma_min_bps=5.0,
        sigma_max_bps=55.0,
        sigma_expand_max=1.9,
        ob_imbalance_min=0.08,
        micro_min_bps=0.001,
        wall_ratio_min=1.0,
        flow_guard=0.12,
        true_break_flow=0.28,
        true_break_imbalance=0.28,
        signal_gap_sec=600,
        horizon_sec=600,
        amount=5.0,
    )


def is_bidwall_trap(row: dict[str, Any]) -> bool:
    return (
        row.get("signal") == "UP"
        and float(row.get("ret_300s_bps", 0.0)) <= -5.0
        and float(row.get("bid20_60s_chg", 0.0)) > 2.0
    )


def flip_to_down(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["original_signal"] = item["signal"]
    item["original_reason"] = item["reason"]
    item["signal"] = "DOWN"
    item["reason"] = "lower_reclaim_bidwall_trap_flip_down"
    item["trap_rule"] = "ret300<=-5bps_and_bid20_60s_chg>2x"
    item["won"] = float(item["settle"]) < float(item["entry"])
    return item


def compact(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        key: summary[key]
        for key in ("trades", "winRate", "pnl", "maxDrawdownU", "maxLoss", "byDay")
    }


def load_rows(data_dir: Path) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    data = base.load_data(data_dir)
    cfg = v1_config()
    features = build_features(data, cfg.normal_window_sec, cfg)
    rows = base.add_quality_features(generate_signals(data, features, cfg), data, features)
    return data, rows


def run() -> dict[str, Any]:
    trade_rows: list[dict[str, Any]] = []
    datasets: dict[str, Any] = {}

    for name, data_dir in DATASETS.items():
        data, rows = load_rows(data_dir)
        baseline = base.apply_gap(rows)
        skip_trap = base.apply_gap([row for row in rows if not is_bidwall_trap(row)])
        flip_trap = base.apply_gap([flip_to_down(row) if is_bidwall_trap(row) else row for row in rows])
        raw_trap = [row for row in rows if is_bidwall_trap(row)]

        for scenario, scenario_rows in (
            ("baseline", baseline),
            ("skip_bidwall_trap", skip_trap),
            ("flip_bidwall_trap_down", flip_trap),
        ):
            for row in scenario_rows:
                item = dict(row)
                item["dataset"] = name
                item["scenario"] = scenario
                item["bidwall_trap"] = is_bidwall_trap(row)
                trade_rows.append(item)

        datasets[name] = {
            "data": {
                "dir": str(data_dir),
                "rows": int(len(data)),
                "start": data.index.min().isoformat(),
                "end": data.index.max().isoformat(),
                "hours": round((data.index.max() - data.index.min()).total_seconds() / 3600.0, 2),
                "observedPct": round(float(data["observed"].mean() * 100.0), 4),
            },
            "baseline": compact(base.summarize(baseline)),
            "skipBidwallTrap": compact(base.summarize(skip_trap)),
            "flipBidwallTrapDown": compact(base.summarize(flip_trap)),
            "rawBidwallTrapOriginalUp": compact(base.summarize(raw_trap)),
            "flipBySide": [
                {"side": side, **compact(base.summarize([row for row in flip_trap if row["signal"] == side]))}
                for side in ("UP", "DOWN")
            ],
        }

    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "rule": {
            "name": "lower_reclaim_bidwall_trap",
            "when": "V1 lower_fake_break_reclaim gives UP, but price is still down <= -5bp over 300s and bid_qty_20 grew more than 2x in 60s.",
            "interpretation": "Falling price plus sudden bid wall growth is treated as fake support / trapped bids, not clean rebound.",
        },
        "datasets": datasets,
        "tradesCsv": str(OUT_TRADES),
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(trade_rows).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    return output


if __name__ == "__main__":
    result = run()
    print(json.dumps(clean(result), ensure_ascii=False))
