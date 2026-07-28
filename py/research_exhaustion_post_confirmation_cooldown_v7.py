"""V7: apply the 600-second cooldown after order-book confirmation."""

from __future__ import annotations

import gc
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research_exhaustion_orderbook_confirmation_v6 import SOURCES, extract_source
from research_long_minute_consensus_v1 import read_minutes
from research_path_efficiency_router_v1 import attach_delay_outcomes, clean, metrics


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "tmp" / "exhaustion_post_confirmation_cooldown_v7.json"
OUT_CSV = ROOT / "tmp" / "exhaustion_post_confirmation_cooldown_v7_trades.csv"
DELAYS = (0, 5, 6, 10)


def build_raw_candidates(minutes: pd.DataFrame) -> pd.DataFrame:
    close = minutes.close.astype(float)
    ret1 = (close / close.shift(1) - 1.0) * 10000.0
    frame = pd.DataFrame(index=minutes.index)
    for width in (1, 3, 5, 10, 30):
        frame[f"ret_{width}"] = (close / close.shift(width) - 1.0) * 10000.0
    frame["path_10"] = ret1.abs().rolling(10, min_periods=10).sum()
    frame["efficiency_10"] = frame.ret_10.abs() / frame.path_10.replace(0.0, np.nan)
    frame["noise_30"] = ret1.rolling(30, min_periods=30).std(ddof=0)
    frame["trend_strength"] = frame.ret_10.abs() / (frame.noise_30 * math.sqrt(10.0)).replace(0.0, np.nan)
    center30 = close.rolling(30, min_periods=30).mean()
    sigma30 = close.rolling(30, min_periods=30).std(ddof=0)
    frame["z_30"] = (close - center30) / sigma30.replace(0.0, np.nan)
    frame["volume_ratio"] = (
        minutes.volume.rolling(5, min_periods=5).mean()
        / minutes.volume.rolling(30, min_periods=30).mean().replace(0.0, np.nan)
    )
    frame["observed"] = close.notna().astype(float).rolling(120, min_periods=120).mean()
    frame["entry_time"] = frame.index + pd.Timedelta(minutes=1)
    frame["settle_time"] = frame.index + pd.Timedelta(minutes=11)
    frame["entry"] = minutes.open.shift(-1)
    frame["settle"] = close.shift(-10)
    frame["move_bps"] = (frame.settle / frame.entry - 1.0) * 10000.0

    setup = (
        frame.efficiency_10.ge(0.60)
        & frame.trend_strength.ge(1.25)
        & (frame.ret_3 * frame.ret_10).gt(0.0)
        & (frame.ret_1 * frame.ret_10).lt(0.0)
        & frame.ret_1.abs().ge(2.0)
        & (frame.z_30 * frame.ret_10).gt(0.0)
        & frame.z_30.abs().ge(1.0)
        & frame.volume_ratio.ge(0.80)
        & frame.observed.ge(0.98)
    )
    frame["signal"] = np.where(setup & frame.ret_10.gt(0.0), "DOWN", np.where(setup, "UP", None))
    frame["branch"] = "one_sided_exhaustion_reclaim"
    return frame[setup & frame.move_bps.notna()].replace([np.inf, -np.inf], np.nan).dropna(
        subset=["entry", "settle", "efficiency_10", "trend_strength", "z_30"]
    ).reset_index(names="time")


def cooldown_after_confirmation(frame: pd.DataFrame) -> pd.DataFrame:
    accepted: list[dict[str, Any]] = []
    last_time: pd.Timestamp | None = None
    for row in frame.sort_values("time").to_dict("records"):
        timestamp = pd.Timestamp(row["time"])
        if last_time is not None and (timestamp - last_time).total_seconds() < 600:
            continue
        accepted.append(row)
        last_time = timestamp
    return pd.DataFrame(accepted)


def run() -> dict[str, Any]:
    raw = build_raw_candidates(read_minutes())
    raw = raw[raw.time.ge(pd.Timestamp("2026-07-05", tz="UTC"))].copy()
    rows: list[dict[str, Any]] = []
    for source in SOURCES:
        rows.extend(extract_source(source, raw))
        gc.collect()
    verified = pd.DataFrame(rows)
    if not verified.empty:
        verified = verified.sort_values(["time", "source"]).drop_duplicates("time", keep="last")
        verified = verified[verified.confirmation_votes.ge(2)].copy()
    trades = cooldown_after_confirmation(verified)
    trades["period"] = np.select(
        [
            trades.time.lt(pd.Timestamp("2026-07-11", tz="UTC")),
            trades.time.lt(pd.Timestamp("2026-07-14", tz="UTC")),
        ],
        ["development_july5_10", "validation_july11_13"],
        default="forward_july14_15",
    )
    trades["beijing_day"] = trades.time.dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
    latest = attach_delay_outcomes(trades[trades.period.eq("forward_july14_15")].copy())
    for delay in DELAYS:
        trades.loc[latest.index, f"move_bps_d{delay}"] = latest[f"move_bps_d{delay}"]
    trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    period_hours = {
        "development_july5_10": 144.0,
        "validation_july11_13": 72.0,
        "forward_july14_15": 35.16,
    }
    report = {
        "method": {
            "parameterSearch": False, "causal": True,
            "parameters": "identical to V6",
            "onlyChange": "600-second cooldown starts after order-book confirmation",
        },
        "rawCandidatesSinceJuly5": len(raw),
        "confirmedBeforeCooldown": len(verified),
        "emittedAfterCooldown": len(trades),
        "periods": {
            period: metrics(group, "move_bps", period_hours[period])
            for period, group in trades.groupby("period")
        },
        "forwardDelays": {
            f"delay{delay}s": metrics(latest, f"move_bps_d{delay}", 35.16)
            for delay in DELAYS
        },
        "forwardByDayDelay6s": {
            day: metrics(group, "move_bps_d6", 24.0)
            for day, group in latest.groupby("beijing_day")
        },
        "acceptance": {"minTradesPerDay": 10.0, "minWinRate": 55.56, "maxDrawdownU": 20.0, "maxLossStreak": 3, "allDelaysMustBeProfitable": True},
        "tradesCsv": str(OUT_CSV),
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
