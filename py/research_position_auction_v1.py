"""Fixed causal test of new-position trend continuation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
EVENTS = ROOT / "tmp" / "unified_auction_events_10m.csv"
CONTEXT = ROOT / "tmp" / "position_context_20260713"
OUT_JSON = ROOT / "tmp" / "position_auction_v1_latest.json"
OUT_CSV = ROOT / "tmp" / "position_auction_v1_trades.csv"
DELAYS = (0, 5, 6, 10)
AMOUNT_U = 5.0
PAYOUT_RATE = 0.8


def read_period(name: str, columns: list[str], prefix: str, context_dir: Path = CONTEXT) -> pd.DataFrame:
    frame = pd.read_csv(context_dir / name)
    frame["timestamp"] = pd.to_datetime(frame.timestamp, utc=True, format="mixed", errors="coerce")
    frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp")
    for column in columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame[f"{column}_change_15m"] = frame[column].pct_change(3, fill_method=None)
        frame[f"{column}_change_30m"] = frame[column].pct_change(6, fill_method=None)
    frame["available_time"] = frame.timestamp + pd.Timedelta(minutes=5)
    selected = ["available_time", *columns]
    selected += [f"{column}_change_{minutes}m" for column in columns for minutes in (15, 30)]
    return frame[selected].rename(columns={column: f"{prefix}_{column}" for column in selected if column != "available_time"})


def merge_context(events: pd.DataFrame, context_dir: Path = CONTEXT) -> pd.DataFrame:
    sources = [
        read_period("btcusdt_open_interest.csv", ["sumOpenInterest", "sumOpenInterestValue"], "oi", context_dir),
        read_period("btcusdt_global_lsratio.csv", ["longShortRatio"], "global", context_dir),
        read_period("btcusdt_top_account_lsratio.csv", ["longShortRatio"], "top_account", context_dir),
        read_period("btcusdt_lsratio.csv", ["longShortRatio"], "top_position", context_dir),
        read_period("btcusdt_taker.csv", ["buySellRatio"], "taker", context_dir),
    ]
    out = events.sort_values("time")
    for index, source in enumerate(sources):
        time_name = f"available_time_{index}"
        source = source.rename(columns={"available_time": time_name})
        out = pd.merge_asof(
            out.sort_values("time"), source.sort_values(time_name),
            left_on="time", right_on=time_name, direction="backward",
            tolerance=pd.Timedelta(minutes=10),
        ).drop(columns=[time_name])
    return out


def decide(row: pd.Series) -> tuple[str | None, str, int]:
    direction = int(np.sign(row.ret_300))
    if direction == 0 or direction * row.ret_60 <= 0.0:
        return None, "waiting_multiscale_direction", 0
    oi_change = row.get("oi_sumOpenInterest_change_15m")
    if not np.isfinite(oi_change) or oi_change <= 0.0:
        return None, "position_closing_or_missing", 0
    confirmations = [
        direction * row.get("top_position_longShortRatio_change_15m", np.nan) > 0.0,
        direction * row.get("top_account_longShortRatio_change_15m", np.nan) > 0.0,
        direction * (row.get("taker_buySellRatio", np.nan) - 1.0) > 0.0,
        direction * row.get("imbalance_60", np.nan) > 0.0,
        direction * row.get("micro_60", np.nan) > 0.0,
    ]
    votes = int(sum(bool(item) for item in confirmations))
    if votes < 3 or row.volume_ratio_60 < 0.8:
        return None, "new_position_waiting_confirmation", votes
    return ("UP" if direction > 0 else "DOWN"), "new_position_trend_follow", votes


def metrics(frame: pd.DataFrame, delay: int) -> dict[str, Any]:
    if frame.empty:
        return {"trades": 0, "wins": 0, "winRate": 0.0, "pnlU": 0.0, "maxDrawdownU": 0.0, "maxLossStreak": 0}
    direction = np.where(frame.signal == "UP", 1.0, -1.0)
    signed = frame[f"raw_move_bps_d{delay}"].to_numpy(float) * direction
    won = signed > 0.0
    pnl = np.where(won, AMOUNT_U * PAYOUT_RATE, -AMOUNT_U)
    equity = np.cumsum(pnl)
    peak = np.maximum.accumulate(np.r_[0.0, equity])[1:]
    streak = maximum = 0
    for item in won:
        streak = 0 if item else streak + 1
        maximum = max(maximum, streak)
    return {
        "trades": int(len(frame)), "wins": int(won.sum()),
        "winRate": round(float(won.mean()) * 100.0, 2),
        "pnlU": round(float(pnl.sum()), 2),
        "maxDrawdownU": round(float((peak - equity).max()), 2),
        "maxLossStreak": maximum,
        "medianSignedBps": round(float(np.median(signed)), 4),
        "thinMarginPctLe3bp": round(float(np.mean(np.abs(signed) <= 3.0)) * 100.0, 2),
    }


def main() -> None:
    events = pd.read_csv(EVENTS, parse_dates=["time", "entry_time", "settle_time"])
    data = merge_context(events)
    decisions = data.apply(decide, axis=1, result_type="expand")
    decisions.columns = ["signal", "reason", "confirmation_votes"]
    data = pd.concat([data, decisions], axis=1)
    trades = data[data.signal.isin(["UP", "DOWN"])].copy()
    report = {
        "method": {
            "parameterSearch": False,
            "availabilityLagMinutes": 5,
            "rule": "Follow aligned 60/300-second price only while open interest grows and at least three of top-position, top-account, taker flow, book and microprice agree.",
            "closingPositions": "Skip; no automatic fade.",
            "delaysSec": DELAYS,
            "validationWarning": "Position fields are newly introduced but the price outcomes were previously inspected; new future evidence is still required.",
        },
        "coverage": {
            "eventsWithOpenInterest": int(data.oi_sumOpenInterest.notna().sum()),
            "start": data.loc[data.oi_sumOpenInterest.notna(), "time"].min().isoformat(),
            "end": data.loc[data.oi_sumOpenInterest.notna(), "time"].max().isoformat(),
            "trades": len(trades),
        },
        "overall": {f"delay{delay}s": metrics(trades, delay) for delay in DELAYS},
        "rolesDelay6s": {role: metrics(group, 6) for role, group in trades.groupby("role")},
        "directionsDelay6s": {signal: metrics(group, 6) for signal, group in trades.groupby("signal")},
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
