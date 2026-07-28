"""Replay today's production signal snapshots locally at the correct entry time."""

from __future__ import annotations

import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_DIR = ROOT / "tmp" / "stable_winrate_local" / "today_20260728"
SIGNAL_FILE = SNAPSHOT_DIR / "signal_audit.jsonl"
PRICE_FILES = [SNAPSHOT_DIR / "2026-07-27.csv", SNAPSHOT_DIR / "2026-07-28.csv"]
OUT_FILE = SNAPSHOT_DIR / "today_replay_report.json"
STRATEGY_ID = "BTC_30min_SHADOW_CANDIDATE"
SHANGHAI = ZoneInfo("Asia/Shanghai")
TODAY = pd.Timestamp("2026-07-28", tz=SHANGHAI)


def load_signals() -> pd.DataFrame:
    rows = []
    with SIGNAL_FILE.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event") != "signal_snapshot" or row.get("strategy_id") != STRATEGY_ID:
                continue
            actionable = pd.to_datetime(row.get("actionable_time"), utc=True, errors="coerce")
            if pd.isna(actionable):
                continue
            local_time = actionable.tz_convert(SHANGHAI)
            if not (TODAY <= local_time < TODAY + pd.Timedelta(days=1)):
                continue
            server_time = pd.to_datetime(row.get("serverTime"), unit="ms", utc=True, errors="coerce")
            if pd.isna(server_time) or server_time > actionable:
                continue
            rows.append(
                {
                    "actionable": actionable,
                    "serverTime": server_time,
                    "signal": row.get("signal"),
                    "avgProb": float(row.get("avg_prob") or 0.5),
                    "rsi": row.get("rsi_value"),
                    "confidence": row.get("confidence"),
                    "dataBlocked": bool(row.get("data_health_blocked", False)),
                }
            )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    # The order available at execution is the final snapshot observed no later
    # than the actionable boundary.
    return (
        frame.sort_values(["actionable", "serverTime"])
        .groupby("actionable", as_index=False)
        .tail(1)
        .sort_values("actionable")
        .reset_index(drop=True)
    )


def load_prices() -> pd.DataFrame:
    frames = []
    for path in PRICE_FILES:
        frame = pd.read_csv(path, usecols=["timestamp", "close"])
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        frames.append(frame.dropna())
    return (
        pd.concat(frames, ignore_index=True)
        .sort_values("timestamp")
        .drop_duplicates("timestamp", keep="last")
        .reset_index(drop=True)
    )


def price_at(prices: pd.DataFrame, target: pd.Timestamp) -> tuple[pd.Timestamp, float] | None:
    position = int(prices["timestamp"].searchsorted(target, side="left"))
    if position >= len(prices):
        return None
    row = prices.iloc[position]
    if row["timestamp"] - target > pd.Timedelta(seconds=10):
        return None
    return row["timestamp"], float(row["close"])


def replay(signals: pd.DataFrame, prices: pd.DataFrame, up_threshold: float) -> dict:
    trades = []
    next_allowed: pd.Timestamp | None = None
    last_price_time = prices["timestamp"].max()
    for row in signals.to_dict("records"):
        actionable = row["actionable"]
        if actionable + pd.Timedelta(minutes=30) > last_price_time:
            continue
        if next_allowed is not None and actionable < next_allowed:
            continue
        direction = row["signal"]
        if direction not in {"UP", "DOWN"} or row["dataBlocked"]:
            continue
        if direction == "UP" and row["avgProb"] < up_threshold:
            continue
        if direction == "DOWN" and row["avgProb"] > 0.45:
            continue
        opened = price_at(prices, actionable)
        closed = price_at(prices, actionable + pd.Timedelta(minutes=30))
        if opened is None or closed is None:
            continue
        won = closed[1] > opened[1] if direction == "UP" else closed[1] < opened[1]
        tie = closed[1] == opened[1]
        trades.append(
            {
                "actionableShanghai": actionable.tz_convert(SHANGHAI).strftime("%H:%M:%S"),
                "direction": direction,
                "avgProb": round(row["avgProb"], 4),
                "rsi": row["rsi"],
                "openTime": str(opened[0]),
                "openPrice": opened[1],
                "closeTime": str(closed[0]),
                "closePrice": closed[1],
                "status": "tie" if tie else "won" if won else "lost",
                "pnl5U": 0.0 if tie else 4.25 if won else -5.0,
            }
        )
        next_allowed = actionable + pd.Timedelta(minutes=30)
    settled = [trade for trade in trades if trade["status"] != "tie"]
    wins = sum(trade["status"] == "won" for trade in settled)
    losses = len(settled) - wins
    return {
        "upThreshold": up_threshold,
        "trades": len(trades),
        "settled": len(settled),
        "wins": wins,
        "losses": losses,
        "winRate": round(wins / len(settled) * 100.0, 2) if settled else None,
        "pnl5U": round(sum(trade["pnl5U"] for trade in trades), 2),
        "details": trades,
    }


def main() -> None:
    signals = load_signals()
    prices = load_prices()
    report = {
        "method": {
            "execution": "local_only",
            "day": str(TODAY.date()),
            "strategyId": STRATEGY_ID,
            "signalRowsAfterDedup": len(signals),
            "priceStart": str(prices["timestamp"].min()),
            "priceEnd": str(prices["timestamp"].max()),
            "entry": "first local 1-second price at or after actionable_time",
            "durationMinutes": 30,
            "cooldownMinutes": 30,
            "downThreshold": 0.55,
        },
        "currentUp055": replay(signals, prices, 0.55),
        "recommendedUp065": replay(signals, prices, 0.65),
        "conservativeUp070": replay(signals, prices, 0.70),
    }
    OUT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Saved {OUT_FILE}")


if __name__ == "__main__":
    main()
