from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "tmp" / "v1_orderbook_bidwall_trap_trades.csv"
OUT_JSON = ROOT / "tmp" / "v1_orderbook_quality_v2_research.json"
OUT_TRADES = ROOT / "tmp" / "v1_orderbook_quality_v2_trades.csv"


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


def payout(won: bool) -> int:
    return 4 if bool(won) else -5


def metrics(rows: pd.DataFrame) -> dict[str, Any]:
    rows = rows.sort_values(["dataset", "time"])
    n = int(len(rows))
    wins = int(rows["won"].astype(bool).sum()) if n else 0
    pnls = [payout(won) for won in rows["won"].astype(bool)] if n else []
    equity = peak = max_dd = loss_streak = max_loss = 0
    for won, pnl in zip(rows["won"].astype(bool), pnls):
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if won:
            loss_streak = 0
        else:
            loss_streak += 1
            max_loss = max(max_loss, loss_streak)
    return {
        "trades": n,
        "wins": wins,
        "winRate": round(wins / n * 100.0, 2) if n else 0.0,
        "pnl": int(sum(pnls)),
        "maxDrawdownU": int(max_dd),
        "maxLoss": int(max_loss),
    }


def split_metrics(rows: pd.DataFrame, key: str) -> dict[str, Any]:
    return {str(name): metrics(group) for name, group in rows.groupby(key, sort=True)}


def flip_direction(rows: pd.DataFrame, mask: pd.Series, reason: str) -> pd.DataFrame:
    out = rows.copy()
    idx = out[mask].index
    out.loc[idx, "signal"] = np.where(out.loc[idx, "signal"].eq("DOWN"), "UP", "DOWN")
    out.loc[idx, "reason"] = reason
    out.loc[idx, "won"] = ~out.loc[idx, "won"].astype(bool)
    return out


def scenario_report(name: str, rows: pd.DataFrame, note: str) -> dict[str, Any]:
    return {
        "scenario": name,
        "note": note,
        "overall": metrics(rows),
        "byDataset": split_metrics(rows, "dataset"),
        "byDay": split_metrics(rows.assign(day=rows["time"].dt.strftime("%Y-%m-%d")), "day"),
        "bySide": split_metrics(rows, "signal"),
        "tradesPerDay": round(len(rows) / 69.29 * 24.0, 2),
    }


def run() -> dict[str, Any]:
    df = pd.read_csv(INPUT, parse_dates=["time"])
    base = df[df["scenario"] == "flip_bidwall_trap_down"].copy()
    base["won"] = base["won"].astype(bool)
    down_bid_fade70 = (base["signal"] == "DOWN") & (base["bid20_60s_chg"] <= -0.7)
    up_flow_negative = (base["signal"] == "UP") & (base["flow_60"] <= -0.063)

    skip_down_bid_fade70 = base[~down_bid_fade70].copy()
    skip_up_flow_negative = base[~up_flow_negative].copy()
    conservative = base[~(down_bid_fade70 | up_flow_negative)].copy()
    aggressive = flip_direction(base[~up_flow_negative].copy(), down_bid_fade70[~up_flow_negative], "down_bid_fade70_flip_up")

    reports = [
        scenario_report("current_bidwall_trap_flip", base, "Current online candidate: lower UP bidwall trap flips to DOWN."),
        scenario_report(
            "skip_down_bid_fade70",
            skip_down_bid_fade70,
            "Skip DOWN when bid_qty_20 fell at least 70% over 60s; this weak DOWN bucket was 11 trades / 36.36%.",
        ),
        scenario_report(
            "skip_up_flow_negative",
            skip_up_flow_negative,
            "Skip UP when 60s taker flow remains negative; rebound support is not confirmed.",
        ),
        scenario_report(
            "quality_v2_conservative",
            conservative,
            "Use both quality vetoes; highest stable win rate without adding another flip.",
        ),
        scenario_report(
            "quality_v2_aggressive",
            aggressive,
            "Also flip DOWN bid-fade70 to UP; more trades and PnL, but drawdown rises.",
        ),
    ]
    all_trades = []
    for name, rows in [
        ("current_bidwall_trap_flip", base),
        ("skip_down_bid_fade70", skip_down_bid_fade70),
        ("skip_up_flow_negative", skip_up_flow_negative),
        ("quality_v2_conservative", conservative),
        ("quality_v2_aggressive", aggressive),
    ]:
        next_rows = rows.copy()
        next_rows["quality_v2_scenario"] = name
        all_trades.append(next_rows)

    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "input": str(INPUT),
        "rules": {
            "existing_bidwall_trap_flip": "lower_fake_break_reclaim UP + ret_300s_bps<=-5 + bid20_60s_chg>2 => DOWN",
            "new_veto_1": "DOWN with bid20_60s_chg<=-0.7 is skipped; bid-side depth vanished too fast.",
            "new_veto_2": "UP with flow_60<=-0.063 is skipped; rebound has no taker-flow confirmation.",
        },
        "reports": reports,
        "recommendation": "quality_v2_conservative is the safer research candidate; do not deploy until user approves.",
        "tradesCsv": str(OUT_TRADES),
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.concat(all_trades, ignore_index=True).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    return output


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False))
