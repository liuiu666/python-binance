from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd


def payout_for_horizon(horizon_sec: int) -> float:
    minutes = horizon_sec / 60.0
    if minutes >= 30:
        return 0.85
    if minutes >= 10:
        return 0.80
    return 0.80


def max_loss_streak(wins: Iterable[bool]) -> int:
    current = 0
    best = 0
    for won in wins:
        if won:
            current = 0
        else:
            current += 1
            best = max(best, current)
    return int(best)


def summarize_trades(
    trades: list[dict],
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    amount: float = 5.0,
    payout_rate: float = 0.80,
) -> dict:
    rows = sorted(trades, key=lambda row: row["time"])
    n = len(rows)
    wins = sum(1 for row in rows if row.get("won"))
    losses = n - wins
    if start is None and rows:
        start = rows[0]["time"]
    if end is None and rows:
        end = rows[-1]["time"]
    sample_hours = 1.0
    if start is not None and end is not None:
        sample_hours = max((end - start).total_seconds() / 3600.0, 1e-9)
    gross_win = amount * payout_rate
    pnl = wins * gross_win - losses * amount
    return {
        "trades": int(n),
        "wins": int(wins),
        "losses": int(losses),
        "winRate": round(100.0 * wins / n, 2) if n else None,
        "tradesPerDay": round(n / max(sample_hours / 24.0, 1e-9), 2),
        "maxLoss": max_loss_streak(row.get("won") for row in rows),
        "amount": float(amount),
        "payoutRate": float(payout_rate),
        "pnl": round(float(pnl), 2),
        "first": rows[0]["time"].isoformat() if rows else None,
        "last": rows[-1]["time"].isoformat() if rows else None,
    }


def split_metrics(
    trades: list[dict],
    start: pd.Timestamp,
    end: pd.Timestamp,
    amount: float = 5.0,
    payout_rate: float = 0.80,
) -> dict:
    total_hours = max((end - start).total_seconds() / 3600.0, 1e-9)
    last24_hours = min(24.0, total_hours)
    cutoff = end - pd.Timedelta(hours=last24_hours)
    before = [row for row in trades if row["time"] < cutoff]
    last = [row for row in trades if row["time"] >= cutoff]

    thirds = []
    for i in range(3):
        a = start + (end - start) * i / 3
        b = start + (end - start) * (i + 1) / 3
        part = [row for row in trades if a <= row["time"] < b]
        thirds.append(summarize_trades(part, a, b, amount, payout_rate))

    by_day = []
    for day, part in _group_by_utc_day(trades).items():
        if not part:
            continue
        day_start = pd.Timestamp(day, tz="UTC")
        day_end = day_start + pd.Timedelta(days=1)
        by_day.append(
            {
                "day": day,
                **summarize_trades(part, day_start, day_end, amount, payout_rate),
            }
        )

    return {
        "all": summarize_trades(trades, start, end, amount, payout_rate),
        "beforeLast24h": summarize_trades(before, start, cutoff, amount, payout_rate),
        "last24h": summarize_trades(last, cutoff, end, amount, payout_rate),
        "thirds": thirds,
        "byUtcDay": by_day,
    }


def robust_score(metrics: dict) -> float:
    all_m = metrics["all"]
    before = metrics["beforeLast24h"]
    last = metrics["last24h"]
    thirds = [
        part["winRate"]
        for part in metrics["thirds"]
        if part["trades"] >= 3 and part["winRate"] is not None
    ]
    if all_m["trades"] < 8 or before["trades"] < 5 or last["trades"] < 3 or len(thirds) < 2:
        return -999.0
    std_wr = float(np.std(thirds))
    min_wr = min(thirds)
    count_bonus = min(all_m["tradesPerDay"], 18) * 0.25
    count_bonus += min(last["tradesPerDay"], 18) * 0.25
    loss_penalty = max(0, max(all_m["maxLoss"], last["maxLoss"]) - 2) * 5.0
    return round(
        all_m["winRate"] * 0.25
        + before["winRate"] * 0.20
        + last["winRate"] * 0.25
        + min_wr * 0.20
        - std_wr * 0.20
        + count_bonus
        - loss_penalty,
        4,
    )


def compact_metrics(metrics: dict) -> dict:
    return {
        "all": metrics["all"],
        "beforeLast24h": metrics["beforeLast24h"],
        "last24h": metrics["last24h"],
        "thirds": [
            {
                "trades": item["trades"],
                "winRate": item["winRate"],
                "tradesPerDay": item["tradesPerDay"],
                "maxLoss": item["maxLoss"],
                "pnl": item["pnl"],
            }
            for item in metrics["thirds"]
        ],
        "byUtcDay": [
            {
                "day": item["day"],
                "trades": item["trades"],
                "winRate": item["winRate"],
                "maxLoss": item["maxLoss"],
                "pnl": item["pnl"],
            }
            for item in metrics.get("byUtcDay", [])
        ],
    }


def _group_by_utc_day(trades: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in trades:
        day = row["time"].strftime("%Y-%m-%d")
        out.setdefault(day, []).append(row)
    return out
