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

from research_normal_liquidity_orderbook import (  # noqa: E402
    LiquidityNormalConfig,
    build_features,
    load_local_data,
)
from research_parameter_stability_audit import SOURCES  # noqa: E402


OUT_JSON = ROOT / "tmp" / "all_branch_matrix_research.json"
OUT_BRANCH_CSV = ROOT / "tmp" / "all_branch_matrix.csv"
OUT_TRANSITION_CSV = ROOT / "tmp" / "all_branch_transition_matrix.csv"
OUT_SNAPSHOT_CSV = ROOT / "tmp" / "all_branch_snapshots.csv"


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


def cfg() -> LiquidityNormalConfig:
    return LiquidityNormalConfig(
        normal_window_sec=600,
        z_entry=0.8,
        z_reclaim=0.8,
        retest_sec=120,
        inside_min=0.45,
        observed_min_pct=88.0,
        center_slope_sec=300,
        center_slope_max_bps=999.0,
        sigma_min_bps=1.0,
        sigma_max_bps=80.0,
        sigma_expand_max=3.0,
        signal_gap_sec=600,
        horizon_sec=600,
        amount=5.0,
    )


def bucket(value: float, cuts: list[tuple[float, str]], default: str) -> str:
    if not math.isfinite(value):
        return "na"
    for limit, label in cuts:
        if value < limit:
            return label
    return default


def trend_bucket(ret10: float, ret30: float, ret60: float) -> str:
    up_votes = int(ret10 >= 8.0) + int(ret30 >= 18.0) + int(ret60 >= 28.0)
    down_votes = int(ret10 <= -8.0) + int(ret30 <= -18.0) + int(ret60 <= -28.0)
    if up_votes >= 2 and down_votes == 0:
        return "trend_up"
    if down_votes >= 2 and up_votes == 0:
        return "trend_down"
    if abs(ret10) <= 7.0 and abs(ret30) <= 18.0 and abs(ret60) <= 28.0:
        return "flat"
    if up_votes > down_votes:
        return "drift_up"
    if down_votes > up_votes:
        return "drift_down"
    return "transition"


def normal_pos(z: float) -> str:
    if not math.isfinite(z):
        return "z_na"
    if z >= 1.2:
        return "above_upper"
    if z >= 0.8:
        return "upper_edge"
    if z >= 0.25:
        return "upper_inside"
    if z > -0.25:
        return "center"
    if z > -0.8:
        return "lower_inside"
    if z > -1.2:
        return "lower_edge"
    return "below_lower"


def sprint_bucket(sign: str, run_len: int, run_move: float) -> str:
    if sign == "UP" and 2 <= run_len <= 4 and 7.0 <= run_move <= 28.0:
        return "up_sprint"
    if sign == "DOWN" and 2 <= run_len <= 4 and -28.0 <= run_move <= -7.0:
        return "down_sprint"
    if sign == "UP" and run_len >= 5:
        return "up_walk"
    if sign == "DOWN" and run_len >= 5:
        return "down_walk"
    return "none"


def side_bucket(value: float, pos: float, neg: float, prefix: str) -> str:
    if not math.isfinite(value):
        return f"{prefix}_na"
    if value >= pos:
        return f"{prefix}_up"
    if value <= neg:
        return f"{prefix}_down"
    return f"{prefix}_neutral"


def build_minute_snapshots(data: pd.DataFrame, source: str) -> pd.DataFrame:
    config = cfg()
    features = build_features(data, 600, config)
    agg = {
        "close": ["first", "max", "min", "last"],
        "volume": "sum",
        "buy_qty": "sum",
        "sell_qty": "sum",
        "bid_qty_20": "mean",
        "ask_qty_20": "mean",
        "imbalance_20": "mean",
        "microprice_edge_bps": "mean",
        "spread_bps": "mean",
    }
    minutes = data.resample("1min").agg(agg)
    minutes.columns = [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "buy_qty",
        "sell_qty",
        "bid20",
        "ask20",
        "imb20",
        "micro",
        "spread",
    ]
    minutes = minutes.dropna(subset=["open", "close"]).copy()
    close = minutes["close"].astype(float)
    for mins in (1, 3, 5, 10, 30, 60):
        minutes[f"ret{mins}_bps"] = (close / close.shift(mins) - 1.0) * 10000.0
    minutes["future10_bps"] = (close.shift(-10) / close - 1.0) * 10000.0
    minutes["range10_bps"] = (
        minutes["high"].rolling(10, min_periods=5).max()
        / minutes["low"].rolling(10, min_periods=5).min()
        - 1.0
    ) * 10000.0
    minutes["range30_bps"] = (
        minutes["high"].rolling(30, min_periods=10).max()
        / minutes["low"].rolling(30, min_periods=10).min()
        - 1.0
    ) * 10000.0
    minutes["sigma10_bps"] = close.rolling(10, min_periods=5).std() / close * 10000.0
    minutes["sigma30_bps"] = close.rolling(30, min_periods=10).std() / close * 10000.0
    minutes["vol_ratio30"] = minutes["volume"] / minutes["volume"].rolling(30, min_periods=10).mean()
    flow = (minutes["buy_qty"] - minutes["sell_qty"]) / (
        minutes["buy_qty"] + minutes["sell_qty"]
    ).replace(0, np.nan)
    minutes["flow1"] = flow
    minutes["flow5"] = flow.rolling(5, min_periods=2).mean()
    minutes["bid20_chg5"] = minutes["bid20"] / minutes["bid20"].shift(5).replace(0, np.nan) - 1.0
    minutes["ask20_chg5"] = minutes["ask20"] / minutes["ask20"].shift(5).replace(0, np.nan) - 1.0

    sign = pd.Series("FLAT", index=minutes.index, dtype="object")
    sign[minutes["ret1_bps"] > 1.0] = "UP"
    sign[minutes["ret1_bps"] < -1.0] = "DOWN"
    minutes["minute_sign"] = sign

    run_lengths: list[int] = []
    run_moves: list[float] = []
    current_sign: str | None = None
    current_len = 0
    current_move = 0.0
    for _, row in minutes.iterrows():
        minute_sign = str(row["minute_sign"])
        ret1 = float(row["ret1_bps"]) if math.isfinite(float(row["ret1_bps"])) else 0.0
        if minute_sign in {"UP", "DOWN"}:
            if minute_sign == current_sign:
                current_len += 1
                current_move += ret1
            else:
                current_sign = minute_sign
                current_len = 1
                current_move = ret1
        else:
            current_sign = None
            current_len = 0
            current_move = 0.0
        run_lengths.append(current_len)
        run_moves.append(current_move)
    minutes["run_len"] = run_lengths
    minutes["run_move_bps"] = run_moves

    empty_feature = {
        "idx": np.nan,
        "z": np.nan,
        "inside1_ratio": np.nan,
        "observed_pct": np.nan,
        "center_slope_bps": np.nan,
        "sigma_bps": np.nan,
        "sigma_expand": np.nan,
    }
    feature_rows = []
    for minute_time in minutes.index:
        target = minute_time + pd.Timedelta(seconds=59)
        idx = int(data.index.searchsorted(target, side="right") - 1)
        if idx < 3605 or idx >= len(data) - 600:
            feature_rows.append(dict(empty_feature))
            continue
        if abs((data.index[idx] - target).total_seconds()) > 3:
            feature_rows.append(dict(empty_feature))
            continue
        feature_rows.append(
            {
                "idx": idx,
                "z": float(features["z"].iloc[idx]),
                "inside1_ratio": float(features["inside1_ratio"].iloc[idx]),
                "observed_pct": float(features["observed_pct"].iloc[idx]),
                "center_slope_bps": float(features["center_slope_bps"].iloc[idx]),
                "sigma_bps": float(features["sigma_bps"].iloc[idx]),
                "sigma_expand": float(features["sigma_expand"].iloc[idx]),
            }
        )
    extra = pd.DataFrame(
        feature_rows,
        index=minutes.index,
        columns=[
            "idx",
            "z",
            "inside1_ratio",
            "observed_pct",
            "center_slope_bps",
            "sigma_bps",
            "sigma_expand",
        ],
    )
    minutes = pd.concat([minutes, extra], axis=1)
    minutes = minutes.replace([np.inf, -np.inf], np.nan).dropna(
        subset=[
            "future10_bps",
            "ret10_bps",
            "ret30_bps",
            "ret60_bps",
            "range10_bps",
            "range30_bps",
            "sigma10_bps",
            "vol_ratio30",
            "flow1",
            "flow5",
            "imb20",
            "micro",
            "z",
            "sigma_bps",
            "sigma_expand",
        ]
    )

    out = pd.DataFrame(index=minutes.index)
    out["source"] = source
    out["time"] = minutes.index
    out["time_shanghai"] = [ts.tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S") for ts in minutes.index]
    out["future10_bps"] = minutes["future10_bps"]
    out["up_win"] = minutes["future10_bps"] > 0
    out["down_win"] = minutes["future10_bps"] < 0
    out["trend"] = [
        trend_bucket(float(row.ret10_bps), float(row.ret30_bps), float(row.ret60_bps))
        for row in minutes.itertuples()
    ]
    out["volatility"] = [
        bucket(float(value), [(3.0, "sigma_low"), (5.0, "sigma_midlow"), (8.0, "sigma_mid"), (12.0, "sigma_high")], "sigma_extreme")
        for value in minutes["sigma10_bps"]
    ]
    out["range"] = [
        bucket(float(value), [(16.0, "range_tight"), (30.0, "range_normal"), (45.0, "range_wide"), (70.0, "range_hot")], "range_extreme")
        for value in minutes["range10_bps"]
    ]
    out["normal_pos"] = [normal_pos(float(value)) for value in minutes["z"]]
    out["normal_quality"] = np.where(
        (minutes["inside1_ratio"] >= 0.45)
        & (minutes["observed_pct"] >= 88.0)
        & (minutes["sigma_expand"] <= 1.25),
        "normal_ready",
        "normal_weak",
    )
    out["sprint"] = [
        sprint_bucket(str(row.minute_sign), int(row.run_len), float(row.run_move_bps))
        for row in minutes.itertuples()
    ]
    out["flow"] = [side_bucket(float(value), 0.12, -0.12, "flow") for value in minutes["flow5"]]
    out["book"] = [side_bucket(float(value), 0.08, -0.08, "book") for value in minutes["imb20"]]
    out["volume"] = [
        bucket(float(value), [(0.7, "vol_low"), (1.25, "vol_normal"), (1.8, "vol_high")], "vol_extreme")
        for value in minutes["vol_ratio30"]
    ]
    out["branch"] = (
        out["trend"]
        + "|"
        + out["volatility"]
        + "|"
        + out["range"]
        + "|"
        + out["normal_quality"]
        + "|"
        + out["normal_pos"]
        + "|"
        + out["sprint"]
        + "|"
        + out["flow"]
        + "|"
        + out["book"]
        + "|"
        + out["volume"]
    )
    out["market_state"] = (
        out["trend"]
        + "|"
        + out["volatility"]
        + "|"
        + out["normal_pos"]
        + "|"
        + out["sprint"]
    )
    out["prev_market_state"] = out["market_state"].shift(1)
    out["transition"] = out["prev_market_state"] + "=>" + out["market_state"]

    for col in (
        "ret10_bps",
        "ret30_bps",
        "ret60_bps",
        "range10_bps",
        "range30_bps",
        "sigma10_bps",
        "vol_ratio30",
        "flow5",
        "imb20",
        "z",
        "sigma_expand",
    ):
        out[col] = minutes[col]
    return out.dropna(subset=["transition"])


def summarize_group(group: pd.DataFrame) -> dict[str, Any]:
    n = int(len(group))
    up_wins = int(group["up_win"].sum())
    down_wins = int(group["down_win"].sum())
    up_rate = up_wins / n * 100.0 if n else 0.0
    down_rate = down_wins / n * 100.0 if n else 0.0
    best_signal = "UP" if up_rate >= down_rate else "DOWN"
    best_rate = max(up_rate, down_rate)
    pnl = up_wins * 4 - (n - up_wins) * 5 if best_signal == "UP" else down_wins * 4 - (n - down_wins) * 5
    return {
        "samples": n,
        "upWinRate": round(up_rate, 2),
        "downWinRate": round(down_rate, 2),
        "bestSignal": best_signal,
        "bestWinRate": round(best_rate, 2),
        "bestPnlU_noCooldown": int(pnl),
        "future10MedianBps": round(float(group["future10_bps"].median()), 4),
        "future10MeanBps": round(float(group["future10_bps"].mean()), 4),
        "sources": int(group["source"].nunique()),
    }


def summarize_matrix(snapshots: pd.DataFrame, key: str) -> pd.DataFrame:
    rows = []
    for name, group in snapshots.groupby(key):
        item = summarize_group(group)
        item[key] = name
        for source_name, source_group in group.groupby("source"):
            source_item = summarize_group(source_group)
            item[f"{source_name}_n"] = source_item["samples"]
            item[f"{source_name}_bestRate"] = source_item["bestWinRate"]
            item[f"{source_name}_bestSignal"] = source_item["bestSignal"]
            item[f"{source_name}_pnl"] = source_item["bestPnlU_noCooldown"]
        rows.append(item)
    return pd.DataFrame(rows).sort_values(
        ["sources", "bestPnlU_noCooldown", "samples"], ascending=[False, False, False]
    )


def run() -> dict[str, Any]:
    snapshots = []
    source_info = []
    for source_name, seconds, orderbook in SOURCES:
        data = load_local_data(seconds, orderbook)
        source_info.append(
            {
                "source": source_name,
                "start": data.index.min(),
                "end": data.index.max(),
                "hours": round((data.index.max() - data.index.min()).total_seconds() / 3600.0, 4),
            }
        )
        snapshots.append(build_minute_snapshots(data, source_name))
    all_snapshots = pd.concat(snapshots, ignore_index=True)
    branch_matrix = summarize_matrix(all_snapshots, "branch")
    transition_matrix = summarize_matrix(all_snapshots, "transition")

    branch_matrix.to_csv(OUT_BRANCH_CSV, index=False, encoding="utf-8-sig")
    transition_matrix.to_csv(OUT_TRANSITION_CSV, index=False, encoding="utf-8-sig")
    all_snapshots.to_csv(OUT_SNAPSHOT_CSV, index=False, encoding="utf-8-sig")

    tradable_branches = branch_matrix[
        (branch_matrix["samples"] >= 20)
        & (branch_matrix["sources"] >= 2)
        & (branch_matrix["bestWinRate"] >= 58.0)
        & (branch_matrix["bestPnlU_noCooldown"] > 0)
    ].copy()
    robust_branches = branch_matrix[
        (branch_matrix["samples"] >= 30)
        & (branch_matrix["sources"] == 3)
        & (branch_matrix["bestWinRate"] >= 56.0)
        & (branch_matrix["bestPnlU_noCooldown"] > 0)
    ].copy()
    output = {
        "method": "All-minute branch matrix. No branch is preselected. Each row asks: if this branch appears, is UP or DOWN better 10 minutes later? PnL is no-cooldown diagnostic only.",
        "sources": source_info,
        "snapshots": int(len(all_snapshots)),
        "branchCount": int(len(branch_matrix)),
        "transitionCount": int(len(transition_matrix)),
        "robustBranches": clean(robust_branches.head(30).to_dict("records")),
        "tradableBranches": clean(tradable_branches.head(50).to_dict("records")),
        "topBranches": clean(branch_matrix.head(30).to_dict("records")),
        "topTransitions": clean(transition_matrix.head(30).to_dict("records")),
        "files": {
            "branchMatrix": str(OUT_BRANCH_CSV),
            "transitionMatrix": str(OUT_TRANSITION_CSV),
            "snapshots": str(OUT_SNAPSHOT_CSV),
        },
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
