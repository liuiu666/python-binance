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

from research_normal_liquidity_orderbook import build_features, load_local_data  # noqa: E402
import research_adaptive_regime_switch as adaptive  # noqa: E402
import research_detailed_trend_states as detailed  # noqa: E402
from research_parameter_stability_audit import SOURCES  # noqa: E402


OUT_JSON = ROOT / "tmp" / "composite_regime_strategy_research.json"
OUT_CSV = ROOT / "tmp" / "composite_regime_strategy_trades.csv"


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


def build_minute_features(data: pd.DataFrame) -> pd.DataFrame:
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
    for period in (1, 3, 5, 10, 15, 30, 60):
        minutes[f"ret{period}_bps"] = (close / close.shift(period) - 1.0) * 10000.0
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
    minutes["flow_accel"] = minutes["flow1"] - minutes["flow5"]
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
    return minutes


def second_index_for_minute(data: pd.DataFrame, minute_time: pd.Timestamp) -> int | None:
    target = minute_time + pd.Timedelta(seconds=59)
    idx = int(data.index.searchsorted(target, side="right") - 1)
    if idx < 0 or idx >= len(data) - 600:
        return None
    if abs((data.index[idx] - target).total_seconds()) > 3:
        return None
    return idx


def orderbook_votes(signal: str, imbalance: float, micro: float, flow: float) -> int:
    sign = 1.0 if signal == "UP" else -1.0
    return int(sign * imbalance >= 0.08) + int(sign * micro >= 0.001) + int(sign * flow >= 0.08)


def append_trade(
    rows: list[dict[str, Any]],
    data: pd.DataFrame,
    features: pd.DataFrame,
    minutes: pd.DataFrame,
    minute_time: pd.Timestamp,
    idx: int,
    signal: str,
    branch: str,
    reason: str,
) -> None:
    entry = float(data["close"].iloc[idx])
    settle = float(data["close"].iloc[idx + 600])
    move_bps = math.log(settle / entry) * 10000.0
    won = move_bps > 0.0 if signal == "UP" else move_bps < 0.0
    mrow = minutes.loc[minute_time]
    rows.append(
        {
            "time": data.index[idx],
            "time_shanghai": data.index[idx].tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
            "idx": idx,
            "signal": signal,
            "branch": branch,
            "reason": reason,
            "entry": round(entry, 4),
            "settle": round(settle, 4),
            "move_bps": round(move_bps, 4),
            "won": bool(won),
            "pnl": 4 if won else -5,
            "z": round(float(features["z"].iloc[idx]), 6),
            "sigma_bps": round(float(features["sigma_bps"].iloc[idx]), 6),
            "sigma_expand": round(float(features["sigma_expand"].iloc[idx]), 6),
            "inside1_ratio": round(float(features["inside1_ratio"].iloc[idx]), 6),
            "ret3_bps": round(float(mrow["ret3_bps"]), 4),
            "ret10_bps": round(float(mrow["ret10_bps"]), 4),
            "ret30_bps": round(float(mrow["ret30_bps"]), 4),
            "ret60_bps": round(float(mrow["ret60_bps"]), 4),
            "range10_bps": round(float(mrow["range10_bps"]), 4),
            "range30_bps": round(float(mrow["range30_bps"]), 4),
            "sigma10_bps": round(float(mrow["sigma10_bps"]), 4),
            "vol_ratio30": round(float(mrow["vol_ratio30"]), 6),
            "flow1": round(float(mrow["flow1"]), 6),
            "flow5": round(float(mrow["flow5"]), 6),
            "flow_accel": round(float(mrow["flow_accel"]), 6),
            "imb20": round(float(mrow["imb20"]), 6),
            "micro": round(float(mrow["micro"]), 6),
            "bid20_chg5": round(float(mrow["bid20_chg5"]), 6),
            "ask20_chg5": round(float(mrow["ask20_chg5"]), 6),
            "run_len": int(mrow["run_len"]),
            "run_move_bps": round(float(mrow["run_move_bps"]), 4),
        }
    )


def build_candidates(data: pd.DataFrame) -> pd.DataFrame:
    cfg = adaptive.config()
    features = build_features(data, 600, cfg)
    minutes = build_minute_features(data)
    rows: list[dict[str, Any]] = []
    z = features["z"]
    zmax120 = z.rolling(120, min_periods=20).max()
    zmin120 = z.rolling(120, min_periods=20).min()

    for minute_time, mrow in minutes.iterrows():
        idx = second_index_for_minute(data, minute_time)
        if idx is None or idx < 3605:
            continue
        needed = (
            "ret3_bps",
            "ret10_bps",
            "ret30_bps",
            "ret60_bps",
            "range10_bps",
            "range30_bps",
            "sigma10_bps",
            "vol_ratio30",
            "flow1",
            "flow5",
            "flow_accel",
            "bid20_chg5",
            "ask20_chg5",
        )
        if any(not math.isfinite(float(mrow[name])) for name in needed):
            continue
        if not (
            float(features["observed_pct"].iloc[idx]) >= 88.0
            and float(features["inside1_ratio"].iloc[idx]) >= 0.45
            and 1.0 <= float(features["sigma_bps"].iloc[idx]) <= 55.0
            and float(features["sigma_expand"].iloc[idx]) <= 1.25
        ):
            continue

        current_z = float(z.iloc[idx])
        ret30 = float(mrow["ret30_bps"])
        ret60 = float(mrow["ret60_bps"])
        range10 = float(mrow["range10_bps"])
        range30 = float(mrow["range30_bps"])
        sigma10 = float(mrow["sigma10_bps"])
        flow1 = float(mrow["flow1"])
        flow5 = float(mrow["flow5"])
        flow_accel = float(mrow["flow_accel"])
        bid_chg5 = float(mrow["bid20_chg5"])
        ask_chg5 = float(mrow["ask20_chg5"])
        imbalance = float(mrow["imb20"])
        micro = float(mrow["micro"])
        run_len = int(mrow["run_len"])
        run_move = float(mrow["run_move_bps"])
        vol_ratio = float(mrow["vol_ratio30"])
        long_up = ret30 >= 22.0 and ret60 >= 28.0
        long_down = ret30 <= -22.0 and ret60 <= -28.0
        moderate_vol = 3.0 <= sigma10 <= 8.5 and 16.0 <= range10 <= 42.0 and 28.0 <= range30 <= 70.0

        # 1) Old robust branch: down drift, lower normal event, order book confirms continuation.
        if (
            ret60 < 0.0
            and ret30 <= -12.0
            and current_z <= -0.75
            and moderate_vol
            and orderbook_votes("DOWN", imbalance, micro, float(features["flow_60"].iloc[idx])) >= 2
            and ask_chg5 <= 2.0
        ):
            append_trade(
                rows,
                data,
                features,
                minutes,
                minute_time,
                idx,
                "DOWN",
                "stable_down_break",
                "下行漂移里跌到下沿，订单薄也支持继续向下",
            )
            continue

        # 2) Short sprint exhaustion. Most 2-3 minute runs reverted in the sample,
        # but skip strong same-direction long trend and explosive volume.
        if (
            str(mrow["minute_sign"]) == "UP"
            and 2 <= run_len <= 4
            and 7.0 <= run_move <= 24.0
            and not long_up
            and moderate_vol
            and vol_ratio <= 1.8
            and flow1 >= 0.12
            and flow_accel <= 0.28
            and ask_chg5 >= -0.35
            and imbalance <= 0.22
            and current_z >= 0.45
        ):
            append_trade(
                rows,
                data,
                features,
                minutes,
                minute_time,
                idx,
                "DOWN",
                "up_sprint_exhaustion",
                "短促连续上涨后买流没有继续加速，且未处于强上涨长趋势",
            )
            continue

        if (
            str(mrow["minute_sign"]) == "DOWN"
            and 2 <= run_len <= 4
            and -24.0 <= run_move <= -7.0
            and not long_down
            and moderate_vol
            and vol_ratio <= 1.8
            and flow1 <= -0.12
            and flow_accel >= -0.28
            and bid_chg5 >= -0.35
            and imbalance >= -0.22
            and current_z <= -0.45
        ):
            append_trade(
                rows,
                data,
                features,
                minutes,
                minute_time,
                idx,
                "UP",
                "down_sprint_exhaustion",
                "短促连续下跌后卖流没有继续加速，且未处于强下跌长趋势",
            )
            continue

        # 3) Normal failed breakout reclaim. This is deliberately stricter than
        # touching the normal band: price must leave and then return inside.
        chop = (
            abs(float(mrow["ret10_bps"])) <= 7.0
            and range10 <= 30.0
            and sigma10 <= 5.0
            and vol_ratio <= 1.25
            and abs(ret30) <= 18.0
        )
        if (
            chop
            and float(zmax120.iloc[idx]) >= 1.05
            and 0.05 <= current_z <= 0.75
            and flow1 <= 0.12
            and ask_chg5 >= -0.45
        ):
            append_trade(
                rows,
                data,
                features,
                minutes,
                minute_time,
                idx,
                "DOWN",
                "normal_upper_reclaim",
                "震荡区间上沿假突破后回到区间内",
            )
            continue
        if (
            chop
            and float(zmin120.iloc[idx]) <= -1.05
            and -0.75 <= current_z <= -0.05
            and flow1 >= -0.12
            and bid_chg5 >= -0.45
        ):
            append_trade(
                rows,
                data,
                features,
                minutes,
                minute_time,
                idx,
                "UP",
                "normal_lower_reclaim",
                "震荡区间下沿假跌破后回到区间内",
            )
            continue

    return pd.DataFrame(rows)


def apply_cooldown(rows: pd.DataFrame) -> pd.DataFrame:
    if rows.empty:
        return rows
    accepted: list[dict[str, Any]] = []
    last_time: pd.Timestamp | None = None
    for row in rows.sort_values("time").to_dict("records"):
        timestamp = pd.Timestamp(row["time"])
        if last_time is not None and (timestamp - last_time).total_seconds() < 600:
            continue
        accepted.append(row)
        last_time = timestamp
    return pd.DataFrame(accepted)


def run() -> dict[str, Any]:
    reports = []
    all_trades = []
    for source_name, seconds, orderbook in SOURCES:
        data = load_local_data(seconds, orderbook)
        hours = (data.index.max() - data.index.min()).total_seconds() / 3600.0
        raw = build_candidates(data)
        trades = apply_cooldown(raw)
        trades["source"] = source_name
        all_trades.append(trades)
        source_report = {
            "source": source_name,
            "start": data.index.min(),
            "end": data.index.max(),
            "hours": round(hours, 4),
            "rawCandidates": int(len(raw)),
            "result": detailed.metrics(trades, hours),
            "byBranch": {
                str(branch): detailed.metrics(group, hours)
                for branch, group in trades.groupby("branch")
            }
            if not trades.empty
            else {},
        }
        reports.append(source_report)

    combined = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    if not combined.empty:
        combined.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    total_hours = sum(float(report["hours"]) for report in reports)
    output = {
        "method": "Composite regime strategy: stable down break + short sprint exhaustion + strict normal failed-break reclaim. One trade max per 10 minutes.",
        "reports": reports,
        "total": detailed.metrics(combined, total_hours),
        "byBranchTotal": {
            str(branch): detailed.metrics(group, total_hours)
            for branch, group in combined.groupby("branch")
        }
        if not combined.empty
        else {},
        "csv": str(OUT_CSV),
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
