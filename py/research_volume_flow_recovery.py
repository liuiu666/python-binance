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
    build_features,
    load_local_data,
)
import research_adaptive_regime_switch as adaptive  # noqa: E402
import research_detailed_trend_states as detailed  # noqa: E402
from research_parameter_stability_audit import SOURCES  # noqa: E402


OUT_JSON = ROOT / "tmp" / "volume_flow_recovery_research.json"
OUT_CSV = ROOT / "tmp" / "volume_flow_recovery_trades.csv"


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


def won_for_signal(move_bps: float, signal: str) -> bool:
    return move_bps > 0.0 if signal == "UP" else move_bps < 0.0


def signal_sign(signal: str) -> float:
    return 1.0 if signal == "UP" else -1.0


def add_flow_features(data: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=data.index)
    volume = data["volume"].astype(float).clip(lower=0.0)
    buy = data["buy_qty"].astype(float).clip(lower=0.0)
    sell = data["sell_qty"].astype(float).clip(lower=0.0)
    out["vol60"] = volume.rolling(60, min_periods=10).sum()
    out["vol300"] = volume.rolling(300, min_periods=60).sum()
    out["vol_ratio60"] = out["vol60"] / (out["vol300"] / 5.0).replace(0, np.nan)
    buy60 = buy.rolling(60, min_periods=10).sum()
    sell60 = sell.rolling(60, min_periods=10).sum()
    buy300 = buy.rolling(300, min_periods=60).sum()
    sell300 = sell.rolling(300, min_periods=60).sum()
    out["flow60"] = (buy60 - sell60) / (buy60 + sell60).replace(0, np.nan)
    out["flow300"] = (buy300 - sell300) / (buy300 + sell300).replace(0, np.nan)
    out["flow_accel"] = out["flow60"] - out["flow300"]
    return out


def event_universe(data: pd.DataFrame) -> pd.DataFrame:
    cfg = adaptive.config()
    features = build_features(data, 600, cfg)
    flow_features = add_flow_features(data)
    close = data["close"].astype(float)
    high = data["high"].astype(float)
    low = data["low"].astype(float)
    ret5 = np.log(close / close.shift(300)) * 10000.0
    ret15 = np.log(close / close.shift(900)) * 10000.0
    ret30 = np.log(close / close.shift(1800)) * 10000.0
    ret60 = np.log(close / close.shift(3600)) * 10000.0
    range15 = (high.rolling(900, min_periods=300).max() / low.rolling(900, min_periods=300).min() - 1.0) * 10000.0
    range30 = (high.rolling(1800, min_periods=900).max() / low.rolling(1800, min_periods=900).min() - 1.0) * 10000.0
    z = features["z"]
    upper = ((z >= 0.8) & (z.shift(1) < 0.8)) | ((z <= 0.8) & (z.shift(1) > 0.8) & (z >= 0.0))
    lower = ((z <= -0.8) & (z.shift(1) > -0.8)) | ((z >= -0.8) & (z.shift(1) < -0.8) & (z <= 0.0))
    valid = (
        (np.arange(len(data)) >= 3605)
        & (np.arange(len(data)) < len(data) - 600)
        & features["inside1_ratio"].ge(0.45).to_numpy()
        & features["observed_pct"].ge(88.0).to_numpy()
        & features["sigma_bps"].between(1.0, 55.0).to_numpy()
        & features["sigma_expand"].le(1.2).to_numpy()
        & range30.between(35.0, 90.0).to_numpy()
    )

    rows: list[dict[str, Any]] = []
    for idx in np.flatnonzero(valid & (upper.to_numpy() | lower.to_numpy())):
        edge = "upper" if bool(upper.iloc[idx]) else "lower"
        state_values = (
            float(ret5.iloc[idx]),
            float(ret15.iloc[idx]),
            float(ret30.iloc[idx]),
            float(ret60.iloc[idx]),
            float(features["center_slope_bps"].iloc[idx]),
            float(range15.iloc[idx]),
        )
        if not all(math.isfinite(value) for value in state_values):
            continue
        state, score = adaptive.macro_state(*state_values)
        entry = float(close.iloc[idx])
        settle = float(close.iloc[idx + 600])
        move_bps = math.log(settle / entry) * 10000.0
        bid20 = float(features["bid_qty_20"].iloc[idx])
        ask20 = float(features["ask_qty_20"].iloc[idx])
        if idx < 60:
            continue
        prev_bid = float(features["bid_qty_20"].iloc[idx - 60])
        prev_ask = float(features["ask_qty_20"].iloc[idx - 60])
        bid_chg = bid20 / prev_bid - 1.0 if prev_bid > 0 else float("nan")
        ask_chg = ask20 / prev_ask - 1.0 if prev_ask > 0 else float("nan")
        base = {
            "time": data.index[idx],
            "time_shanghai": data.index[idx].tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
            "idx": int(idx),
            "state": state,
            "score": int(score),
            "edge": edge,
            "entry": round(entry, 4),
            "settle": round(settle, 4),
            "move_bps": round(move_bps, 4),
            "ret5_bps": round(state_values[0], 4),
            "ret15_bps": round(state_values[1], 4),
            "ret30_bps": round(state_values[2], 4),
            "ret60_bps": round(state_values[3], 4),
            "slope_bps": round(state_values[4], 4),
            "range15_bps": round(state_values[5], 4),
            "range30_bps": round(float(range30.iloc[idx]), 4),
            "z": round(float(z.iloc[idx]), 6),
            "sigma_bps": round(float(features["sigma_bps"].iloc[idx]), 6),
            "sigma_expand": round(float(features["sigma_expand"].iloc[idx]), 6),
            "imbalance_20": round(float(features["imbalance_20"].iloc[idx]), 6),
            "micro_bps": round(float(features["micro_bps"].iloc[idx]), 6),
            "book_flow60": round(float(features["flow_60"].iloc[idx]), 6),
            "bid_chg60": round(float(bid_chg), 6),
            "ask_chg60": round(float(ask_chg), 6),
            "vol_ratio60": round(float(flow_features["vol_ratio60"].iloc[idx]), 6),
            "flow60": round(float(flow_features["flow60"].iloc[idx]), 6),
            "flow300": round(float(flow_features["flow300"].iloc[idx]), 6),
            "flow_accel": round(float(flow_features["flow_accel"].iloc[idx]), 6),
        }
        for signal in ("UP", "DOWN"):
            sign = signal_sign(signal)
            row = dict(base)
            row["signal"] = signal
            row["won"] = won_for_signal(move_bps, signal)
            row["pnl"] = 4 if row["won"] else -5
            row["signed_flow60"] = round(sign * row["flow60"], 6)
            row["signed_flow300"] = round(sign * row["flow300"], 6)
            row["signed_flow_accel"] = round(sign * row["flow_accel"], 6)
            row["signed_imb20"] = round(sign * row["imbalance_20"], 6)
            row["signed_micro"] = round(sign * row["micro_bps"], 6)
            row["support_chg60"] = row["bid_chg60"] if signal == "UP" else row["ask_chg60"]
            row["ob_votes"] = adaptive.orderbook_votes(signal, row["imbalance_20"], row["micro_bps"], row["book_flow60"])
            rows.append(row)
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


def select_variant(rows: pd.DataFrame, variant: str) -> pd.DataFrame:
    if rows.empty:
        return rows
    support_ok = rows["support_chg60"].between(-0.5, 2.0)
    down_core = (
        rows["signal"].eq("DOWN")
        & rows["state"].eq("drift_down")
        & rows["edge"].eq("lower")
        & (rows["ret60_bps"] < 0)
        & (rows["ob_votes"] >= 2)
        & support_ok
    )
    if variant == "stable_down_break":
        return rows[down_core].copy()

    flat_up_recovery = (
        rows["signal"].eq("UP")
        & rows["state"].eq("flat")
        & rows["edge"].eq("lower")
        & (rows["ret60_bps"] >= -20)
        & (rows["signed_flow60"] >= -0.20)
        & (rows["signed_flow_accel"] >= -0.20)
        & (rows["vol_ratio60"] <= 2.2)
        & support_ok
    )
    flat_down_recovery = (
        rows["signal"].eq("DOWN")
        & rows["state"].eq("flat")
        & rows["edge"].eq("upper")
        & (rows["ret60_bps"] <= 20)
        & (rows["signed_flow60"] >= -0.20)
        & (rows["signed_flow_accel"] >= -0.20)
        & (rows["vol_ratio60"] <= 2.2)
        & support_ok
    )
    drift_up_fail = (
        rows["signal"].eq("DOWN")
        & rows["state"].eq("drift_up")
        & rows["edge"].eq("lower")
        & (rows["ob_votes"] >= 2)
        & (rows["signed_flow60"] >= -0.10)
        & (rows["support_chg60"] <= 2.0)
    )
    if variant == "plus_flat_flow":
        return rows[down_core | flat_up_recovery | flat_down_recovery].copy()
    if variant == "plus_drift_up_fail":
        return rows[down_core | drift_up_fail].copy()
    if variant == "plus_all_recovery":
        return rows[down_core | flat_up_recovery | flat_down_recovery | drift_up_fail].copy()
    raise ValueError(f"unknown variant {variant}")


def run() -> dict[str, Any]:
    variants = ("stable_down_break", "plus_flat_flow", "plus_drift_up_fail", "plus_all_recovery")
    reports = []
    all_trades = []
    for source_name, seconds, orderbook in SOURCES:
        data = load_local_data(seconds, orderbook)
        hours = (data.index.max() - data.index.min()).total_seconds() / 3600.0
        events = event_universe(data)
        source_report = {
            "source": source_name,
            "start": data.index.min(),
            "end": data.index.max(),
            "hours": round(hours, 4),
            "events": len(events),
            "variants": {},
        }
        for variant in variants:
            trades = apply_cooldown(select_variant(events, variant))
            trades["variant"] = variant
            trades["source"] = source_name
            source_report["variants"][variant] = detailed.metrics(trades, hours)
            if not trades.empty:
                source_report["variants"][variant]["byBranch"] = {
                    str(key): detailed.metrics(group, hours)
                    for key, group in trades.groupby(["state", "edge", "signal"])
                }
            all_trades.append(trades)
        reports.append(source_report)
    trades_out = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    trades_out.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "method": "Recover filtered trades only when volume and active-flow conditions confirm the branch. All variants run on the same event universe before cooldown.",
        "reports": reports,
        "tradesCsv": str(OUT_CSV),
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


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


if __name__ == "__main__":
    result = run()
    print(json.dumps(clean(result), ensure_ascii=False, indent=2))
