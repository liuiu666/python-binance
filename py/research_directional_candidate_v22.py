"""Freeze and second-validate the V22 DOWN-only directional candidate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from research_actual_horizon_walkforward_v21 import load_candidates
from research_minute_volatility_normal_v15 import clean
from research_stationarity_router_v19 import _bootstrap_block_ev, fixed_metrics
from research_v17_second_execution_validation import (
    DELAYS_SEC,
    INVENTORY,
    _evaluate_price_mode,
    _target_seconds,
    load_needed_second_rows,
    select_daily_sources,
)
from research_long_history_walkforward_v20 import OUT_CANDIDATES
from research_multiregime_strategy_v16 import apply_shared_cooldown, metrics


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "tmp" / "v22_directional_candidate_20260730.json"
OUT_TRADES = ROOT / "tmp" / "v22_directional_candidate_second_trades_20260730.csv"

CELL = "mid|revertible"
PROFILE = "v19_edge_w60_z2p0"
SIGNAL = "DOWN"
Z_THRESHOLD = 2.5


def minute_period_summary(frame: pd.DataFrame, label: str) -> dict[str, Any]:
    exact = metrics(frame, 10, 0)
    shifted = metrics(frame, 10, 1)
    fixed = fixed_metrics(frame)
    month = pd.to_datetime(frame["signal_time"], utc=True).dt.strftime("%Y-%m")
    monthly = {}
    for month_name, group in frame.groupby(month, sort=True):
        monthly[str(month_name)] = {
            "trades": int(len(group)),
            "exactPnlU": metrics(group, 10, 0)["pnlU"],
            "shiftedPnlU": metrics(group, 10, 1)["pnlU"],
            "fixedPnlU": fixed_metrics(group)["pnlU"],
        }
    return {
        "label": label,
        "exact": exact,
        "shiftedOneMinute": shifted,
        "fixedSettlementStress": fixed,
        "exactBootstrap": _bootstrap_block_ev(
            frame, "pnl_u_h10_d0", seed_key=f"{label}|exact"
        ),
        "shiftedBootstrap": _bootstrap_block_ev(
            frame, "pnl_u_h10_d1", seed_key=f"{label}|shifted"
        ),
        "fixedBootstrap": _bootstrap_block_ev(
            frame, "pnl_u_h10_fixed_d1", seed_key=f"{label}|fixed"
        ),
        "monthly": monthly,
    }


def run(candidate_path: str | Path, inventory_path: str | Path) -> dict[str, Any]:
    candidates = load_candidates(candidate_path)
    frozen = apply_shared_cooldown(
        candidates.loc[
            candidates["cell"].eq(CELL)
            & candidates["profile"].eq(PROFILE)
            & candidates["signal"].eq(SIGNAL)
            & candidates["z"].ge(Z_THRESHOLD)
        ]
    )
    if frozen.empty:
        raise ValueError("frozen V22 candidate has no signals")
    reverse = frozen.loc[frozen["signal_time"].lt(pd.Timestamp("2026-01-01T00:00:00Z"))]
    reused = frozen.loc[frozen["signal_time"].ge(pd.Timestamp("2026-01-01T00:00:00Z"))]
    combined = minute_period_summary(frozen, "2024-2026_combined")

    needed = _target_seconds(frozen["signal_time"])
    sources, source_audit = select_daily_sources(
        inventory_path,
        {timestamp.strftime("%Y-%m-%d") for timestamp in needed},
    )
    second_rows, tick_audit = load_needed_second_rows(sources, needed)
    if second_rows.empty:
        raise ValueError("no available second rows overlap the V22 candidate")
    mode_reports: dict[str, Any] = {}
    all_trades = []
    for price_column in ("open", "close"):
        trades, mode_report = _evaluate_price_mode(
            frozen, second_rows, price_column
        )
        if not trades.empty:
            tagged = trades.copy()
            tagged["price_mode"] = price_column
            all_trades.append(tagged)
        mode_reports[price_column] = mode_report
    output_trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    output_trades.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")

    common_counts = [
        int(mode_reports[mode]["commonCoverageSignals"])
        for mode in ("open", "close")
    ]
    delay_rows = [
        mode_reports[mode]["commonCoverageByDelay"].get(str(delay))
        for mode in ("open", "close")
        for delay in DELAYS_SEC
    ]
    seconds_passed = bool(
        min(common_counts, default=0) >= 20
        and all(row is not None for row in delay_rows)
        and all(
            row["settled"] >= 20
            and row["winRatePct"] is not None
            and row["winRatePct"] >= 63.0
            and row["pnlU"] > 0.0
            and row["maxDrawdownU"] <= 20.0
            and row["maxLossStreak"] <= 2
            for row in delay_rows
            if row is not None
        )
    )
    minute_platform_passed = bool(
        combined["exact"]["wilson95LowerPct"] is not None
        and combined["exact"]["wilson95LowerPct"] > 55.5556
        and combined["exactBootstrap"]["lower90EvU"] is not None
        and combined["exactBootstrap"]["lower90EvU"] > 0.0
        and combined["shiftedOneMinute"]["pnlU"] > 0.0
        and combined["shiftedBootstrap"]["lower90EvU"] is not None
        and combined["shiftedBootstrap"]["lower90EvU"] > 0.0
    )
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "status": "V22_DIRECTIONAL_CANDIDATE_SECOND_VALIDATION",
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
            "totalSignals": int(len(frozen)),
            "warning": "Direction was isolated after long-history inspection; this is retrospective, not sealed holdout.",
        },
        "minuteEvidence": {
            "reverse2024To2025": minute_period_summary(reverse, "2024-2025"),
            "reused2026": minute_period_summary(reused, "2026_reused"),
            "combined": combined,
            "platformPassed": minute_platform_passed,
        },
        "secondEvidence": {
            "sources": source_audit,
            "ticks": tick_audit,
            "results": mode_reports,
            "promotionStyleGatePassed": seconds_passed,
        },
        "decision": {
            "researchCandidate": minute_platform_passed and seconds_passed,
            "deployment": "none",
            "realTradingAllowed": False,
        },
        "outputs": {"json": str(OUT_JSON), "trades": str(OUT_TRADES)},
    }
    OUT_JSON.write_text(
        json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", default=str(OUT_CANDIDATES))
    parser.add_argument("--inventory", default=str(INVENTORY))
    args = parser.parse_args()
    report = run(args.candidates, args.inventory)
    print(
        json.dumps(
            clean(
                {
                    "candidate": report["candidate"],
                    "minuteEvidence": report["minuteEvidence"],
                    "secondEvidence": report["secondEvidence"],
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
