"""Causal regime audit for recorded live and shadow ten-minute orders.

This script is diagnostic only.  It does not search thresholds or modify the
live strategy.  Every feature is calculated from data available at or before
the recorded actionable time, while outcomes use the recorded execution and
settlement prices.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from backtest_io import read_orderbook  # noqa: E402
from second_backtest.data import load_second_bars  # noqa: E402


API = "http://115.190.218.128:3000/api/trade-history"
DAYS = pd.date_range("2026-07-09", "2026-07-15", freq="D").strftime("%Y-%m-%d").tolist()
OUT_CSV = ROOT / "tmp" / "live_order_regime_audit.csv"
OUT_JSON = ROOT / "tmp" / "live_order_regime_audit.json"

SOURCES = (
    (
        ROOT / "tmp" / "latest_pull_20260710_203217" / "data" / "btcusdt_1s_trades.csv",
        ROOT / "tmp" / "latest_pull_20260710_203217" / "data" / "btcusdt_orderbook_1s.csv",
    ),
    (
        ROOT / "data" / "server_latest" / "btcusdt_1s_trades.csv",
        ROOT / "data" / "server_latest" / "btcusdt_orderbook_1s.csv",
    ),
    (
        ROOT / "tmp" / "latest_pull_20260712_migration_fix" / "extracted" / "data" / "btcusdt_1s_trades.csv",
        ROOT / "tmp" / "latest_pull_20260712_migration_fix" / "extracted" / "data" / "btcusdt_orderbook_1s.csv",
    ),
    (
        ROOT / "tmp" / "phase_live_audit_20260713" / "btcusdt_1s_trades.csv",
        ROOT / "tmp" / "phase_live_audit_20260713" / "btcusdt_orderbook_1s.csv",
    ),
    (
        ROOT / "tmp" / "daily_archive_20260713" / "btcusdt_1s_trades.csv",
        ROOT / "tmp" / "daily_archive_20260713" / "btcusdt_orderbook_1s.csv",
    ),
    (
        ROOT / "tmp" / "frozen_position_forward" / "btcusdt_1s_trades.csv",
        ROOT / "tmp" / "frozen_position_forward" / "btcusdt_orderbook_1s.csv",
    ),
)

WINDOWS = (60, 300, 600, 1800)
RETURN_WINDOWS = (10, 30, 60, 120, 300, 600, 1800)
CONFIRM_DELAYS = (10, 30, 60)


def sign(value: float) -> int:
    return 1 if value > 0.0 else -1 if value < 0.0 else 0


def fetch_orders() -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for day in DAYS:
        query = urlencode({"day": day, "kind": "all", "pageSize": 300})
        with urlopen(f"{API}?{query}", timeout=20) as response:  # noqa: S310 - fixed research endpoint
            payload = json.load(response)
        for order in [*payload.get("recent", []), *payload.get("active", [])]:
            order_id = str(order.get("id") or "")
            if not order_id or order_id in seen:
                continue
            seen.add(order_id)
            rows.append(order)
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame["signal_time"] = pd.to_datetime(
        frame.get("actionableTime", frame.get("signalTime")), utc=True, errors="coerce"
    )
    frame["open_time"] = pd.to_datetime(frame["openTime"], unit="ms", utc=True, errors="coerce")
    frame["settle_time"] = pd.to_datetime(frame["settleTime"], unit="ms", utc=True, errors="coerce")
    frame["local_day"] = frame["open_time"].dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
    return frame.sort_values("open_time").reset_index(drop=True)


def shape(close: pd.Series) -> dict[str, float]:
    values = close.astype(float).to_numpy()
    center = float(np.mean(values))
    sigma = float(np.std(values, ddof=0))
    sigma_bps = sigma / center * 10000.0 if center > 0.0 else 0.0
    if sigma <= 0.0 or center <= 0.0:
        return {"z": 0.0, "inside1": 1.0, "slope_sigma": 0.0, "sigma_expand": 1.0, "sigma_bps": 0.0}
    normalized = (values - center) / sigma
    quarter = max(10, len(values) // 4)
    first_center = float(np.mean(values[:quarter]))
    last_center = float(np.mean(values[-quarter:]))
    slope_bps = (last_center / first_center - 1.0) * 10000.0 if first_center > 0.0 else 0.0
    half = len(values) // 2
    first_sigma = float(np.std(values[:half], ddof=0))
    last_sigma = float(np.std(values[half:], ddof=0))
    return {
        "z": float(normalized[-1]),
        "inside1": float(np.mean(np.abs(normalized) <= 1.0)),
        "slope_sigma": slope_bps / sigma_bps if sigma_bps > 0.0 else 0.0,
        "sigma_expand": last_sigma / first_sigma if first_sigma > 0.0 else 1.0,
        "sigma_bps": sigma_bps,
    }


def flow_ratio(frame: pd.DataFrame) -> float:
    buy = float(frame["buy_qty"].fillna(0.0).sum())
    sell = float(frame["sell_qty"].fillna(0.0).sum())
    return (buy - sell) / (buy + sell) if buy + sell > 0.0 else 0.0


def finite_mean(frame: pd.DataFrame, column: str) -> float:
    if column not in frame:
        return float("nan")
    values = pd.to_numeric(frame[column], errors="coerce").replace([np.inf, -np.inf], np.nan)
    return float(values.mean())


def classify_regime(features: dict[str, float]) -> str:
    slope300 = features["slope_sigma_300"]
    slope600 = features["slope_sigma_600"]
    expansion = features["sigma_expand_600"]
    inside = features["inside1_600"]
    if expansion > 1.5 or np.sign(slope300) != np.sign(slope600):
        return "transition"
    if slope300 >= 0.75 and slope600 >= 0.75:
        return "up_migration"
    if slope300 <= -0.75 and slope600 <= -0.75:
        return "down_migration"
    if 0.55 <= inside <= 0.80 and abs(slope600) < 0.75 and 0.67 <= expansion <= 1.50:
        return "balanced_value"
    return "drift_or_unclear"


def feature_row(data: pd.DataFrame, signal_time: pd.Timestamp) -> dict[str, Any] | None:
    pos = int(data.index.searchsorted(signal_time, side="right")) - 1
    if pos < max(WINDOWS) - 1 or pos >= len(data):
        return None
    if abs((signal_time - data.index[pos]).total_seconds()) > 2.0:
        return None
    history = data.iloc[pos - 1799 : pos + 1]
    observed = history.get("observed", pd.Series(True, index=history.index)).fillna(False)
    available = history.get("ob_available", pd.Series(False, index=history.index)).fillna(False)
    row: dict[str, Any] = {
        "feature_time": data.index[pos],
        "observed_pct_600": float(observed.iloc[-600:].mean() * 100.0),
        "orderbook_pct_600": float(available.iloc[-600:].mean() * 100.0),
    }
    for width in RETURN_WINDOWS:
        window = history.iloc[-width:]
        close = window["close"].astype(float)
        row[f"ret_{width}"] = (float(close.iloc[-1]) / float(close.iloc[0]) - 1.0) * 10000.0
        if width not in WINDOWS:
            continue
        info = shape(close)
        for key, value in info.items():
            row[f"{key}_{width}"] = value
    minute = history.iloc[-60:]
    ten = history.iloc[-10:]
    row["flow_10"] = flow_ratio(ten)
    row["flow_60"] = flow_ratio(minute)
    row["imbalance_10"] = finite_mean(ten, "imbalance_20")
    row["imbalance_60"] = finite_mean(minute, "imbalance_20")
    row["micro_10"] = finite_mean(ten, "microprice_edge_bps")
    row["micro_60"] = finite_mean(minute, "microprice_edge_bps")
    row["spread_60"] = finite_mean(minute, "spread_bps")
    volume60 = float(minute["volume"].fillna(0.0).sum())
    volume600 = float(history.iloc[-600:]["volume"].fillna(0.0).sum()) / 10.0
    row["volume_ratio_60"] = volume60 / volume600 if volume600 > 0.0 else 0.0
    minute_close = minute["close"].astype(float)
    current_price = float(minute_close.iloc[-1])
    row["pullback_from_high_60_bps"] = (float(minute_close.max()) / current_price - 1.0) * 10000.0
    row["rebound_from_low_60_bps"] = (current_price / float(minute_close.min()) - 1.0) * 10000.0
    old_value = history.iloc[-1200:-600]["close"].astype(float)
    current_value = history.iloc[-600:]["close"].astype(float)
    old_center = float(old_value.mean())
    old_sigma = float(old_value.std(ddof=0))
    current_price = float(current_value.iloc[-1])
    if old_center > 0.0 and old_sigma > 0.0:
        old_z = (current_price - old_center) / old_sigma
        edge = sign(old_z)
        boundary = old_center + edge * old_sigma
        beyond = (current_value >= boundary) if edge > 0 else (current_value <= boundary)
        transitions = int((beyond.astype(int).diff().abs() > 0).sum())
        streak = 0
        for accepted in reversed(beyond.tolist()):
            if not accepted:
                break
            streak += 1
        row["old_value_z"] = old_z
        row["old_value_center_shift_sigma"] = (float(current_value.mean()) - old_center) / old_sigma
        row["old_value_acceptance_60"] = float(beyond.iloc[-60:].mean())
        row["old_value_acceptance_120"] = float(beyond.iloc[-120:].mean())
        row["old_value_acceptance_300"] = float(beyond.iloc[-300:].mean())
        row["old_value_outside_streak_sec"] = streak
        row["old_value_boundary_transitions_600"] = transitions
    else:
        row.update({
            "old_value_z": 0.0,
            "old_value_center_shift_sigma": 0.0,
            "old_value_acceptance_60": 0.0,
            "old_value_acceptance_120": 0.0,
            "old_value_acceptance_300": 0.0,
            "old_value_outside_streak_sec": 0,
            "old_value_boundary_transitions_600": 0,
        })
    row["regime"] = classify_regime(row)
    return row


def confirmation_row(data: pd.DataFrame, signal_time: pd.Timestamp, direction: str) -> dict[str, Any]:
    direction_sign = 1.0 if direction == "UP" else -1.0 if direction == "DOWN" else 0.0
    result: dict[str, Any] = {}
    if direction_sign == 0.0:
        return result
    signal_pos = int(data.index.searchsorted(signal_time, side="right")) - 1
    if signal_pos < 0 or signal_pos >= len(data):
        return result
    signal_price = float(data["close"].iloc[signal_pos])
    for delay in CONFIRM_DELAYS:
        entry_target = signal_time + pd.Timedelta(seconds=delay)
        settle_target = entry_target + pd.Timedelta(seconds=600)
        entry_pos = int(data.index.searchsorted(entry_target))
        settle_pos = int(data.index.searchsorted(settle_target))
        if entry_pos >= len(data) or settle_pos >= len(data):
            continue
        if (data.index[entry_pos] - entry_target).total_seconds() > 1.0:
            continue
        if (data.index[settle_pos] - settle_target).total_seconds() > 1.0:
            continue
        entry = float(data["close"].iloc[entry_pos])
        settle = float(data["close"].iloc[settle_pos])
        post_signal = data.iloc[signal_pos + 1 : entry_pos + 1]
        result[f"confirm_progress_bps_{delay}"] = (entry / signal_price - 1.0) * 10000.0 * direction_sign
        result[f"confirm_flow_{delay}"] = flow_ratio(post_signal) * direction_sign if not post_signal.empty else 0.0
        result[f"delayed_signed_bps_{delay}"] = (settle / entry - 1.0) * 10000.0 * direction_sign
        result[f"delayed_won_{delay}"] = bool(result[f"delayed_signed_bps_{delay}"] > 0.0)
    return result


def attach_features(orders: pd.DataFrame) -> pd.DataFrame:
    pending = set(orders.index)
    extracted: dict[int, dict[str, Any]] = {}
    for seconds, orderbook in SOURCES:
        if not seconds.exists() or not orderbook.exists():
            continue
        bars = load_second_bars(seconds, include_shards=False)
        book = read_orderbook(orderbook, bars.index, max_age_sec=3)
        data = bars.join(book, how="left").sort_index()
        start = data.index.min() + pd.Timedelta(seconds=max(WINDOWS))
        end = data.index.max()
        candidates = [idx for idx in pending if start <= orders.at[idx, "signal_time"] <= end]
        for idx in candidates:
            features = feature_row(data, orders.at[idx, "signal_time"])
            if features is not None and features["observed_pct_600"] >= 90.0:
                features.update(confirmation_row(data, orders.at[idx, "signal_time"], orders.at[idx, "direction"]))
                extracted[idx] = features
                pending.remove(idx)
    feature_frame = pd.DataFrame.from_dict(extracted, orient="index")
    result = orders.join(feature_frame, how="left")
    direction = result["direction"].map({"UP": 1.0, "DOWN": -1.0})
    result["signal_fades_ret300"] = direction * result["ret_300"] < 0.0
    result["signal_follows_ret300"] = direction * result["ret_300"] > 0.0
    result["signed_ret300"] = direction * result["ret_300"]
    result["signed_z600"] = direction * result["z_600"]
    for width in (10, 30, 60, 120, 300, 600, 1800):
        result[f"signed_ret{width}"] = direction * result[f"ret_{width}"]
    result["signal_rejection_60_bps"] = np.where(
        result["direction"].eq("DOWN"),
        result["pullback_from_high_60_bps"],
        result["rebound_from_low_60_bps"],
    )
    result["recorded_won"] = result["status"].eq("won")
    result["normalized_pnl_5u"] = np.where(result["recorded_won"], 4.0, -5.0)
    result["recorded_move_bps"] = (
        pd.to_numeric(result["closePrice"], errors="coerce")
        / pd.to_numeric(result["openPrice"], errors="coerce")
        - 1.0
    ) * 10000.0 * direction
    result["execution_delay_sec"] = pd.to_numeric(result.get("executionDelayMs"), errors="coerce") / 1000.0
    return result


def metrics(frame: pd.DataFrame) -> dict[str, Any]:
    settled = frame[frame["status"].isin(["won", "lost", "tie"])].copy()
    if settled.empty:
        return {"trades": 0, "winRate": None, "normalizedPnl5U": 0.0}
    wins = settled["recorded_won"].astype(bool)
    pnl = settled["normalized_pnl_5u"].astype(float)
    equity = pnl.cumsum().to_numpy()
    peaks = np.maximum.accumulate(np.r_[0.0, equity])[:-1]
    streak = maximum = 0
    for won in wins:
        streak = 0 if won else streak + 1
        maximum = max(maximum, streak)
    return {
        "trades": int(len(settled)),
        "wins": int(wins.sum()),
        "winRate": round(float(wins.mean()) * 100.0, 2),
        "normalizedPnl5U": round(float(pnl.sum()), 2),
        "maxDrawdown5U": round(float(np.max(np.maximum(0.0, peaks - equity))), 2),
        "maxLossStreak": int(maximum),
        "avgSignedMoveBps": round(float(settled["recorded_move_bps"].mean()), 3),
    }


def grouped(frame: pd.DataFrame, column: str) -> dict[str, Any]:
    return {str(key): metrics(group) for key, group in frame.groupby(column, dropna=False)}


def hypothetical_metrics(frame: pd.DataFrame, won_column: str) -> dict[str, Any]:
    eligible = frame[frame[won_column].notna()].copy()
    if eligible.empty:
        return {"trades": 0, "winRate": None, "normalizedPnl5U": 0.0}
    eligible["recorded_won"] = eligible[won_column].astype(bool)
    eligible["normalized_pnl_5u"] = np.where(eligible["recorded_won"], 4.0, -5.0)
    return metrics(eligible)


def overlap_summary(frame: pd.DataFrame) -> dict[str, Any]:
    settled = frame[frame["status"].isin(["won", "lost", "tie"])].sort_values("open_time").copy()
    cluster_ids: list[int] = []
    cluster_id = -1
    cluster_end: pd.Timestamp | None = None
    for order in settled.itertuples():
        if cluster_end is None or order.open_time > cluster_end:
            cluster_id += 1
            cluster_end = order.settle_time
        else:
            cluster_end = max(cluster_end, order.settle_time)
        cluster_ids.append(cluster_id)
    settled["overlap_cluster"] = cluster_ids
    clusters = settled.groupby("overlap_cluster").agg(
        start=("open_time", "min"),
        orders=("id", "size"),
        strategies=("strategyId", "nunique"),
        directions=("direction", "nunique"),
    )
    independent_decisions = settled.drop_duplicates(
        ["overlap_cluster", "strategyId", "direction"], keep="last"
    )
    return {
        "settledRecords": int(len(settled)),
        "nonOverlappingMarketWindows": int(len(clusters)),
        "windowsWithMultipleRecords": int((clusters["orders"] > 1).sum()),
        "maxRecordsInOneWindow": int(clusters["orders"].max()) if not clusters.empty else 0,
        "uniqueWindowStrategyDirections": int(len(independent_decisions)),
        "deduplicatedMetrics": metrics(independent_decisions),
        "deduplicatedByDay": grouped(independent_decisions, "local_day"),
    }


def main() -> None:
    orders = fetch_orders()
    audited = attach_features(orders)
    covered = audited[audited["regime"].notna()].copy()
    confirmation: dict[str, Any] = {}
    for delay in CONFIRM_DELAYS:
        won_column = f"delayed_won_{delay}"
        progress_column = f"confirm_progress_bps_{delay}"
        flow_column = f"confirm_flow_{delay}"
        available = covered[covered[won_column].notna()].copy()
        price_confirmed = available[available[progress_column] > 0.0]
        price_flow_confirmed = price_confirmed[price_confirmed[flow_column] > 0.0]
        confirmation[str(delay)] = {
            "enterAllAfterDelay": hypothetical_metrics(available, won_column),
            "enterOnlyAfterPriceMovesInSignalDirection": hypothetical_metrics(price_confirmed, won_column),
            "enterOnlyAfterPriceAndTakerFlowConfirm": hypothetical_metrics(price_flow_confirmed, won_column),
            "priceConfirmedByDay": {
                str(day): hypothetical_metrics(group, won_column)
                for day, group in price_confirmed.groupby("local_day")
            },
        }
    report = {
        "method": {
            "purpose": "Explain cross-day live instability without selecting thresholds from outcomes.",
            "causal": "All regime features end at or before actionableTime.",
            "outcome": "Recorded execution open and settlement prices; normalized comparison uses 5U stake, +4U win and -5U loss.",
            "fixedRegimes": {
                "balanced_value": "inside1 55-80%, abs 600s slope below 0.75 sigma, sigma expansion 0.67-1.50",
                "migration": "300s and 600s slopes agree beyond 0.75 sigma",
                "transition": "sigma expansion above 1.50 or 300s/600s slope signs disagree",
                "drift_or_unclear": "none of the fixed states above",
            },
            "warning": "This audit describes already observed orders. It is not an untouched strategy validation and must not be used alone to deploy a rule.",
        },
        "coverage": {
            "orders": int(len(audited)),
            "ordersWithCausalFeatures": int(len(covered)),
            "start": audited["open_time"].min(),
            "end": audited["open_time"].max(),
        },
        "overall": metrics(audited),
        "byDay": grouped(audited, "local_day"),
        "byStrategy": grouped(audited, "strategyId"),
        "byRegime": grouped(covered, "regime"),
        "byRegimeAndAction": {
            f"{regime}|{'fade' if fade else 'follow'}": metrics(group)
            for (regime, fade), group in covered.groupby(["regime", "signal_fades_ret300"], dropna=False)
        },
        "executionDelaySec": {
            "count": int(audited["execution_delay_sec"].notna().sum()),
            "p50": round(float(audited["execution_delay_sec"].median()), 3),
            "p90": round(float(audited["execution_delay_sec"].quantile(0.9)), 3),
        },
        "causalPostSignalConfirmation": confirmation,
        "overlapAudit": overlap_summary(audited),
    }
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    audited.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
