"""All-signal archived agg-trade validation for the frozen V22 candidate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from research_actual_horizon_walkforward_v21 import load_candidates
from research_archived_aggtrades_v23 import (
    ARCHIVE_END,
    ARCHIVE_START,
    CACHE,
    DELAYS_SEC,
    HORIZON_SEC,
    MAX_TICK_LAG_SEC,
    _needed_ranges,
    download_archives,
    load_filtered_ticks,
)
from research_directional_candidate_v22 import CELL, PROFILE, SIGNAL, Z_THRESHOLD
from research_long_history_walkforward_v20 import OUT_CANDIDATES
from research_minute_volatility_normal_v15 import clean
from v14_validation import (
    apply_family_cooldown,
    metrics_by_delay,
    normalize_candidates,
    normalize_futures_ticks,
    resolve_candidate_trades,
    summarize_metrics,
)


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "tmp" / "v24_all_archived_aggtrades_validation_20260730.json"
OUT_TICKS = ROOT / "tmp" / "v24_all_archived_aggtrades_filtered_ticks_20260730.csv"
OUT_TRADES = ROOT / "tmp" / "v24_all_archived_aggtrades_trades_20260730.csv"


def select_all_reverse_signals(candidates: pd.DataFrame) -> pd.DataFrame:
    return candidates.loc[
        candidates["cell"].eq(CELL)
        & candidates["profile"].eq(PROFILE)
        & candidates["signal"].eq(SIGNAL)
        & candidates["z"].ge(Z_THRESHOLD)
        & candidates["signal_time"].ge(ARCHIVE_START)
        & candidates["signal_time"].lt(ARCHIVE_END)
    ].sort_values("signal_time", kind="stable").reset_index(drop=True)


def run(candidate_path: str | Path, workers: int) -> dict[str, Any]:
    candidates = load_candidates(candidate_path)
    selected = select_all_reverse_signals(candidates)
    if len(selected) < 50:
        raise ValueError(f"unexpectedly small all-signal set: {len(selected)}")
    ranges = _needed_ranges(selected)
    dates = sorted(ranges)
    archives = download_archives(dates, workers=workers)
    ticks = load_filtered_ticks(archives, ranges)
    ticks.to_csv(OUT_TICKS, index=False, encoding="utf-8-sig")
    normalized_ticks = normalize_futures_ticks(
        ticks,
        time_col="time",
        price_col="price",
        market_col=None,
        require_futures=False,
    )
    normalized_candidates = normalize_candidates(
        selected.rename(columns={"signal_time": "time"}),
        time_col="time",
        signal_col="signal",
        family_col="profile",
        branch_col="cell",
    )
    cooldown = apply_family_cooldown(
        normalized_candidates, cooldown_sec=HORIZON_SEC
    )
    trades = resolve_candidate_trades(
        cooldown,
        normalized_ticks,
        delays_sec=DELAYS_SEC,
        execution_base_lag_sec=0,
        horizon_sec=HORIZON_SEC,
        amount_u=5.0,
        payout_rate=0.8,
        max_tick_lag_sec=MAX_TICK_LAG_SEC,
    )
    trades.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    settled_delays = (
        trades.loc[trades["status"].isin(("won", "lost", "tie"))]
        .groupby("candidate_key")["delay_sec"]
        .nunique()
    )
    common_keys = set(
        settled_delays.loc[settled_delays.eq(len(DELAYS_SEC))].index
    )
    common = trades.loc[trades["candidate_key"].isin(common_keys)].copy()
    by_delay = metrics_by_delay(trades)
    common_by_delay = metrics_by_delay(common)
    by_year_delay = {
        f"{year}|{int(delay)}": summarize_metrics(group)
        for (year, delay), group in trades.assign(
            year=pd.to_datetime(trades["signal_time"], utc=True).dt.year
        ).groupby(["year", "delay_sec"], sort=True)
    }
    by_month_delay = {
        f"{month}|{int(delay)}": summarize_metrics(group)
        for (month, delay), group in trades.assign(
            month=pd.to_datetime(trades["signal_time"], utc=True).dt.strftime("%Y-%m")
        ).groupby(["month", "delay_sec"], sort=True)
    }
    delay_rows = [common_by_delay.get(str(delay)) for delay in DELAYS_SEC]
    passed = bool(
        len(common_keys) >= 50
        and all(row is not None for row in delay_rows)
        and all(
            row["settled"] >= 50
            and row["winRatePct"] is not None
            and row["winRatePct"] >= 63.0
            and row["pnlU"] > 0.0
            and row["maxDrawdownU"] <= 20.0
            and row["maxLossStreak"] <= 2
            for row in delay_rows
            if row is not None
        )
    )
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "status": "V24_ALL_ARCHIVED_AGGTRADES_VALIDATION",
        "safety": {
            "researchOnly": True,
            "tradeEnabled": False,
            "deploymentPerformed": False,
            "realTradingAllowed": False,
        },
        "candidate": {
            "cell": CELL,
            "profile": PROFILE,
            "signal": SIGNAL,
            "zThreshold": Z_THRESHOLD,
        },
        "sampling": {
            "policy": "all 2024-2025 frozen candidate signals; no outcome-based date selection",
            "selectedSignals": int(len(selected)),
            "archiveDates": dates,
        },
        "archives": archives,
        "ticks": {
            "filteredAggTrades": int(len(ticks)),
            "start": ticks["time"].min(),
            "end": ticks["time"].max(),
        },
        "results": {
            "byDelay": by_delay,
            "commonCoverageSignals": int(len(common_keys)),
            "commonCoverageByDelay": common_by_delay,
            "byYearAndDelay": by_year_delay,
            "byMonthAndDelay": by_month_delay,
            "promotionStyleGatePassed": passed,
        },
        "decision": {
            "researchCandidate": passed,
            "deployment": "none",
            "realTradingAllowed": False,
        },
        "outputs": {
            "json": str(OUT_JSON),
            "ticks": str(OUT_TICKS),
            "trades": str(OUT_TRADES),
            "cache": str(CACHE),
        },
    }
    OUT_JSON.write_text(
        json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", default=str(OUT_CANDIDATES))
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    report = run(args.candidates, workers=args.workers)
    print(
        json.dumps(
            clean(
                {
                    "sampling": report["sampling"],
                    "ticks": report["ticks"],
                    "results": report["results"],
                    "decision": report["decision"],
                    "outputs": report["outputs"],
                }
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
