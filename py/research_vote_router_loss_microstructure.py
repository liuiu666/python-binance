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

import research_all_branch_matrix as matrix  # noqa: E402


TRADES = ROOT / "tmp" / "all_branch_vote_router_trades.csv"
OUT_JSON = ROOT / "tmp" / "vote_router_loss_microstructure.json"
OUT_CSV = ROOT / "tmp" / "vote_router_loss_microstructure_trades.csv"
OUT_DIFF_CSV = ROOT / "tmp" / "vote_router_loss_microstructure_diffs.csv"


def clean(value: Any) -> Any:
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


def load_recommended_trades() -> pd.DataFrame:
    trades = pd.read_csv(TRADES)
    trades = trades[
        (trades["ruleSet"].eq("balanced"))
        & (trades["minVotes"].eq(2))
        & (trades["requireNoConflict"].eq(False))
    ].copy()
    trades["time"] = pd.to_datetime(trades["time"], utc=True)
    return trades.sort_values(["testSource", "time"]).reset_index(drop=True)


def add_source_features(source_name: str, seconds: Path, orderbook: Path, trades: pd.DataFrame) -> pd.DataFrame:
    data = matrix.load_local_data(seconds, orderbook)
    minutes = matrix.build_minute_snapshots(data, source_name).copy()
    minutes["time"] = pd.to_datetime(minutes["time"], utc=True)
    minutes = minutes.set_index("time").sort_index()

    close = data["close"].astype(float)
    volume = data["volume"].astype(float).clip(lower=0)
    buy = data["buy_qty"].astype(float).clip(lower=0)
    sell = data["sell_qty"].astype(float).clip(lower=0)
    bid20 = data["bid_qty_20"].astype(float)
    ask20 = data["ask_qty_20"].astype(float)
    imbalance = data["imbalance_20"].astype(float)
    micro = data["microprice_edge_bps"].astype(float)

    features: list[dict[str, Any]] = []
    for trade in trades.to_dict("records"):
        timestamp = pd.Timestamp(trade["time"])
        idx = int(data.index.searchsorted(timestamp, side="right") - 1)
        if idx < 1800 or idx >= len(data) - 600:
            continue
        row = dict(trade)
        for sec in (30, 60, 180, 300, 600, 900, 1800):
            row[f"sec_ret{sec}_bps"] = float((close.iloc[idx] / close.iloc[idx - sec] - 1.0) * 10000.0)
            row[f"sec_range{sec}_bps"] = float(
                (data["high"].iloc[idx - sec + 1 : idx + 1].max() / data["low"].iloc[idx - sec + 1 : idx + 1].min() - 1.0)
                * 10000.0
            )
            row[f"sec_vol{sec}"] = float(volume.iloc[idx - sec + 1 : idx + 1].sum())
            b = float(buy.iloc[idx - sec + 1 : idx + 1].sum())
            s = float(sell.iloc[idx - sec + 1 : idx + 1].sum())
            row[f"sec_flow{sec}"] = (b - s) / (b + s) if b + s > 0 else np.nan
        for sec in (30, 60, 180, 300):
            prev = idx - sec
            row[f"bid20_chg{sec}"] = float(bid20.iloc[idx] / bid20.iloc[prev] - 1.0) if bid20.iloc[prev] > 0 else np.nan
            row[f"ask20_chg{sec}"] = float(ask20.iloc[idx] / ask20.iloc[prev] - 1.0) if ask20.iloc[prev] > 0 else np.nan
        for lag_min in (1, 3, 5, 10):
            lag_time = timestamp - pd.Timedelta(minutes=lag_min)
            if lag_time in minutes.index:
                lag = minutes.loc[lag_time]
                row[f"lag{lag_min}_trend"] = lag["trend"]
                row[f"lag{lag_min}_normal_pos"] = lag["normal_pos"]
                row[f"lag{lag_min}_future10_bps"] = float(lag["future10_bps"])
                row[f"lag{lag_min}_z"] = float(lag["z"])
                row[f"lag{lag_min}_flow"] = lag["flow"]
                row[f"lag{lag_min}_book"] = lag["book"]
        row["imb20_now"] = float(imbalance.iloc[idx])
        row["micro_now"] = float(micro.iloc[idx])
        features.append(row)
    return pd.DataFrame(features)


def summarize_case(rows: pd.DataFrame, case_name: str) -> dict[str, Any]:
    if rows.empty:
        return {"case": case_name, "trades": 0}
    numeric_cols = [
        col
        for col in rows.columns
        if (
            col.startswith("sec_ret")
            or col.startswith("sec_range")
            or col.startswith("sec_flow")
            or col.startswith("bid20_chg")
            or col.startswith("ask20_chg")
            or col in {"future10_bps", "z", "sigma10_bps", "range10_bps", "vol_ratio30", "imb20_now", "micro_now"}
        )
    ]
    result = {
        "case": case_name,
        "trades": int(len(rows)),
        "wins": int(rows["won"].astype(bool).sum()),
        "winRate": round(float(rows["won"].astype(bool).mean() * 100.0), 2),
    }
    for col in numeric_cols:
        wins = rows[rows["won"].astype(bool)][col].dropna()
        losses = rows[~rows["won"].astype(bool)][col].dropna()
        if len(wins) == 0 or len(losses) == 0:
            continue
        result[col] = {
            "winMedian": round(float(wins.median()), 6),
            "lossMedian": round(float(losses.median()), 6),
            "deltaLossMinusWin": round(float(losses.median() - wins.median()), 6),
        }
    return result


def find_simple_rules(rows: pd.DataFrame, case_name: str) -> list[dict[str, Any]]:
    if rows.empty:
        return []
    candidates = []
    features = [
        "sec_ret60_bps",
        "sec_ret180_bps",
        "sec_ret300_bps",
        "sec_ret600_bps",
        "sec_flow60",
        "sec_flow180",
        "sec_flow300",
        "bid20_chg60",
        "ask20_chg60",
        "bid20_chg180",
        "ask20_chg180",
        "imb20_now",
        "micro_now",
        "z",
        "sigma10_bps",
        "range10_bps",
        "vol_ratio30",
    ]
    base_n = len(rows)
    base_wins = int(rows["won"].astype(bool).sum())
    for feature in features:
        if feature not in rows.columns:
            continue
        values = rows[feature].replace([np.inf, -np.inf], np.nan).dropna()
        if len(values) < 8:
            continue
        thresholds = sorted(set(float(values.quantile(q)) for q in (0.2, 0.35, 0.5, 0.65, 0.8)))
        for threshold in thresholds:
            for op in ("<=", ">="):
                if op == "<=":
                    selected = rows[rows[feature] <= threshold]
                else:
                    selected = rows[rows[feature] >= threshold]
                if len(selected) < max(6, int(base_n * 0.18)):
                    continue
                wins = int(selected["won"].astype(bool).sum())
                pnl = wins * 4 - (len(selected) - wins) * 5
                base_pnl = base_wins * 4 - (base_n - base_wins) * 5
                candidates.append(
                    {
                        "case": case_name,
                        "rule": f"{feature} {op} {threshold:.6g}",
                        "trades": int(len(selected)),
                        "winRate": round(wins / len(selected) * 100.0, 2),
                        "pnlU": int(pnl),
                        "removed": int(base_n - len(selected)),
                        "baseTrades": int(base_n),
                        "baseWinRate": round(base_wins / base_n * 100.0, 2),
                        "basePnlU": int(base_pnl),
                    }
                )
    return sorted(candidates, key=lambda item: (item["pnlU"], item["winRate"], item["trades"]), reverse=True)[:20]


def run() -> dict[str, Any]:
    trades = load_recommended_trades()
    enriched = []
    for source_name, seconds, orderbook in matrix.SOURCES:
        source_trades = trades[trades["testSource"].eq(source_name)].copy()
        if source_trades.empty:
            continue
        enriched.append(add_source_features(source_name, seconds, orderbook, source_trades))
    data = pd.concat(enriched, ignore_index=True)
    data.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    cases = {
        "trend_up_above_upper_up_sprint_DOWN": data[
            data["signal"].eq("DOWN")
            & data["trend"].eq("trend_up")
            & data["normal_pos"].eq("above_upper")
            & data["sprint"].eq("up_sprint")
        ],
        "trend_down_lower_UP": data[
            data["signal"].eq("UP")
            & data["trend"].eq("trend_down")
            & data["normal_pos"].isin(["below_lower", "lower_inside"])
        ],
        "trend_down_lower_inside_UP": data[
            data["signal"].eq("UP")
            & data["trend"].eq("trend_down")
            & data["normal_pos"].eq("lower_inside")
        ],
        "flat_mid_lowerinside_DOWN": data[
            data["signal"].eq("DOWN")
            & data["trend"].eq("flat")
            & data["volatility"].eq("sigma_mid")
            & data["normal_pos"].eq("lower_inside")
        ],
    }
    summaries = [summarize_case(rows, name) for name, rows in cases.items()]
    rules = []
    for name, rows in cases.items():
        rules.extend(find_simple_rules(rows, name))
    pd.DataFrame(rules).to_csv(OUT_DIFF_CSV, index=False, encoding="utf-8-sig")
    output = {
        "method": "Compare wins vs losses inside the recommended balanced-2 vote router, with second-level pre-entry features.",
        "totalTrades": int(len(data)),
        "summaries": summaries,
        "candidateRules": rules[:40],
        "files": {
            "enrichedTrades": str(OUT_CSV),
            "candidateRules": str(OUT_DIFF_CSV),
        },
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
