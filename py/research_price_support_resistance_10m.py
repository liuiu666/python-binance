"""Price-only support/resistance path study for 10-minute contracts.

This report describes what happens after a price-only rolling-band touch. It
uses no order book, flow, or future-derived filter. Touch labels use data up to
the event; future path fields are outcomes for research only.
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


WINDOW_SEC = 600
ENTRY_DELAY_SEC = 5
HORIZON_SEC = 600
EVENT_SPACING_SEC = 60
TOUCH_Z = 1.0
STRONG_TOUCH_Z = 2.0
MIN_COVERAGE = 0.95


def safe_float(value: float | int | None) -> float | None:
    number = float(value) if value is not None else math.nan
    return number if math.isfinite(number) else None


def level_stats(frame: pd.DataFrame) -> dict | None:
    if len(frame) != WINDOW_SEC or float(frame.observed.mean()) < MIN_COVERAGE:
        return None
    observed = frame[frame.observed]
    if len(observed) < 30:
        return None
    log_price = np.log(observed.close.to_numpy(float))
    center = float(log_price.mean())
    sigma = float(log_price.std(ddof=0))
    if sigma <= 0.0 or not math.isfinite(sigma):
        return None
    z = (log_price - center) / sigma
    times = (observed.index - observed.index[0]).total_seconds().to_numpy(float)
    recent_returns = {}
    for seconds in (10, 30, 60, 120, 300):
        position = int(np.searchsorted(times, times[-1] - seconds, side="left"))
        recent_returns[f"ret{seconds}Bps"] = float((log_price[-1] - log_price[position]) * 10000.0)
    lower_touch = z <= -TOUCH_Z
    upper_touch = z >= TOUCH_Z
    lower_last = np.flatnonzero(lower_touch)
    upper_last = np.flatnonzero(upper_touch)

    def touch_run(mask: np.ndarray) -> tuple[float, int]:
        starts = mask & ~np.r_[False, mask[:-1]]
        episodes = int(starts.sum())
        if not mask[-1]:
            return 0.0, episodes
        last_inside = np.flatnonzero(~mask)
        run_start = int(last_inside[-1] + 1) if len(last_inside) else 0
        return float(times[-1] - times[run_start]), episodes

    lower_age, lower_episodes = touch_run(lower_touch)
    upper_age, upper_episodes = touch_run(upper_touch)
    slope = float(np.polyfit(times, log_price, 1)[0] * 600.0 * 10000.0)
    returns = np.diff(log_price) * 10000.0
    centered = returns - returns.mean()
    r_sigma = float(returns.std(ddof=0))
    skew = float((centered**3).mean() / r_sigma**3) if r_sigma > 0 else 0.0
    kurt = float((centered**4).mean() / r_sigma**4 - 3.0) if r_sigma > 0 else 0.0
    return {
        "center": center,
        "sigma": sigma,
        "sigmaBps": sigma * 10000.0,
        "z": float(z[-1]),
        "inside1": float(np.mean(np.abs(z) <= 1.0)),
        "inside2": float(np.mean(np.abs(z) <= 2.0)),
        "slopeBps600": slope,
        "retBps": float((log_price[-1] - log_price[0]) * 10000.0),
        "returnSigmaBps": r_sigma,
        "returnSkew": skew,
        "returnExcessKurtosis": kurt,
        "rangeBps": float((log_price.max() - log_price.min()) * 10000.0),
        **recent_returns,
        "lowerTouchCount": int(lower_touch.sum()),
        "upperTouchCount": int(upper_touch.sum()),
        "lowerDwellPct": float(lower_touch.mean() * 100.0),
        "upperDwellPct": float(upper_touch.mean() * 100.0),
        "lowerLastTouchAgeSec": float(times[-1] - times[lower_last[-1]]) if len(lower_last) else None,
        "upperLastTouchAgeSec": float(times[-1] - times[upper_last[-1]]) if len(upper_last) else None,
        "lowerOutsideAgeSec": lower_age,
        "upperOutsideAgeSec": upper_age,
        "lowerEpisodeCount": lower_episodes,
        "upperEpisodeCount": upper_episodes,
    }


def future_path(bars: pd.DataFrame, event: pd.Timestamp, side: str, stats: dict) -> dict | None:
    entry_time = event + pd.Timedelta(seconds=ENTRY_DELAY_SEC)
    settle_time = entry_time + pd.Timedelta(seconds=HORIZON_SEC)
    frame = bars.loc[entry_time:settle_time]
    if len(frame) != HORIZON_SEC + 1 or float(frame.observed.mean()) < MIN_COVERAGE:
        return None
    observed = frame[frame.observed]
    if len(observed) < int(HORIZON_SEC * MIN_COVERAGE):
        return None
    prices = observed.close.to_numpy(float)
    log_prices = np.log(prices)
    entry = float(prices[0])
    settle = float(prices[-1])
    sign = 1.0 if side == "LOWER" else -1.0
    signed_final_bps = sign * (log_prices[-1] - math.log(entry)) * 10000.0
    if side == "LOWER":
        signed_high_bps = float((log_prices.max() - math.log(entry)) * 10000.0)
        signed_low_bps = float((log_prices.min() - math.log(entry)) * 10000.0)
    else:
        signed_high_bps = float((math.log(entry) - log_prices.min()) * 10000.0)
        signed_low_bps = float((math.log(entry) - log_prices.max()) * 10000.0)
    signed_steps = sign * np.diff(log_prices) * 10000.0
    future_z = (log_prices - stats["center"]) / stats["sigma"]
    center_cross = np.flatnonzero(future_z * sign >= 0.0)
    elapsed = (observed.index - observed.index[0]).total_seconds().to_numpy(float)
    midpoint = len(log_prices) // 2
    favorable_run = 0
    max_favorable_run = 0
    for step in signed_steps:
        if step > 0.0:
            favorable_run += 1
            max_favorable_run = max(max_favorable_run, favorable_run)
        else:
            favorable_run = 0
    if side == "LOWER":
        adverse = float((log_prices.min() - math.log(entry)) * 10000.0)
    else:
        adverse = float((math.log(entry) - log_prices.max()) * 10000.0)
    if signed_final_bps >= 0.5 * stats["sigmaBps"]:
        outcome = "revert"
    elif signed_final_bps <= -0.5 * stats["sigmaBps"]:
        outcome = "continue"
    else:
        outcome = "flat"
    return {
        "entry": entry,
        "settle": settle,
        "settleMoveBps": (settle / entry - 1.0) * 10000.0,
        "signedFinalBps": signed_final_bps,
        "signedMaxFavorableBps": signed_high_bps,
        "signedMaxAdverseBps": adverse,
        "favorableStepPct": float(np.mean(signed_steps > 0.0) * 100.0),
        "firstHalfSignedBps": float(sign * (log_prices[midpoint] - log_prices[0]) * 10000.0),
        "secondHalfSignedBps": float(sign * (log_prices[-1] - log_prices[midpoint]) * 10000.0),
        "maxFavorableRunSec": int(max_favorable_run),
        "pathSlopeBps600": float(np.polyfit(np.arange(len(log_prices)), log_prices, 1)[0] * 600.0 * 10000.0 * sign),
        "centerCrossSec": int(elapsed[center_cross[0]]) if len(center_cross) else None,
        "outcome": outcome,
    }


def summarize(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {"samples": 0}
    return {
        "samples": int(len(frame)),
        "revert": int((frame.outcome == "revert").sum()),
        "continue": int((frame.outcome == "continue").sum()),
        "flat": int((frame.outcome == "flat").sum()),
        "revertPct": round(float((frame.outcome == "revert").mean()) * 100.0, 2),
        "meanSignedFinalBps": round(float(frame.signedFinalBps.mean()), 4),
        "medianSignedFinalBps": round(float(frame.signedFinalBps.median()), 4),
        "meanFavorableStepPct": round(float(frame.favorableStepPct.mean()), 2),
        "centerCrossPct": round(float(frame.centerCrossSec.notna().mean()) * 100.0, 2),
        "medianCenterCrossSec": round(float(frame.centerCrossSec.dropna().median()), 2) if frame.centerCrossSec.notna().any() else None,
        "medianFirstHalfSignedBps": round(float(frame.firstHalfSignedBps.median()), 4),
        "medianSecondHalfSignedBps": round(float(frame.secondHalfSignedBps.median()), 4),
        "medianMaxFavorableRunSec": round(float(frame.maxFavorableRunSec.median()), 2),
    }


def pre_feature_summary(frame: pd.DataFrame) -> dict:
    fields = (
        "feature_z",
        "feature_sigmaBps",
        "feature_inside1",
        "feature_slopeBps600",
        "feature_retBps",
        "feature_returnSigmaBps",
        "feature_returnSkew",
        "feature_returnExcessKurtosis",
        "feature_rangeBps",
        "feature_signedRet10Bps",
        "feature_signedRet30Bps",
        "feature_signedRet60Bps",
        "feature_signedRet120Bps",
        "feature_signedRet300Bps",
        "feature_signedSpeed10Minus60BpsPerSec",
        "feature_signedSpeed30Minus120BpsPerSec",
        "feature_sideTouchCount",
        "feature_sideDwellPct",
        "feature_sideLastTouchAgeSec",
        "feature_sideOutsideAgeSec",
        "feature_sideEpisodeCount",
    )
    if frame.empty:
        return {"samples": 0}
    out = {"samples": int(len(frame))}
    for field in fields:
        if field in frame:
            values = pd.to_numeric(frame[field], errors="coerce").dropna()
            out[field + "Median"] = round(float(values.median()), 4) if not values.empty else None
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(ROOT / "data" / "server_latest" / "btcusdt_1s_trades.csv"))
    parser.add_argument("--output-prefix", default=str(ROOT / "tmp" / "price_support_resistance_10m"))
    args = parser.parse_args()

    bars = load_second_bars(args.input, include_shards=False).sort_index()
    start = bars.index.min() + pd.Timedelta(seconds=WINDOW_SEC)
    end = bars.index.max() - pd.Timedelta(seconds=ENTRY_DELAY_SEC + HORIZON_SEC)
    samples: list[dict] = []
    for event in pd.date_range(start.ceil(f"{EVENT_SPACING_SEC}s"), end, freq=f"{EVENT_SPACING_SEC}s", tz="UTC"):
        frame = bars.loc[event - pd.Timedelta(seconds=WINDOW_SEC - 1):event]
        stats = level_stats(frame)
        if stats is None:
            continue
        if stats["z"] <= -TOUCH_Z:
            side = "LOWER"
        elif stats["z"] >= TOUCH_Z:
            side = "UPPER"
        else:
            continue
        side_sign = 1.0 if side == "LOWER" else -1.0
        signed10 = side_sign * stats["ret10Bps"]
        signed30 = side_sign * stats["ret30Bps"]
        signed60 = side_sign * stats["ret60Bps"]
        signed120 = side_sign * stats["ret120Bps"]
        signed300 = side_sign * stats["ret300Bps"]
        stats.update({
            "signedRet10Bps": signed10,
            "signedRet30Bps": signed30,
            "signedRet60Bps": signed60,
            "signedRet120Bps": signed120,
            "signedRet300Bps": signed300,
            "signedSpeed10Minus60BpsPerSec": signed10 / 10.0 - signed60 / 60.0,
            "signedSpeed30Minus120BpsPerSec": signed30 / 30.0 - signed120 / 120.0,
            "sideTouchCount": stats["lowerTouchCount"] if side == "LOWER" else stats["upperTouchCount"],
            "sideDwellPct": stats["lowerDwellPct"] if side == "LOWER" else stats["upperDwellPct"],
            "sideLastTouchAgeSec": stats["lowerLastTouchAgeSec"] if side == "LOWER" else stats["upperLastTouchAgeSec"],
            "sideOutsideAgeSec": stats["lowerOutsideAgeSec"] if side == "LOWER" else stats["upperOutsideAgeSec"],
            "sideEpisodeCount": stats["lowerEpisodeCount"] if side == "LOWER" else stats["upperEpisodeCount"],
        })
        path = future_path(bars, event, side, stats)
        if path is None:
            continue
        row = {"eventTime": event.isoformat(), "side": side, "strongTouch": abs(stats["z"]) >= STRONG_TOUCH_Z}
        row.update({f"feature_{key}": value for key, value in stats.items()})
        row.update(path)
        samples.append(row)

    data = pd.DataFrame(samples)
    z_bins = [-np.inf, -2.0, -1.5, -1.0, 1.0, 1.5, 2.0, np.inf]
    z_band_summary = {}
    age_band_summary = {}
    episode_summary = {}
    if not data.empty:
        data["zBand"] = pd.cut(data.feature_z, bins=z_bins, right=False)
        z_band_summary = {
            f"{side}_{band}": summarize(group)
            for side in ("LOWER", "UPPER")
            for band, group in data[data.side == side].groupby("zBand", observed=False)
        }
        age_bins = [-np.inf, 30.0, 60.0, 120.0, 300.0, np.inf]
        data["outsideAgeBand"] = pd.cut(data.feature_sideOutsideAgeSec, bins=age_bins, right=False)
        age_band_summary = {
            f"{side}_{band}": summarize(group)
            for side in ("LOWER", "UPPER")
            for band, group in data[data.side == side].groupby("outsideAgeBand", observed=False)
        }
        episode_summary = {
            f"{side}_{episodes}": summarize(group)
            for side in ("LOWER", "UPPER")
            for episodes, group in data[data.side == side].groupby("feature_sideEpisodeCount", observed=False)
        }
    report = {
        "status": "price_only_path_measurement",
        "input": str(Path(args.input).resolve()),
        "windowSec": WINDOW_SEC,
        "entryDelaySec": ENTRY_DELAY_SEC,
        "horizonSec": HORIZON_SEC,
        "eventSpacingSec": EVENT_SPACING_SEC,
        "touchZ": TOUCH_Z,
        "strongTouchZ": STRONG_TOUCH_Z,
        "minCoverage": MIN_COVERAGE,
        "coverage": {
            "start": bars.index.min().isoformat(),
            "end": bars.index.max().isoformat(),
            "seconds": int(len(bars)),
            "observedPct": round(float(bars.observed.mean()) * 100.0, 4),
        },
        "samples": int(len(data)),
        "bySide": {side: summarize(data[data.side == side]) for side in ("LOWER", "UPPER")},
        "byTouchStrength": {
            str(strong): summarize(data[data.strongTouch == strong])
            for strong in (False, True)
        },
        "bySideAndOutcome": {
            f"{side}_{outcome}": summarize(data[(data.side == side) & (data.outcome == outcome)])
            for side in ("LOWER", "UPPER")
            for outcome in ("revert", "continue", "flat")
        },
        "preFeaturesBySideAndOutcome": {
            f"{side}_{outcome}": pre_feature_summary(data[(data.side == side) & (data.outcome == outcome)])
            for side in ("LOWER", "UPPER")
            for outcome in ("revert", "continue", "flat")
        },
        "fixedZBandBySide": z_band_summary,
        "fixedOutsideAgeBySide": age_band_summary,
        "touchEpisodesBySide": episode_summary,
        "interpretationRule": "revert means signed final move >= 0.5 sigma; continue means <= -0.5 sigma; otherwise flat. Fixed for description, not optimized for profit.",
        "warning": "Overlapping 60-second touch samples are descriptive and are not a backtest or deployment approval.",
    }
    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    data.to_csv(prefix.with_name(prefix.name + "_samples.csv"), index=False, encoding="utf-8-sig")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
