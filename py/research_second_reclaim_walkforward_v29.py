"""Chronological validation of a frozen five-second reclaim confirmation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research_actual_horizon_walkforward_v21 import load_candidates
from research_archived_aggtrades_v23 import (
    HORIZON_SEC,
    MAX_TICK_LAG_SEC,
    download_archives,
    load_filtered_ticks,
)
from research_directional_candidate_v22 import CELL, PROFILE, SIGNAL, Z_THRESHOLD
from research_long_history_walkforward_v20 import OUT_CANDIDATES
from research_minute_volatility_normal_v15 import BREAKEVEN_WR, clean
from research_multiregime_strategy_v16 import apply_shared_cooldown
from research_stationarity_router_v19 import _bootstrap_block_ev
from v14_validation import TickResolver, summarize_metrics


ROOT = Path(__file__).resolve().parents[1]
EARLY_CANDIDATES = ROOT / "tmp" / "v27_h1_direct_reversion_trades_20260730.csv"
PROTOCOL = ROOT / "docs" / "v29_second_reclaim_walkforward_protocol_20260730.md"
OUT_JSON = ROOT / "tmp" / "v29_second_reclaim_walkforward_20260730.json"
OUT_TICKS = ROOT / "tmp" / "v29_second_reclaim_ticks_20260730.csv"
OUT_TRADES = ROOT / "tmp" / "v29_second_reclaim_trades_20260730.csv"

PRIMARY_RULE = "reclaim5_le_0bp"
RULES = (
    ("reclaim5_le_m1bp", 5, -1.0, True),
    (PRIMARY_RULE, 5, 0.0, True),
    ("reclaim5_le_p1bp", 5, 1.0, True),
    ("reclaim10_le_0bp", 10, 0.0, False),
)
POST_CONFIRM_DELAYS = (0, 5, 10)
EXPECTED_EARLY = 201
EXPECTED_LATER = 97
EXPECTED_2024_2025 = 74
EXPECTED_2026 = 23
MIN_COMBINED_TEST = 60
CALENDAR_TEST_MONTHS = pd.period_range("2023-01", "2026-07", freq="M").strftime(
    "%Y-%m"
).tolist()


def load_signal_sets() -> tuple[pd.DataFrame, pd.DataFrame]:
    early = pd.read_csv(EARLY_CANDIDATES)
    early["signal_time"] = pd.to_datetime(early["signal_time"], utc=True)
    if len(early) != EXPECTED_EARLY:
        raise ValueError(f"V29 expected {EXPECTED_EARLY} early signals, got {len(early)}")
    candidates = load_candidates(OUT_CANDIDATES)
    later = apply_shared_cooldown(
        candidates.loc[
            candidates["cell"].eq(CELL)
            & candidates["profile"].eq(PROFILE)
            & candidates["signal"].eq(SIGNAL)
            & candidates["z"].ge(Z_THRESHOLD)
        ],
        cooldown_min=10,
    )
    later["signal_time"] = pd.to_datetime(later["signal_time"], utc=True)
    if len(later) != EXPECTED_LATER:
        raise ValueError(f"V29 expected {EXPECTED_LATER} later signals, got {len(later)}")
    before_2026 = later["signal_time"].lt(pd.Timestamp("2026-01-01T00:00:00Z"))
    if int(before_2026.sum()) != EXPECTED_2024_2025:
        raise ValueError("V29 2024-2025 frozen signal count changed")
    if int((~before_2026).sum()) != EXPECTED_2026:
        raise ValueError("V29 2026 frozen signal count changed")
    common_columns = ["signal_time", "signal", "profile", "cell", "z"]
    early = early[common_columns].copy()
    later = later[common_columns].copy()
    all_signals = pd.concat([early, later], ignore_index=True).sort_values(
        "signal_time", kind="stable"
    ).reset_index(drop=True)
    all_signals["candidate_id"] = [f"h1_{index:04d}" for index in range(len(all_signals))]
    all_signals["period"] = np.select(
        [
            all_signals["signal_time"].lt(pd.Timestamp("2023-01-01T00:00:00Z")),
            all_signals["signal_time"].lt(pd.Timestamp("2024-01-01T00:00:00Z")),
            all_signals["signal_time"].lt(pd.Timestamp("2026-01-01T00:00:00Z")),
        ],
        ["development_2020_2022", "test_2023", "reused_2024_2025"],
        default="reused_2026",
    )
    return all_signals, later.loc[~before_2026].copy()


def needed_ranges(signals: pd.DataFrame) -> dict[str, list[tuple[int, int]]]:
    ranges: dict[str, list[tuple[int, int]]] = {}
    for signal_time in pd.to_datetime(signals["signal_time"], utc=True):
        targets = [
            *((signal_time + pd.Timedelta(seconds=value), 5) for value in (0, 5, 10, 15)),
            *((signal_time + pd.Timedelta(seconds=value), 10) for value in (600, 605, 610, 615)),
        ]
        for target, extra_sec in targets:
            date_text = target.strftime("%Y-%m-%d")
            start_ms = int(target.timestamp() * 1000)
            end_ms = int((target + pd.Timedelta(seconds=extra_sec)).timestamp() * 1000)
            ranges.setdefault(date_text, []).append((start_ms, end_ms))
    return ranges


def load_all_ticks(signals: pd.DataFrame, workers: int) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    ranges = needed_ranges(signals)
    archives = download_archives(sorted(ranges), workers=workers)
    ticks = load_filtered_ticks(archives, ranges)
    ticks["time"] = pd.to_datetime(ticks["time"], utc=True, errors="coerce")
    ticks["price"] = pd.to_numeric(ticks["price"], errors="coerce")
    ticks["agg_trade_id"] = pd.to_numeric(ticks["agg_trade_id"], errors="coerce")
    ticks = ticks.dropna(subset=["time", "price", "agg_trade_id"])
    ticks = ticks.loc[np.isfinite(ticks["price"]) & ticks["price"].gt(0.0)]
    ticks = ticks.sort_values(["time", "agg_trade_id"], kind="stable").drop_duplicates(
        ["time", "agg_trade_id"], keep="last"
    ).reset_index(drop=True)
    return ticks, archives


def _resolve_tick(
    resolver: TickResolver, target: pd.Timestamp
) -> dict[str, Any] | None:
    return resolver.first_at_or_after(target, max_lag_sec=MAX_TICK_LAG_SEC)


def build_reclaim_trades(signals: pd.DataFrame, ticks: pd.DataFrame) -> pd.DataFrame:
    canonical_ticks = pd.DataFrame(
        {
            "time": pd.to_datetime(ticks["time"], utc=True).to_numpy(),
            "price": pd.to_numeric(ticks["price"], errors="coerce").to_numpy(),
        }
    ).sort_values("time", kind="stable").reset_index(drop=True)
    resolver = TickResolver(canonical_ticks)
    rows: list[dict[str, Any]] = []
    for candidate in signals.to_dict("records"):
        signal_time = pd.Timestamp(candidate["signal_time"])
        p0 = _resolve_tick(resolver, signal_time)
        if p0 is None:
            continue
        fixed_target = signal_time + pd.Timedelta(seconds=HORIZON_SEC)
        fixed_tick = _resolve_tick(resolver, fixed_target)
        for rule_name, confirm_sec, threshold_bps, full_delay_grid in RULES:
            confirm_target = signal_time + pd.Timedelta(seconds=confirm_sec)
            confirm_tick = _resolve_tick(resolver, confirm_target)
            if confirm_tick is None:
                continue
            confirm_bps = (confirm_tick["price"] / p0["price"] - 1.0) * 10_000.0
            confirmed = bool(confirm_bps <= threshold_bps)
            post_delays = POST_CONFIRM_DELAYS if full_delay_grid else (0,)
            for post_delay in post_delays:
                entry_target = signal_time + pd.Timedelta(
                    seconds=confirm_sec + int(post_delay)
                )
                entry = _resolve_tick(resolver, entry_target)
                for settlement_mode in ("entry_plus_600s", "fixed_boundary"):
                    base = {
                        "candidate_id": candidate["candidate_id"],
                        "signal_time": signal_time,
                        "period": candidate["period"],
                        "year": signal_time.year,
                        "rule": rule_name,
                        "confirm_sec": confirm_sec,
                        "threshold_bps": threshold_bps,
                        "confirm_bps": float(confirm_bps),
                        "confirmed": confirmed,
                        "post_confirm_delay_sec": int(post_delay),
                        "total_delay_sec": int(confirm_sec + post_delay),
                        "settlement_mode": settlement_mode,
                        "entry_target_time": entry_target,
                    }
                    if not confirmed:
                        rows.append(
                            {
                                **base,
                                "status": "rejected",
                                "entry_time": pd.NaT,
                                "entry_price": np.nan,
                                "settle_target_time": pd.NaT,
                                "settle_time": pd.NaT,
                                "settle_price": np.nan,
                                "signed_bps": np.nan,
                                "pnl_u": np.nan,
                            }
                        )
                        continue
                    if entry is None:
                        rows.append(
                            {
                                **base,
                                "status": "missing_entry",
                                "entry_time": pd.NaT,
                                "entry_price": np.nan,
                                "settle_target_time": pd.NaT,
                                "settle_time": pd.NaT,
                                "settle_price": np.nan,
                                "signed_bps": np.nan,
                                "pnl_u": np.nan,
                            }
                        )
                        continue
                    if settlement_mode == "entry_plus_600s":
                        settle_target = entry["time"] + pd.Timedelta(seconds=HORIZON_SEC)
                        settle = _resolve_tick(resolver, settle_target)
                    else:
                        settle_target = fixed_target
                        settle = fixed_tick
                    if settle is None:
                        rows.append(
                            {
                                **base,
                                "status": "missing_settlement",
                                "entry_time": entry["time"],
                                "entry_price": entry["price"],
                                "settle_target_time": settle_target,
                                "settle_time": pd.NaT,
                                "settle_price": np.nan,
                                "signed_bps": np.nan,
                                "pnl_u": np.nan,
                            }
                        )
                        continue
                    signed = (settle["price"] / entry["price"] - 1.0) * -10_000.0
                    status = "won" if signed > 0.0 else "lost" if signed < 0.0 else "tie"
                    pnl = 4.0 if signed > 0.0 else -5.0 if signed < 0.0 else 0.0
                    rows.append(
                        {
                            **base,
                            "status": status,
                            "entry_time": entry["time"],
                            "entry_price": entry["price"],
                            "settle_target_time": settle_target,
                            "settle_time": settle["time"],
                            "settle_price": settle["price"],
                            "signed_bps": float(signed),
                            "pnl_u": pnl,
                        }
                    )
    return pd.DataFrame(rows).sort_values(
        ["signal_time", "rule", "post_confirm_delay_sec", "settlement_mode"],
        kind="stable",
    ).reset_index(drop=True)


def common_settled(frame: pd.DataFrame, *, full_grid: bool) -> pd.DataFrame:
    expected = 2 * (len(POST_CONFIRM_DELAYS) if full_grid else 1)
    settled = frame.loc[frame["status"].isin(("won", "lost", "tie"))].copy()
    counts = settled.groupby("candidate_id")[["settlement_mode", "post_confirm_delay_sec"]].apply(
        lambda group: len(set(zip(group["settlement_mode"], group["post_confirm_delay_sec"])))
    )
    keys = set(counts.loc[counts.eq(expected)].index)
    return settled.loc[settled["candidate_id"].isin(keys)].copy()


def summarize_rule(frame: pd.DataFrame, rule: str, period: str) -> dict[str, Any]:
    part = frame.loc[frame["rule"].eq(rule)]
    full_grid = rule != "reclaim10_le_0bp"
    common = common_settled(part, full_grid=full_grid)
    requested = int(part["candidate_id"].nunique()) if not part.empty else 0
    confirmed = int(part.loc[part["confirmed"]].candidate_id.nunique()) if not part.empty else 0
    common_count = int(common["candidate_id"].nunique()) if not common.empty else 0
    slices: dict[str, Any] = {}
    for (mode, delay), group in common.groupby(
        ["settlement_mode", "post_confirm_delay_sec"], sort=True
    ):
        metrics = summarize_metrics(group)
        bootstrap = _bootstrap_block_ev(
            group, "pnl_u", seed_key=f"V29|{rule}|{period}|{mode}|{int(delay)}"
        )
        yearly = (
            pd.to_numeric(group["pnl_u"], errors="coerce")
            .fillna(0.0)
            .groupby(pd.to_datetime(group["signal_time"], utc=True).dt.year)
            .sum()
        )
        slices[f"{mode}|{int(delay)}"] = {
            **metrics,
            "yearlyPnlU": {str(key): float(value) for key, value in yearly.items()},
            "bootstrap": bootstrap,
        }
    return {
        "period": period,
        "requestedSignals": requested,
        "confirmedSignals": confirmed,
        "confirmationRatePct": round(100.0 * confirmed / requested, 4) if requested else None,
        "commonCoverageSignals": common_count,
        "slices": slices,
    }


def _all_slices_positive(report: dict[str, Any]) -> bool:
    return bool(
        report["slices"]
        and all(value["pnlU"] > 0.0 for value in report["slices"].values())
    )


def promotion_gate(test_2023: dict[str, Any], reused: dict[str, Any], combined: dict[str, Any]) -> bool:
    if not _all_slices_positive(test_2023) or not _all_slices_positive(reused):
        return False
    if combined["commonCoverageSignals"] < MIN_COMBINED_TEST or len(combined["slices"]) != 6:
        return False
    for row in combined["slices"].values():
        yearly = row["yearlyPnlU"]
        if not (
            row["winRatePct"] is not None
            and row["winRatePct"] > BREAKEVEN_WR
            and row["pnlU"] > 0.0
            and row["wilson95LowerPct"] is not None
            and row["wilson95LowerPct"] > BREAKEVEN_WR
            and sum(value > 0.0 for value in yearly.values()) >= 3
            and row["bootstrap"]["lower90EvU"] is not None
            and row["bootstrap"]["lower90EvU"] > 0.0
            and row["maxDrawdownU"] <= 25.0
            and row["maxLossStreak"] <= 3
        ):
            return False
    return True


def run(workers: int) -> dict[str, Any]:
    signals, _signals_2026 = load_signal_sets()
    ticks, archives = load_all_ticks(signals, workers)
    ticks.to_csv(OUT_TICKS, index=False, encoding="utf-8-sig")
    trades = build_reclaim_trades(signals, ticks)
    trades.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    period_masks = {
        "development_2020_2022": trades["period"].eq("development_2020_2022"),
        "test_2023": trades["period"].eq("test_2023"),
        "reused_2024_2025": trades["period"].eq("reused_2024_2025"),
        "reused_2026": trades["period"].eq("reused_2026"),
        "combined_test_2023_2026": trades["period"].ne("development_2020_2022"),
    }
    results: dict[str, Any] = {}
    for rule, _, _, _ in RULES:
        results[rule] = {
            period: summarize_rule(trades.loc[mask], rule, period)
            for period, mask in period_masks.items()
        }
    primary = results[PRIMARY_RULE]
    passed = promotion_gate(
        primary["test_2023"],
        primary["reused_2024_2025"],
        primary["combined_test_2023_2026"],
    )
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "status": "V29_FROZEN_SECOND_RECLAIM_WALKFORWARD",
        "safety": {
            "researchOnly": True,
            "tradeEnabled": False,
            "deploymentPerformed": False,
            "onlineConfigurationChanged": False,
            "realTradingAllowed": False,
        },
        "protocol": {
            "file": str(PROTOCOL.resolve()),
            "primaryRule": PRIMARY_RULE,
            "rules": [
                {
                    "name": name,
                    "confirmSec": confirm_sec,
                    "thresholdBps": threshold,
                    "fullDelayGrid": full_grid,
                }
                for name, confirm_sec, threshold, full_grid in RULES
            ],
            "postConfirmDelaysSec": list(POST_CONFIRM_DELAYS),
        },
        "signals": {
            "total": int(len(signals)),
            "byPeriod": {
                str(key): int(value)
                for key, value in signals["period"].value_counts().items()
            },
        },
        "ticks": {
            "rows": int(len(ticks)),
            "start": ticks["time"].min(),
            "end": ticks["time"].max(),
            "archives": archives,
        },
        "results": results,
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
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    report = run(args.workers)
    print(
        json.dumps(
            clean(
                {
                    "signals": report["signals"],
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
