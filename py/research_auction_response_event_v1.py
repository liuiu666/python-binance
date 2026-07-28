"""Causal auction-response events: absorption versus liquidity vacuum.

Thresholds are rolling quantiles of the previous hour and are shifted by one
second.  No outcome-driven threshold search or branch selection is used.
"""

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

from research_auction_confirmation_router_v1 import load_forward_live  # noqa: E402
from research_multiscale_phase_gate import load_live_parity_sources  # noqa: E402
from research_normal_shape_1m_10m import clean  # noqa: E402


LOOKBACK_SEC = 3600
MIN_HISTORY_SEC = 1800
FLOW_WINDOW_SEC = 10
HORIZON_SEC = 600
MIN_GAP_SEC = 600
DELAYS = (0, 5, 6, 10)
AMOUNT_U = 5.0
PAYOUT_RATE = 0.8
OUT_JSON = ROOT / "tmp" / "auction_response_event_v1_latest.json"
OUT_CSV = ROOT / "tmp" / "auction_response_event_v1_trades.csv"


def build_features(data: pd.DataFrame) -> pd.DataFrame:
    frame = pd.DataFrame(index=data.index)
    close = data["close"].astype(float)
    buy10 = data["buy_qty"].fillna(0.0).rolling(FLOW_WINDOW_SEC, min_periods=FLOW_WINDOW_SEC).sum()
    sell10 = data["sell_qty"].fillna(0.0).rolling(FLOW_WINDOW_SEC, min_periods=FLOW_WINDOW_SEC).sum()
    total10 = buy10 + sell10
    frame["flow10"] = (buy10 - sell10) / total10.replace(0.0, np.nan)
    frame["volume10"] = data["volume"].fillna(0.0).rolling(FLOW_WINDOW_SEC, min_periods=FLOW_WINDOW_SEC).sum()
    frame["ret10_bps"] = close.pct_change(FLOW_WINDOW_SEC, fill_method=None) * 10000.0
    frame["flow_q90"] = frame.flow10.abs().shift(1).rolling(LOOKBACK_SEC, min_periods=MIN_HISTORY_SEC).quantile(0.90)
    frame["volume_q75"] = frame.volume10.shift(1).rolling(LOOKBACK_SEC, min_periods=MIN_HISTORY_SEC).quantile(0.75)
    frame["move_q50"] = frame.ret10_bps.abs().shift(1).rolling(LOOKBACK_SEC, min_periods=MIN_HISTORY_SEC).quantile(0.50)
    frame["move_q75"] = frame.ret10_bps.abs().shift(1).rolling(LOOKBACK_SEC, min_periods=MIN_HISTORY_SEC).quantile(0.75)
    frame["imbalance20"] = data["imbalance_20"].astype(float)
    frame["micro_bps"] = data["microprice_edge_bps"].astype(float)
    frame["bid_change10"] = data["bid_qty_20"].astype(float).pct_change(FLOW_WINDOW_SEC, fill_method=None)
    frame["ask_change10"] = data["ask_qty_20"].astype(float).pct_change(FLOW_WINDOW_SEC, fill_method=None)
    frame["ob_available"] = data["ob_available"].fillna(False).astype(bool)
    return frame.replace([np.inf, -np.inf], np.nan)


def candidates(features: pd.DataFrame) -> pd.DataFrame:
    frame = features.copy()
    frame["crowd"] = np.sign(frame.flow10)
    frame["aligned_move"] = frame.crowd * frame.ret10_bps
    frame["aligned_book"] = frame.crowd * frame.imbalance20
    frame["aligned_micro"] = frame.crowd * frame.micro_bps
    frame["opposing_depth_change"] = np.where(frame.crowd > 0.0, frame.ask_change10, frame.bid_change10)
    impulse = (
        frame.ob_available
        & (frame.flow10.abs() >= frame.flow_q90)
        & (frame.volume10 >= frame.volume_q75)
        & frame.crowd.ne(0.0)
    )
    absorbed = (
        impulse
        & (frame.aligned_move <= 0.50 * frame.move_q50)
        & (frame.aligned_book <= 0.0)
        & (frame.aligned_micro <= 0.0)
    )
    vacuum = (
        impulse
        & (frame.aligned_move >= frame.move_q75)
        & (frame.aligned_book > 0.0)
        & (frame.aligned_micro > 0.0)
        & (frame.opposing_depth_change <= 0.0)
    )
    frame["action"] = np.where(absorbed, "absorption_fade", np.where(vacuum, "vacuum_follow", None))
    frame["signal_direction"] = np.where(absorbed, -frame.crowd, np.where(vacuum, frame.crowd, 0.0))
    return frame[frame.signal_direction.ne(0.0)]


def replay(source: Any) -> pd.DataFrame:
    data = source.data
    feature = build_features(data)
    event = candidates(feature)
    close = data["close"].astype(float)
    rows: list[dict[str, Any]] = []
    last_entry: pd.Timestamp | None = None
    for timestamp, row in event.iterrows():
        timestamp = pd.Timestamp(timestamp)
        if timestamp < source.test_start or timestamp >= source.test_end:
            continue
        entry_time = timestamp + pd.Timedelta(seconds=6)
        if last_entry is not None and (entry_time - last_entry).total_seconds() < MIN_GAP_SEC:
            continue
        record: dict[str, Any] = {
            "source": source.spec.name,
            "role": source.spec.role,
            "time": timestamp,
            "action": row.action,
            "signal": "UP" if row.signal_direction > 0.0 else "DOWN",
            "crowd": "UP" if row.crowd > 0.0 else "DOWN",
            "flow10": row.flow10,
            "flow_q90": row.flow_q90,
            "volume10": row.volume10,
            "volume_q75": row.volume_q75,
            "ret10_bps": row.ret10_bps,
            "move_q50": row.move_q50,
            "move_q75": row.move_q75,
            "aligned_book": row.aligned_book,
            "aligned_micro": row.aligned_micro,
            "opposing_depth_change": row.opposing_depth_change,
        }
        valid = True
        for delay in DELAYS:
            target = timestamp + pd.Timedelta(seconds=delay)
            settle_target = target + pd.Timedelta(seconds=HORIZON_SEC)
            entry_pos = int(close.index.searchsorted(target))
            settle_pos = int(close.index.searchsorted(settle_target))
            if entry_pos >= len(close) or settle_pos >= len(close):
                valid = False
                break
            if (close.index[entry_pos] - target).total_seconds() > 1 or (close.index[settle_pos] - settle_target).total_seconds() > 1:
                valid = False
                break
            entry = float(close.iloc[entry_pos])
            settle = float(close.iloc[settle_pos])
            record[f"entry_d{delay}"] = entry
            record[f"settle_d{delay}"] = settle
            record[f"raw_move_bps_d{delay}"] = (settle / entry - 1.0) * 10000.0
        if valid:
            rows.append(record)
            last_entry = entry_time
    return pd.DataFrame(rows)


def metrics(frame: pd.DataFrame, delay: int) -> dict[str, Any]:
    if frame.empty:
        return {"trades": 0, "wins": 0, "winRate": 0.0, "pnlU": 0.0, "maxDrawdownU": 0.0, "maxLossStreak": 0}
    direction = np.where(frame.signal == "UP", 1.0, -1.0)
    signed = frame[f"raw_move_bps_d{delay}"].to_numpy(float) * direction
    won = signed > 0.0
    pnl = np.where(won, AMOUNT_U * PAYOUT_RATE, -AMOUNT_U)
    equity = np.cumsum(pnl)
    peak = np.maximum.accumulate(np.r_[0.0, equity])[:-1]
    streak = maximum = 0
    for item in won:
        streak = 0 if item else streak + 1
        maximum = max(maximum, streak)
    return {
        "trades": int(len(frame)),
        "wins": int(won.sum()),
        "winRate": round(float(won.mean()) * 100.0, 2),
        "pnlU": round(float(pnl.sum()), 2),
        "maxDrawdownU": round(float((peak - equity).max()), 2),
        "maxLossStreak": maximum,
        "medianSignedBps": round(float(np.median(signed)), 4),
        "thinMarginPctLe3bp": round(float(np.mean(np.abs(signed) <= 3.0)) * 100.0, 2),
    }


def main() -> None:
    sources = [*load_live_parity_sources(), load_forward_live()]
    frames = [replay(source) for source in sources]
    trades = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    trades = trades.sort_values("time")
    report = {
        "method": {
            "parameterSearch": False,
            "lookbackSec": LOOKBACK_SEC,
            "flowImpulse": "Absolute 10-second flow >= prior-hour 90th percentile and volume >= 75th percentile.",
            "absorption": "Crowd flow is not moving price and both book imbalance and microprice oppose it; fade.",
            "vacuum": "Price response exceeds the 75th percentile, book/microprice align and opposing depth falls; follow.",
            "delaysSec": DELAYS,
            "validationWarning": "All available periods are already inspected; future frozen evidence remains required.",
        },
        "overall": {f"delay{delay}s": metrics(trades, delay) for delay in DELAYS},
        "rolesDelay6s": {role: metrics(group, 6) for role, group in trades.groupby("role")},
        "actionsDelay6s": {action: metrics(group, 6) for action, group in trades.groupby("action")},
        "sourcesDelay6s": {source: metrics(group, 6) for source, group in trades.groupby("source")},
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(json.dumps(clean(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
