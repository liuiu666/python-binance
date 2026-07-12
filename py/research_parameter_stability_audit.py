from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_normal_liquidity_orderbook import load_local_data  # noqa: E402
import research_detailed_trend_states as detailed  # noqa: E402


OUT_JSON = ROOT / "tmp" / "parameter_stability_audit.json"
OUT_CSV = ROOT / "tmp" / "parameter_stability_audit.csv"

SOURCES = (
    (
        "2026-07-05_06",
        ROOT / "tmp" / "latest_pull_20260706_2130" / "data" / "btcusdt_1s_trades.csv",
        ROOT / "tmp" / "latest_pull_20260706_2130" / "data" / "btcusdt_orderbook_1s.csv",
    ),
    (
        "2026-07-08_09",
        ROOT / "tmp" / "latest_live_pull_20260709_101331" / "data" / "btcusdt_1s_trades.csv",
        ROOT / "tmp" / "latest_live_pull_20260709_101331" / "data" / "btcusdt_orderbook_1s.csv",
    ),
    (
        "2026-07-09_10",
        ROOT / "tmp" / "latest_pull_20260710_203217" / "data" / "btcusdt_1s_trades.csv",
        ROOT / "tmp" / "latest_pull_20260710_203217" / "data" / "btcusdt_orderbook_1s.csv",
    ),
)


Gate = Callable[[pd.DataFrame], pd.Series]


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if np.isfinite(number) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def gates() -> dict[str, Gate]:
    return {
        "baseline": lambda rows: pd.Series(True, index=rows.index),
        "slope_abs_le_4": lambda rows: rows["slope_bps"].abs() <= 4.0,
        "slope_abs_le_6": lambda rows: rows["slope_bps"].abs() <= 6.0,
        "slope_abs_le_8": lambda rows: rows["slope_bps"].abs() <= 8.0,
        "slope_abs_le_12": lambda rows: rows["slope_bps"].abs() <= 12.0,
        "z_abs_ge_1_0": lambda rows: rows["z"].abs() >= 1.0,
        "z_abs_ge_1_2": lambda rows: rows["z"].abs() >= 1.2,
        "z_abs_ge_1_5": lambda rows: rows["z"].abs() >= 1.5,
        "sigma_3_10": lambda rows: rows["sigma_bps"].between(3.0, 10.0),
        "sigma_4_10": lambda rows: rows["sigma_bps"].between(4.0, 10.0),
        "sigma_4_8": lambda rows: rows["sigma_bps"].between(4.0, 8.0),
        "range30_35_45": lambda rows: rows["range30_bps"].between(35.0, 45.0),
        "range30_35_50": lambda rows: rows["range30_bps"].between(35.0, 50.0),
        "speed_signed_le_5": lambda rows: rows["signed_ret60_bps"] <= 5.0,
        "speed_signed_m10_5": lambda rows: rows["signed_ret60_bps"].between(-10.0, 5.0),
        "support_chg_m05_1": lambda rows: rows["support_chg60"].between(-0.5, 1.0),
        "support_chg_m05_2": lambda rows: rows["support_chg60"].between(-0.5, 2.0),
        "flat_only": lambda rows: rows["trend_state"].eq("balanced_flat"),
        "no_mixed_transition": lambda rows: ~rows["trend_state"].eq("mixed_transition"),
    }


def summarize_source(rows: pd.DataFrame, hours: float) -> dict[str, Any]:
    return detailed.metrics(detailed.apply_cooldown(rows), hours)


def run() -> dict[str, Any]:
    source_rows: dict[str, tuple[pd.DataFrame, float, dict[str, Any]]] = {}
    for name, seconds, orderbook in SOURCES:
        data = load_local_data(seconds, orderbook)
        hours = (data.index.max() - data.index.min()).total_seconds() / 3600.0
        candidates = detailed.enrich(detailed.base_candidates(data), data)
        source_rows[name] = (
            candidates,
            hours,
            {
                "seconds": str(seconds),
                "orderbook": str(orderbook),
                "start": data.index.min(),
                "end": data.index.max(),
                "hours": round(hours, 4),
                "candidateRows": len(candidates),
            },
        )

    reports = []
    for gate_name, gate in gates().items():
        by_source = {}
        totals = []
        for source_name, (candidates, hours, _) in source_rows.items():
            selected = candidates[gate(candidates).fillna(False)].copy()
            result = summarize_source(selected, hours)
            by_source[source_name] = result
            totals.append(result)
        reports.append(
            {
                "gate": gate_name,
                "bySource": by_source,
                "totalTrades": int(sum(item["trades"] for item in totals)),
                "weightedWins": int(sum(item["wins"] for item in totals)),
                "weightedWinRate": round(
                    sum(item["wins"] for item in totals)
                    / max(1, sum(item["trades"] for item in totals))
                    * 100.0,
                    2,
                ),
                "totalPnlU": int(sum(item["pnlU"] for item in totals)),
                "worstSourcePnlU": int(min(item["pnlU"] for item in totals)),
                "worstSourceWinRate": round(min(item["winRate"] for item in totals), 2),
                "maxSourceDrawdownU": int(max(item["maxDrawdownU"] for item in totals)),
                "positiveSources": int(sum(item["pnlU"] > 0 for item in totals)),
            }
        )

    flat_rows = []
    for report in reports:
        row = {key: value for key, value in report.items() if key != "bySource"}
        for source_name, source_result in report["bySource"].items():
            row[f"{source_name}_trades"] = source_result["trades"]
            row[f"{source_name}_winRate"] = source_result["winRate"]
            row[f"{source_name}_pnlU"] = source_result["pnlU"]
            row[f"{source_name}_maxDrawdownU"] = source_result["maxDrawdownU"]
        flat_rows.append(row)
    pd.DataFrame(flat_rows).to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "method": "One-factor-at-a-time audit. Each gate is applied before the 600-second cooldown on three distinct market periods.",
        "sources": {name: info for name, (_, _, info) in source_rows.items()},
        "reports": reports,
        "csv": str(OUT_CSV),
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    result = run()
    print(json.dumps(clean(result), ensure_ascii=False, indent=2))
