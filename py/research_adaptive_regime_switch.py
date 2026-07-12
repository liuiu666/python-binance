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
import research_detailed_trend_states as detailed  # noqa: E402
from research_parameter_stability_audit import SOURCES  # noqa: E402


OUT_JSON = ROOT / "tmp" / "adaptive_regime_switch_backtest.json"
OUT_CSV = ROOT / "tmp" / "adaptive_regime_switch_trades.csv"


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


def config() -> LiquidityNormalConfig:
    return LiquidityNormalConfig(
        normal_window_sec=600,
        z_entry=0.8,
        z_reclaim=0.8,
        mode="hybrid",
        retest_sec=120,
        inside_min=0.45,
        observed_min_pct=88.0,
        center_slope_sec=300,
        center_slope_max_bps=999.0,
        sigma_min_bps=1.0,
        sigma_max_bps=55.0,
        sigma_expand_max=1.2,
        signal_gap_sec=600,
        horizon_sec=600,
        amount=5.0,
    )


def macro_state(r5: float, r15: float, r30: float, r60: float, slope: float, range15: float) -> tuple[str, int]:
    up_votes = int(r5 >= 5.0) + int(r15 >= 10.0) + int(r30 >= 15.0) + int(r60 >= 20.0)
    down_votes = int(r5 <= -5.0) + int(r15 <= -10.0) + int(r30 <= -15.0) + int(r60 <= -20.0)
    score = up_votes - down_votes
    if range15 >= 90.0 or abs(r5) >= 25.0:
        if score >= 2:
            return "shock_up", score
        if score <= -2:
            return "shock_down", score
        return "shock_transition", score
    if up_votes >= 3 and down_votes == 0:
        return "trend_up", score
    if down_votes >= 3 and up_votes == 0:
        return "trend_down", score
    if abs(r15) <= 10.0 and abs(r30) <= 14.0 and abs(r60) <= 24.0 and abs(slope) <= 4.0:
        return "flat", score
    if score >= 2:
        return "drift_up", score
    if score <= -2:
        return "drift_down", score
    return "transition", score


def orderbook_votes(signal: str, imbalance: float, micro: float, flow: float) -> int:
    sign = 1.0 if signal == "UP" else -1.0
    return int(sign * imbalance >= 0.08) + int(sign * micro >= 0.001) + int(sign * flow >= 0.08)


def decide(policy: str, state: str, edge: str, score: int, ob_votes: dict[str, int]) -> tuple[str | None, str]:
    if policy == "stable_down_break":
        if state == "drift_down" and edge == "lower" and ob_votes["DOWN"] >= 2:
            return "DOWN", "stable_down_break"
        return None, "stable_case_wait"

    if state == "flat":
        return ("DOWN", "flat_upper_fade") if edge == "upper" else ("UP", "flat_lower_fade")

    if policy in {"robust_lower_ob0", "robust_lower_ob1", "robust_lower_ob2"}:
        if state in {"drift_down", "drift_up"} and edge == "lower":
            required = int(policy[-1])
            if ob_votes["DOWN"] >= required:
                return "DOWN", f"{state}_lower_down_ob{required}"
        return None, "robust_case_wait"

    upward = state in {"trend_up", "drift_up"}
    downward = state in {"trend_down", "drift_down"}
    if not upward and not downward:
        return None, "state_wait"

    trend_signal = "UP" if upward else "DOWN"
    pullback_edge = "lower" if upward else "upper"
    breakout_edge = "upper" if upward else "lower"
    if edge == pullback_edge:
        if policy in {"pullback_only", "hybrid_ob1", "hybrid_ob2", "trend_all"}:
            return trend_signal, f"{state}_pullback"
    if edge == breakout_edge:
        if policy == "trend_all":
            return trend_signal, f"{state}_breakout"
        required = 1 if policy == "hybrid_ob1" else 2
        if policy in {"hybrid_ob1", "hybrid_ob2"} and ob_votes[trend_signal] >= required:
            return trend_signal, f"{state}_breakout_ob{required}"
    return None, "edge_wait"


def build_candidates(data: pd.DataFrame, policy: str) -> pd.DataFrame:
    cfg = config()
    features = build_features(data, 600, cfg)
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
    # Treat band contact/re-entry as an event. A persistent band walk must not
    # generate another order every time the global cooldown expires.
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

    rows = []
    for idx in np.flatnonzero(valid & (upper.to_numpy() | lower.to_numpy())):
        edge = "upper" if bool(upper.iloc[idx]) else "lower"
        values = (
            float(ret5.iloc[idx]),
            float(ret15.iloc[idx]),
            float(ret30.iloc[idx]),
            float(ret60.iloc[idx]),
            float(features["center_slope_bps"].iloc[idx]),
            float(range15.iloc[idx]),
        )
        if not all(math.isfinite(value) for value in values):
            continue
        state, score = macro_state(*values)
        imbalance = float(features["imbalance_20"].iloc[idx])
        micro = float(features["micro_bps"].iloc[idx])
        flow = float(features["flow_60"].iloc[idx])
        ob_votes = {
            "UP": orderbook_votes("UP", imbalance, micro, flow),
            "DOWN": orderbook_votes("DOWN", imbalance, micro, flow),
        }
        signal, branch = decide(policy, state, edge, score, ob_votes)
        if signal is None:
            continue

        bid20 = float(features["bid_qty_20"].iloc[idx])
        ask20 = float(features["ask_qty_20"].iloc[idx])
        if idx < 60:
            continue
        prev_bid = float(features["bid_qty_20"].iloc[idx - 60])
        prev_ask = float(features["ask_qty_20"].iloc[idx - 60])
        bid_chg = bid20 / prev_bid - 1.0 if prev_bid > 0 else float("nan")
        ask_chg = ask20 / prev_ask - 1.0 if prev_ask > 0 else float("nan")
        support_chg = bid_chg if signal == "UP" else ask_chg
        if not math.isfinite(support_chg) or not (-0.5 < support_chg < 3.0):
            continue
        if policy == "stable_down_break" and (values[3] >= 0.0 or support_chg > 2.0):
            continue

        entry = float(close.iloc[idx])
        settle = float(close.iloc[idx + 600])
        move_bps = math.log(settle / entry) * 10000.0
        won = move_bps > 0.0 if signal == "UP" else move_bps < 0.0
        rows.append(
            {
                "time": data.index[idx],
                "time_shanghai": data.index[idx].tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
                "idx": int(idx),
                "policy": policy,
                "state": state,
                "score": score,
                "edge": edge,
                "branch": branch,
                "signal": signal,
                "entry": round(entry, 4),
                "settle": round(settle, 4),
                "move_bps": round(move_bps, 4),
                "won": bool(won),
                "pnl": 4 if won else -5,
                "ret5_bps": round(values[0], 4),
                "ret15_bps": round(values[1], 4),
                "ret30_bps": round(values[2], 4),
                "ret60_bps": round(values[3], 4),
                "slope_bps": round(values[4], 4),
                "range15_bps": round(values[5], 4),
                "range30_bps": round(float(range30.iloc[idx]), 4),
                "z": round(float(z.iloc[idx]), 6),
                "sigma_bps": round(float(features["sigma_bps"].iloc[idx]), 6),
                "sigma_expand": round(float(features["sigma_expand"].iloc[idx]), 6),
                "ob_votes": ob_votes[signal],
                "imbalance_20": round(imbalance, 6),
                "micro_bps": round(micro, 6),
                "flow_60": round(flow, 6),
                "support_chg60": round(support_chg, 6),
            }
        )
    return pd.DataFrame(rows)


def apply_cooldown(rows: pd.DataFrame) -> pd.DataFrame:
    if rows.empty:
        return rows
    accepted = []
    last_time: pd.Timestamp | None = None
    for row in rows.sort_values("time").to_dict("records"):
        timestamp = pd.Timestamp(row["time"])
        if last_time is not None and (timestamp - last_time).total_seconds() < 600:
            continue
        accepted.append(row)
        last_time = timestamp
    return pd.DataFrame(accepted)


def run() -> dict[str, Any]:
    policies = (
        "pullback_only",
        "hybrid_ob1",
        "hybrid_ob2",
        "trend_all",
        "robust_lower_ob0",
        "robust_lower_ob1",
        "robust_lower_ob2",
        "stable_down_break",
    )
    reports = []
    all_trades = []
    for source_name, seconds, orderbook in SOURCES:
        data = load_local_data(seconds, orderbook)
        hours = (data.index.max() - data.index.min()).total_seconds() / 3600.0
        source_report = {
            "source": source_name,
            "start": data.index.min(),
            "end": data.index.max(),
            "hours": round(hours, 4),
            "policies": {},
        }
        for policy in policies:
            trades = apply_cooldown(build_candidates(data, policy))
            trades["source"] = source_name
            source_report["policies"][policy] = detailed.metrics(trades, hours)
            if not trades.empty:
                source_report["policies"][policy]["byBranch"] = {
                    str(branch): detailed.metrics(group, hours)
                    for branch, group in trades.groupby("branch")
                }
                source_report["policies"][policy]["byState"] = {
                    str(state): detailed.metrics(group, hours)
                    for state, group in trades.groupby("state")
                }
            all_trades.append(trades)
        reports.append(source_report)

    trades_out = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    trades_out.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "method": "Fixed symmetric regime voting. Flat states fade the 600s normal edge; aligned 5/15/30/60m trends trade with direction. No parameters are selected from a single winning day.",
        "reports": reports,
        "tradesCsv": str(OUT_CSV),
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    result = run()
    print(json.dumps(clean(result), ensure_ascii=False, indent=2))
