"""Validate frozen V27 H1 signals on official archived aggregate trades."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pull_binance_futures_monthly_klines import sha256_file
from research_archived_aggtrades_v23 import (
    DELAYS_SEC,
    HORIZON_SEC,
    MAX_TICK_LAG_SEC,
    _needed_ranges,
    download_archives,
    load_filtered_ticks,
)
from research_minute_volatility_normal_v15 import BREAKEVEN_WR, clean
from research_stationarity_router_v19 import _bootstrap_block_ev
from v14_validation import (
    TickResolver,
    _candidate_key,
    apply_family_cooldown,
    metrics_by_delay,
    normalize_candidates,
    normalize_futures_ticks,
    resolve_candidate_trades,
    summarize_metrics,
)


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "tmp" / "v27_h1_direct_reversion_trades_20260730.csv"
INPUT_SHA256 = "d3123884e02b743b24a81f61e23f130025bfb1c04ec2975714d2d99f2c56104a"
OUT_JSON = ROOT / "tmp" / "v28_frozen_second_execution_20260730.json"
OUT_TICKS = ROOT / "tmp" / "v28_frozen_second_execution_ticks_20260730.csv"
OUT_TRADES = ROOT / "tmp" / "v28_frozen_second_execution_trades_20260730.csv"
PROTOCOL = ROOT / "docs" / "v28_frozen_second_execution_protocol_20260730.md"
CALENDAR_MONTHS = pd.period_range("2020-01", "2023-12", freq="M").strftime(
    "%Y-%m"
).tolist()
YEARS = (2020, 2021, 2022, 2023)
EXPECTED_SIGNALS = 201
MIN_COMMON_COVERAGE = 190


def load_frozen_candidates(path: str | Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    observed = sha256_file(path)
    if observed != INPUT_SHA256:
        raise ValueError(
            f"V28 candidate SHA-256 mismatch: {observed} != {INPUT_SHA256}"
        )
    source = pd.read_csv(path)
    if len(source) != EXPECTED_SIGNALS:
        raise ValueError(f"V28 expected {EXPECTED_SIGNALS} signals, got {len(source)}")
    required = {"signal_time", "signal", "profile", "cell"}
    if not required.issubset(source.columns):
        raise ValueError(f"V28 candidate columns missing: {sorted(required - set(source.columns))}")
    normalized = normalize_candidates(
        source.rename(columns={"signal_time": "time"}),
        time_col="time",
        signal_col="signal",
        family_col="profile",
        branch_col="cell",
    )
    cooled = apply_family_cooldown(normalized, cooldown_sec=HORIZON_SEC)
    if len(cooled) != EXPECTED_SIGNALS:
        raise ValueError("V28 frozen cooldown changed the candidate set")
    return source, cooled


def resolve_fixed_boundary_trades(
    candidates: pd.DataFrame,
    ticks: pd.DataFrame,
    *,
    delays_sec: tuple[int, ...] = DELAYS_SEC,
    max_tick_lag_sec: float = MAX_TICK_LAG_SEC,
) -> pd.DataFrame:
    resolver = TickResolver(ticks)
    rows: list[dict[str, Any]] = []
    for candidate in candidates.to_dict("records"):
        signal_time = pd.Timestamp(candidate["time"])
        direction = 1.0 if candidate["signal"] == "UP" else -1.0
        candidate_key = _candidate_key(candidate)
        settle_target = signal_time + pd.Timedelta(seconds=HORIZON_SEC)
        settle = resolver.first_at_or_after(
            settle_target, max_lag_sec=max_tick_lag_sec
        )
        for delay in delays_sec:
            entry_target = signal_time + pd.Timedelta(seconds=int(delay))
            entry = resolver.first_at_or_after(
                entry_target, max_lag_sec=max_tick_lag_sec
            )
            base = {
                "trade_key": f"{candidate_key}|fixed|d{delay}",
                "candidate_key": candidate_key,
                "family": candidate["family"],
                "signal": candidate["signal"],
                "branch": candidate["branch"],
                "signal_time": signal_time,
                "delay_sec": int(delay),
                "settlement_mode": "fixed_boundary",
                "horizon_sec": HORIZON_SEC,
                "entry_target_time": entry_target,
                "settle_target_time": settle_target,
            }
            if entry is None or settle is None:
                rows.append(
                    {
                        **base,
                        "status": "missing_entry" if entry is None else "missing_settlement",
                        "entry_time": pd.NaT if entry is None else entry["time"],
                        "entry_price": np.nan if entry is None else entry["price"],
                        "entry_lag_sec": np.nan if entry is None else entry["lag_sec"],
                        "settle_time": pd.NaT if settle is None else settle["time"],
                        "settle_price": np.nan if settle is None else settle["price"],
                        "settle_lag_sec": np.nan if settle is None else settle["lag_sec"],
                        "signed_bps": np.nan,
                        "pnl_u": np.nan,
                    }
                )
                continue
            signed = (
                settle["price"] / entry["price"] - 1.0
            ) * 10_000.0 * direction
            status = "won" if signed > 0.0 else "lost" if signed < 0.0 else "tie"
            pnl = 4.0 if signed > 0.0 else -5.0 if signed < 0.0 else 0.0
            rows.append(
                {
                    **base,
                    "status": status,
                    "entry_time": entry["time"],
                    "entry_price": entry["price"],
                    "entry_lag_sec": entry["lag_sec"],
                    "settle_time": settle["time"],
                    "settle_price": settle["price"],
                    "settle_lag_sec": settle["lag_sec"],
                    "signed_bps": float(signed),
                    "pnl_u": pnl,
                }
            )
    return pd.DataFrame(rows).sort_values(
        ["entry_target_time", "delay_sec"], kind="stable"
    ).reset_index(drop=True)


def _common_candidate_keys(trades: pd.DataFrame) -> set[str]:
    settled = trades.loc[trades["status"].isin(("won", "lost", "tie"))]
    coverage = settled.groupby("candidate_key")[["settlement_mode", "delay_sec"]].apply(
        lambda group: len(set(zip(group["settlement_mode"], group["delay_sec"])))
    )
    return set(coverage.loc[coverage.eq(2 * len(DELAYS_SEC))].index)


def _slice_diagnostics(frame: pd.DataFrame) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for (mode, delay), group in frame.groupby(
        ["settlement_mode", "delay_sec"], sort=True
    ):
        metrics = summarize_metrics(group)
        signal_time = pd.to_datetime(group["signal_time"], utc=True)
        pnl = pd.to_numeric(group["pnl_u"], errors="coerce").fillna(0.0)
        month = signal_time.dt.strftime("%Y-%m")
        year = signal_time.dt.year
        monthly = pnl.groupby(month).sum().reindex(CALENDAR_MONTHS, fill_value=0.0)
        yearly = pnl.groupby(year).sum().reindex(YEARS, fill_value=0.0)
        bootstrap = _bootstrap_block_ev(
            group,
            "pnl_u",
            seed_key=f"V28|{mode}|{int(delay)}",
        )
        positive_month_pct = float(monthly.gt(0.0).mean()) * 100.0
        passed = bool(
            metrics["settled"] >= MIN_COMMON_COVERAGE
            and metrics["winRatePct"] is not None
            and metrics["winRatePct"] > BREAKEVEN_WR
            and metrics["pnlU"] > 0.0
            and metrics["wilson95LowerPct"] is not None
            and metrics["wilson95LowerPct"] > BREAKEVEN_WR
            and yearly.ge(0.0).all()
            and int(yearly.gt(0.0).sum()) >= 3
            and positive_month_pct >= 60.0
            and bootstrap["lower90EvU"] is not None
            and bootstrap["lower90EvU"] > 0.0
            and metrics["maxDrawdownU"] <= 30.0
            and metrics["maxLossStreak"] <= 3
        )
        output[f"{mode}|{int(delay)}"] = {
            **metrics,
            "positiveMonthPct": round(positive_month_pct, 4),
            "worstMonthPnlU": round(float(monthly.min()), 4),
            "monthlyPnlU": {str(key): float(value) for key, value in monthly.items()},
            "yearlyPnlU": {str(key): float(value) for key, value in yearly.items()},
            "bootstrap": bootstrap,
            "passed": passed,
        }
    return output


def run(candidate_path: str | Path, workers: int) -> dict[str, Any]:
    source, candidates = load_frozen_candidates(candidate_path)
    ranges = _needed_ranges(source)
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
    full = resolve_candidate_trades(
        candidates,
        normalized_ticks,
        delays_sec=DELAYS_SEC,
        execution_base_lag_sec=0,
        horizon_sec=HORIZON_SEC,
        amount_u=5.0,
        payout_rate=0.8,
        max_tick_lag_sec=MAX_TICK_LAG_SEC,
    )
    full["settlement_mode"] = "entry_plus_600s"
    fixed = resolve_fixed_boundary_trades(candidates, normalized_ticks)
    all_trades = pd.concat([full, fixed], ignore_index=True)
    common_keys = _common_candidate_keys(all_trades)
    common = all_trades.loc[all_trades["candidate_key"].isin(common_keys)].copy()
    common.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    diagnostics = _slice_diagnostics(common)
    passed = bool(
        len(common_keys) >= MIN_COMMON_COVERAGE
        and len(diagnostics) == 2 * len(DELAYS_SEC)
        and all(row["passed"] for row in diagnostics.values())
    )
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "status": "V28_FROZEN_SECOND_EXECUTION_VALIDATION",
        "safety": {
            "researchOnly": True,
            "tradeEnabled": False,
            "deploymentPerformed": False,
            "onlineConfigurationChanged": False,
            "realTradingAllowed": False,
        },
        "protocol": {
            "file": str(PROTOCOL.resolve()),
            "candidateSha256": sha256_file(candidate_path),
            "expectedSignals": EXPECTED_SIGNALS,
            "delaysSec": list(DELAYS_SEC),
            "settlementModes": ["entry_plus_600s", "fixed_boundary"],
        },
        "sampling": {
            "signals": int(len(candidates)),
            "archiveDates": int(len(dates)),
            "commonCoverageSignals": int(len(common_keys)),
        },
        "archives": archives,
        "ticks": {
            "filteredAggTrades": int(len(ticks)),
            "start": ticks["time"].min(),
            "end": ticks["time"].max(),
        },
        "results": diagnostics,
        "decision": {
            "passed": passed,
            "action": "historical_candidate" if passed else "no_trade",
            "deployment": "none",
            "realTradingAllowed": False,
        },
        "outputs": {
            "json": str(OUT_JSON.resolve()),
            "ticks": str(OUT_TICKS.resolve()),
            "trades": str(OUT_TRADES.resolve()),
        },
    }
    OUT_JSON.write_text(
        json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", default=str(INPUT))
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    report = run(args.candidates, args.workers)
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
