"""Research a native causal 2-minute strategy with 10-minute settlement.

The 2-minute layer owns market classification and direction. One-second data
is used only for realistic delayed entry and settlement. Rules are fixed from
auction concepts before running the replay; this script performs no search.
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

from research_two_min_guard_recovery import (  # noqa: E402
    build_two_min_features,
    clean,
    load_research_sources,
)
from run_multi_normal_hf_stable_backtest import (  # noqa: E402
    LoadedSource,
    metrics,
    price_at_or_after,
    utc,
)
from multi_normal_hf_stable_core import MultiNormalHFStableConfig  # noqa: E402


OUT_JSON = ROOT / "tmp" / "two_min_native_strategy_latest.json"
OUT_CSV = ROOT / "tmp" / "two_min_native_strategy_trades.csv"
HORIZON_SEC = 600
GAP_SEC = 600


def finite(row: pd.Series, keys: tuple[str, ...]) -> bool:
    return all(key in row and math.isfinite(float(row[key])) for key in keys)


def decide(row: pd.Series) -> tuple[str | None, str | None, dict[str, Any]]:
    keys = (
        "ret2_bps",
        "ret10_bps",
        "sigma10_bps",
        "range10_bps",
        "z30",
        "center_slope10_bps",
        "volume_ratio30",
        "efficiency10",
        "direction_persistence10",
    )
    if not finite(row, keys):
        return None, None, {}
    regime = str(row.get("regime") or "unknown")
    z30 = float(row["z30"])
    ret2 = float(row["ret2_bps"])
    sigma10 = float(row["sigma10_bps"])
    range10 = float(row["range10_bps"])
    slope = float(row["center_slope10_bps"])
    efficiency = float(row["efficiency10"])
    persistence = float(row["direction_persistence10"])
    volume_ratio = float(row["volume_ratio30"])
    payload = {
        "two_regime": regime,
        "two_z30": z30,
        "two_ret2_bps": ret2,
        "two_ret10_bps": float(row["ret10_bps"]),
        "two_sigma10_bps": sigma10,
        "two_range10_bps": range10,
        "two_center_slope10_bps": slope,
        "two_efficiency10": efficiency,
        "two_direction_persistence10": persistence,
        "two_volume_ratio30": volume_ratio,
    }

    # Balance auction: price is at a 30-minute value tail, the 10-minute path
    # is rotational rather than efficient, and the completed 2-minute bar has
    # already moved inward.
    if (
        regime == "flat"
        and sigma10 <= 5.0
        and range10 <= 30.0
        and abs(slope) <= 5.0
        and 1.2 <= abs(z30) <= 2.0
        and efficiency <= 0.55
    ):
        signal = "DOWN" if z30 > 0.0 else "UP"
        sign = 1.0 if signal == "UP" else -1.0
        if sign * ret2 > 0.0:
            return signal, "two_min_balance_tail_reversion", payload

    # Accepted migration: direction agrees across the 10/30/60-minute regime,
    # the recent path is efficient and persistent, and the last completed bar
    # still advances with non-depleted volume.
    if regime in {"trend_up", "trend_down"}:
        sign = 1.0 if regime == "trend_up" else -1.0
        if (
            sign * z30 >= 0.5
            and sign * ret2 > 0.0
            and efficiency >= 0.55
            and persistence >= 0.6
            and volume_ratio >= 0.7
        ):
            signal = "UP" if sign > 0.0 else "DOWN"
            return signal, "two_min_accepted_migration_follow", payload
    return None, None, payload


def replay(source: LoadedSource, delay_sec: int = 2) -> pd.DataFrame:
    features = build_two_min_features(source.data)
    close = source.data["close"].astype(float)
    rows: list[dict[str, Any]] = []
    last_emit: pd.Timestamp | None = None
    for bar_time, row in features.iterrows():
        detected_time = utc(bar_time) + pd.Timedelta(minutes=2) - pd.Timedelta(seconds=1)
        if detected_time < source.test_start or detected_time > source.test_end:
            continue
        signal, reason, payload = decide(row)
        if not signal:
            continue
        if last_emit is not None and (detected_time - last_emit).total_seconds() < GAP_SEC:
            continue
        target = detected_time + pd.Timedelta(seconds=delay_sec)
        entry = price_at_or_after(close, target)
        settle = price_at_or_after(close, target + pd.Timedelta(seconds=HORIZON_SEC))
        if entry is None or settle is None:
            continue
        sign = 1.0 if signal == "UP" else -1.0
        outcome = (settle[1] / entry[1] - 1.0) * 10000.0 * sign
        rows.append(
            {
                "source": source.spec.name,
                "role": source.spec.role,
                "bar_time": bar_time,
                "detected_time": detected_time,
                "entry_time": entry[0],
                "settle_time": settle[0],
                "signal": signal,
                "reason": reason,
                "entry": entry[1],
                "settle": settle[1],
                "signed_outcome_bps": outcome,
                "won": bool(outcome > 0.0),
                **payload,
            }
        )
        last_emit = detected_time
    return pd.DataFrame(rows)


def report_for(frame: pd.DataFrame, sources: list[LoadedSource]) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for role in ("history", "independent", "today"):
        subset = frame[frame["role"] == role]
        hours = sum(source.hours for source in sources if source.spec.role == role)
        report[role] = {
            "all": metrics(subset, hours),
            "byReason": {
                str(name): metrics(group, hours)
                for name, group in subset.groupby("reason", dropna=False)
            },
        }
    hours = sum(source.hours for source in sources)
    report["combined"] = {
        "all": metrics(frame, hours),
        "byReason": {
            str(name): metrics(group, hours)
            for name, group in frame.groupby("reason", dropna=False)
        },
    }
    return report


def main() -> None:
    sources = load_research_sources(MultiNormalHFStableConfig())
    frames = [replay(source, 2) for source in sources]
    trades = pd.concat(frames, ignore_index=True)
    delay_sweep: dict[str, Any] = {}
    for delay in (0, 2, 5, 10):
        delayed = pd.concat([replay(source, delay) for source in sources], ignore_index=True)
        delay_sweep[str(delay)] = report_for(delayed, sources)
    report = {
        "method": {
            "causal": "Signals use completed 2-minute OHLCV only; entries use one-second prices after the configured delay.",
            "balance": "Flat low-volatility 30-minute tail with an inward completed 2-minute bar.",
            "migration": "Aligned mature trend with efficient persistent 2-minute movement and non-depleted volume.",
            "search": False,
            "horizonSec": HORIZON_SEC,
            "gapSec": GAP_SEC,
            "delaySec": 2,
        },
        "sources": {
            source.spec.name: {
                "role": source.spec.role,
                "start": source.test_start,
                "end": source.test_end,
                "hours": round(source.hours, 4),
            }
            for source in sources
        },
        "result": report_for(trades, sources),
        "delaySweep": delay_sweep,
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(json.dumps(clean(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
