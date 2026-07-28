"""Causal foundation study for Gaussian-like behavior in 10-minute contracts.

This is a measurement report, not a trading strategy.  It deliberately fixes
the windows and execution delays before looking at any outcomes.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from second_backtest.data import load_second_bars  # noqa: E402


WINDOWS_SEC = (60, 120, 300, 600, 900, 1800)
DELAYS_SEC = (0, 5, 6, 10)
HORIZON_SEC = 600
SPACING_SEC = 600
MIN_HISTORY_COVERAGE = 0.95
MIN_FUTURE_COVERAGE = 0.95
PAYOUT_RATE = 0.8
STAKE_U = 5.0


def finite_float(value: float | int | None) -> float | None:
    number = float(value) if value is not None else math.nan
    return number if math.isfinite(number) else None


def shape(values: np.ndarray) -> dict[str, float | None]:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    n = len(values)
    if n < 8:
        return {"count": n, "mean": None, "sigma": None, "skew": None, "excessKurtosis": None, "jb": None}
    mean = float(values.mean())
    centered = values - mean
    m2 = float(np.mean(centered**2))
    if m2 <= 0.0:
        return {"count": n, "mean": mean, "sigma": 0.0, "skew": 0.0, "excessKurtosis": 0.0, "jb": 0.0}
    m3 = float(np.mean(centered**3))
    m4 = float(np.mean(centered**4))
    skew = m3 / (m2 ** 1.5)
    excess = m4 / (m2**2) - 3.0
    jb = n / 6.0 * (skew**2 + excess**2 / 4.0)
    return {
        "count": n,
        "mean": mean,
        "sigma": math.sqrt(m2),
        "skew": skew,
        "excessKurtosis": excess,
        "jb": jb,
    }


def price_tick(bars: pd.DataFrame, target: pd.Timestamp) -> tuple[float | None, bool]:
    if target not in bars.index:
        return None, False
    row = bars.loc[target]
    if not bool(row.observed) or not math.isfinite(float(row.close)):
        return None, False
    return float(row.close), True


def window_stats(bars: pd.DataFrame, event_time: pd.Timestamp, window: int) -> dict | None:
    start = event_time - pd.Timedelta(seconds=window - 1)
    frame = bars.loc[start:event_time]
    if len(frame) != window:
        return None
    coverage = float(frame.observed.mean())
    if coverage < MIN_HISTORY_COVERAGE:
        return None
    observed = frame[frame.observed].copy()
    log_price = np.log(observed.close.to_numpy(float))
    if len(log_price) < 8:
        return None
    levels = shape(log_price)
    returns = np.diff(log_price) * 10000.0
    return_shape = shape(returns)
    sigma_level_bps = float(levels["sigma"] * 10000.0) if levels["sigma"] is not None else None
    last_level = float(log_price[-1])
    z = (last_level - float(levels["mean"])) / float(levels["sigma"]) if levels["sigma"] else 0.0
    z_values = (log_price - float(levels["mean"])) / float(levels["sigma"]) if levels["sigma"] else np.zeros(len(log_price))
    inside1 = float(np.mean(np.abs(z_values) <= 1.0))
    inside2 = float(np.mean(np.abs(z_values) <= 2.0))
    times = (observed.index - observed.index[0]).total_seconds().to_numpy(float)
    slope = float(np.polyfit(times, log_price, 1)[0] * 600.0 * 10000.0) if len(log_price) >= 2 else 0.0
    ret = (log_price[-1] - log_price[0]) * 10000.0
    return {
        "coverage": coverage,
        "sampleCount": int(len(log_price)),
        "priceSigmaBps": sigma_level_bps,
        "priceZ": float(z),
        "inside1": inside1,
        "inside2": inside2,
        "slopeBpsPer600s": slope,
        "retBps": float(ret),
        "returnMeanBps": return_shape["mean"],
        "returnSigmaBps": return_shape["sigma"],
        "returnSkew": return_shape["skew"],
        "returnExcessKurtosis": return_shape["excessKurtosis"],
        "returnJb": return_shape["jb"],
        "normalLike": bool(
            return_shape["skew"] is not None
            and abs(float(return_shape["skew"])) <= 0.5
            and return_shape["excessKurtosis"] is not None
            and abs(float(return_shape["excessKurtosis"])) <= 1.0
            and inside1 >= 0.60
        ),
    }


def outcome(bars: pd.DataFrame, event_time: pd.Timestamp, delay: int) -> dict | None:
    entry_time = event_time + pd.Timedelta(seconds=delay)
    settle_time = entry_time + pd.Timedelta(seconds=HORIZON_SEC)
    future = bars.loc[entry_time:settle_time]
    if len(future) != HORIZON_SEC + 1 or float(future.observed.mean()) < MIN_FUTURE_COVERAGE:
        return None
    entry, entry_ok = price_tick(bars, entry_time)
    settle, settle_ok = price_tick(bars, settle_time)
    if not entry_ok or not settle_ok or entry is None or settle is None:
        return None
    move = (settle / entry - 1.0) * 10000.0
    won = settle > entry
    pnl = STAKE_U * PAYOUT_RATE if won else -STAKE_U
    return {
        "delaySec": delay,
        "entryTime": entry_time.isoformat(),
        "settleTime": settle_time.isoformat(),
        "entry": entry,
        "settle": settle,
        "moveBps": move,
        "up": bool(won),
        "pnlU": pnl,
    }


def summarize(rows: pd.DataFrame) -> dict:
    if rows.empty:
        return {"samples": 0, "wins": 0, "winRate": None, "pnlU": 0.0}
    wins = int(rows.up.sum())
    return {
        "samples": int(len(rows)),
        "wins": wins,
        "winRate": round(wins / len(rows) * 100.0, 2),
        "pnlU": round(float(rows.pnlU.sum()), 2),
        "meanMoveBps": round(float(rows.moveBps.mean()), 4),
        "medianMoveBps": round(float(rows.moveBps.median()), 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(ROOT / "data" / "server_latest" / "btcusdt_1s_trades.csv"))
    parser.add_argument("--output-prefix", default=str(ROOT / "tmp" / "normality_10m_foundation"))
    args = parser.parse_args()

    bars = load_second_bars(args.input, include_shards=False).sort_index()
    observed = bars.observed.astype(bool)
    gaps = np.diff(bars.index.view("int64")) / 1e9
    rows: list[dict] = []
    start = bars.index.min() + pd.Timedelta(seconds=max(WINDOWS_SEC))
    end = bars.index.max() - pd.Timedelta(seconds=max(DELAYS_SEC) + HORIZON_SEC)
    for event_time in pd.date_range(start.ceil(f"{SPACING_SEC}s"), end, freq=f"{SPACING_SEC}s", tz="UTC"):
        features: dict[str, object] = {"eventTime": event_time.isoformat()}
        valid = True
        for window in WINDOWS_SEC:
            stats = window_stats(bars, event_time, window)
            if stats is None:
                valid = False
                break
            features[f"w{window}"] = stats
        if not valid:
            continue
        for delay in DELAYS_SEC:
            result = outcome(bars, event_time, delay)
            if result is not None:
                features[f"d{delay}"] = result
        if any(f"d{delay}" in features for delay in DELAYS_SEC):
            rows.append(features)

    flat_rows = []
    for row in rows:
        for delay in DELAYS_SEC:
            result = row.get(f"d{delay}")
            if not result:
                continue
            flat = {"eventTime": row["eventTime"], **result}
            for window in WINDOWS_SEC:
                stats = row[f"w{window}"]
                flat.update({f"w{window}_{key}": value for key, value in stats.items()})
            flat_rows.append(flat)
    flat = pd.DataFrame(flat_rows)
    summaries = {}
    for delay in DELAYS_SEC:
        summaries[f"delay{delay}s"] = summarize(flat[flat.delaySec == delay]) if not flat.empty else summarize(flat)

    bins = [-np.inf, -2.0, -1.0, 0.0, 1.0, 2.0, np.inf]
    z_summary = {}
    normality_summary = {}
    by_day = {}
    if not flat.empty:
        target = flat[flat.delaySec == 5].copy()
        target["zBin"] = pd.cut(target.w600_priceZ, bins=bins, right=False)
        z_summary = {
            str(interval): summarize(group)
            for interval, group in target.groupby("zBin", observed=False)
        }
        normality_summary = {
            str(state): summarize(group)
            for state, group in target.groupby("w600_normalLike", observed=False)
        }
        target["utcDate"] = target.eventTime.str.slice(0, 10)
        by_day = {
            str(day): summarize(group)
            for day, group in target.groupby("utcDate", observed=False)
        }
    report = {
        "status": "measurement_only_no_strategy",
        "input": str(Path(args.input).resolve()),
        "fixedWindowsSec": WINDOWS_SEC,
        "fixedExecutionDelaysSec": DELAYS_SEC,
        "horizonSec": HORIZON_SEC,
        "spacingSec": SPACING_SEC,
        "minHistoryCoverage": MIN_HISTORY_COVERAGE,
        "minFutureCoverage": MIN_FUTURE_COVERAGE,
        "breakevenWinRatePct": round(100.0 * STAKE_U / (STAKE_U + STAKE_U * PAYOUT_RATE), 4),
        "coverage": {
            "start": bars.index.min().isoformat(),
            "end": bars.index.max().isoformat(),
            "seconds": int(len(bars)),
            "observedPct": round(float(observed.mean()) * 100.0, 4),
            "maxGapSec": int(gaps.max()) if len(gaps) else 0,
        },
        "events": int(len(rows)),
        "outcomes": summaries,
        "z600Delay5s": z_summary,
        "normalityState600Delay5s": normality_summary,
        "byUtcDateDelay5s": by_day,
        "warning": "All results describe fixed windows; they do not select a profitable branch or prove a tradable edge.",
    }
    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    flat.to_csv(prefix.with_name(prefix.name + "_samples.csv"), index=False, encoding="utf-8-sig")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
