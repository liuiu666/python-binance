"""Run the complete leakage-resistant V14 second-reversal stability study.

This is an offline research entry point.  It does not import deployment code,
write production configuration, or expose any real-trading switch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from current_v2_augmented_v9_core import AugmentedV9Rules  # noqa: E402
from research_v9_all_history_stability import metric as v13_metric  # noqa: E402
from research_v9_all_history_stability import replay_dataset as replay_v13_dataset  # noqa: E402
from v14_candidates import (  # noqa: E402
    CANDIDATE_NAMES,
    V14CandidateRules,
    build_core_features,
    build_robust_features,
    candidate_metadata,
    generate_candidates_from_features,
    parameter_neighborhood,
)
from v14_dataset import PairedSource, discover_paired_sources, load_canonical_dataset  # noqa: E402
from v14_research_safety import shadow_only_candidate_metadata  # noqa: E402
from v14_validation import (  # noqa: E402
    DEFAULT_DELAYS_SEC,
    metrics_by_block,
    run_validation,
    summarize_metrics,
)


RUN_ID = "20260729"
OUT_REPORT = ROOT / "tmp" / f"v14_stability_research_{RUN_ID}.json"
OUT_TRADES = ROOT / "tmp" / f"v14_stability_research_{RUN_ID}_trades.csv"
OUT_CANDIDATES = ROOT / "tmp" / f"v14_stability_research_{RUN_ID}_candidates.csv"
OUT_NEIGHBORHOOD = ROOT / "tmp" / f"v14_stability_research_{RUN_ID}_neighborhood.csv"
OUT_SOURCE_MANIFEST = ROOT / "tmp" / f"v14_stability_research_{RUN_ID}_sources.json"

DELAYS_SEC = tuple(DEFAULT_DELAYS_SEC)
HORIZON_SEC = 600
EXECUTION_BASE_LAG_SEC = 1
FAMILY_COOLDOWN_SEC = 600
MAX_TICK_LAG_SEC = 2.0
FEATURE_WARMUP_SEC = 3600
MAX_INTERNAL_GAP_SEC = 300
AMOUNT_U = 5.0
PAYOUT_RATE = 0.8
BREAKEVEN_WIN_RATE_PCT = 100.0 / (1.0 + PAYOUT_RATE)


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if value is pd.NaT or (not isinstance(value, (str, bool)) and pd.isna(value)):
        return None
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_frozen_sources() -> tuple[list[PairedSource], dict[str, Any]]:
    """Freeze all real paired pulls available now, excluding pytest artifacts."""

    discovered = discover_paired_sources([ROOT / "data", ROOT / "tmp"])
    real = []
    for source in discovered:
        trade_parts = [part.lower() for part in source.trades.parts]
        book_parts = [part.lower() for part in source.orderbook.parts]
        if any(part.startswith("pytest") for part in (*trade_parts, *book_parts)):
            continue
        real.append(source)
    rows: list[dict[str, Any]] = []
    unique: list[PairedSource] = []
    seen_pairs: dict[tuple[str, str], str] = {}
    for source in real:
        trade_sha = _sha256(source.trades)
        book_sha = _sha256(source.orderbook)
        key = (trade_sha, book_sha)
        duplicate_of = seen_pairs.get(key)
        row = {
            "name": source.name,
            "trades": str(source.trades),
            "orderbook": str(source.orderbook),
            "tradesBytes": source.trades.stat().st_size,
            "orderbookBytes": source.orderbook.stat().st_size,
            "tradesSha256": trade_sha,
            "orderbookSha256": book_sha,
            "exactDuplicateOf": duplicate_of,
        }
        rows.append(row)
        if duplicate_of is not None:
            continue
        seen_pairs[key] = source.name
        unique.append(PairedSource(
            name=source.name,
            trades=source.trades,
            orderbook=source.orderbook,
            priority=len(unique),
        ))
    manifest = {
        "policy": "all paired data/server and tmp snapshots available at run start; pytest artifacts excluded; exact file-pair duplicates loaded once",
        "discoveredRealPairs": len(real),
        "uniqueFilePairsLoaded": len(unique),
        "exactDuplicatePairsSkipped": len(real) - len(unique),
        "sources": rows,
    }
    return unique, manifest


def _validate_futures_market(frame: pd.DataFrame) -> None:
    market_columns = [column for column in ("market", "market_book") if column in frame.columns]
    if not market_columns:
        raise ValueError("canonical data has no market provenance column")
    for column in market_columns:
        values = {
            str(value).strip().lower()
            for value in frame[column].dropna().unique()
            if str(value).strip()
        }
        if values and not values.issubset({"futures", "future", "fapi", "um"}):
            raise ValueError(f"non-futures data in {column}: {sorted(values)}")


def densify_causal_segments(
    canonical: pd.DataFrame,
    *,
    max_internal_gap_sec: int = MAX_INTERNAL_GAP_SEC,
) -> tuple[list[pd.DataFrame], list[dict[str, Any]]]:
    """Densify short gaps causally; long outages start a fresh feature segment."""

    if canonical.empty:
        return [], []
    _validate_futures_market(canonical)
    ordered = canonical.sort_index().copy()
    steps = ordered.index.to_series().diff().dt.total_seconds()
    segment_ids = steps.gt(max_internal_gap_sec + 1).cumsum().astype(int)
    segments: list[pd.DataFrame] = []
    audits: list[dict[str, Any]] = []
    book_columns = [
        column
        for column in (
            "mid", "spread_bps", "bid_qty_20", "ask_qty_20", "imbalance_5",
            "imbalance_20", "microprice_edge_bps", "bid_wall_qty", "ask_wall_qty",
        )
        if column in ordered.columns
    ]
    for segment_id, raw in ordered.groupby(segment_ids, sort=True):
        full_index = pd.date_range(raw.index[0], raw.index[-1], freq="s", tz="UTC")
        dense = raw.reindex(full_index)
        exact = dense["close"].notna()
        dense["observed"] = exact.astype(bool)
        dense["close"] = pd.to_numeric(dense["close"], errors="coerce").ffill()
        for column in ("high", "low"):
            dense[column] = pd.to_numeric(dense.get(column), errors="coerce").fillna(dense["close"])
        for column in ("volume", "buy_qty", "sell_qty"):
            dense[column] = pd.to_numeric(dense.get(column), errors="coerce").fillna(0.0)

        exact_time = pd.Series(pd.NaT, index=full_index, dtype="datetime64[ns, UTC]")
        exact_time.loc[exact] = full_index[exact]
        last_exact = exact_time.ffill()
        dense["ob_age_sec"] = (pd.Series(full_index, index=full_index) - last_exact).dt.total_seconds()
        for column in book_columns:
            dense[column] = pd.to_numeric(dense[column], errors="coerce").ffill(limit=3)
        dense["ob_available"] = (
            dense["mid"].notna()
            & dense["ob_age_sec"].notna()
            & dense["ob_age_sec"].le(3.0)
        )
        dense["source"] = dense["source"].ffill().bfill()
        dense["research_segment"] = int(segment_id)
        dense.index.name = "time"
        segments.append(dense)
        audits.append({
            "segment": int(segment_id),
            "start": full_index[0],
            "end": full_index[-1],
            "denseRows": len(dense),
            "exactPairedRows": int(exact.sum()),
            "filledMissingRows": int((~exact).sum()),
            "observedPct": round(float(exact.mean()) * 100.0, 6),
            "eligibleAfterWarmup": max(0, len(dense) - FEATURE_WARMUP_SEC),
        })
    return segments, audits


def _candidate_variants() -> dict[str, list[tuple[str, V14CandidateRules]]]:
    rules = V14CandidateRules()
    return {
        candidate: parameter_neighborhood(rules, candidate=candidate)
        for candidate in CANDIDATE_NAMES
    }


def generate_all_candidates(
    segments: list[pd.DataFrame],
) -> tuple[dict[tuple[str, str], pd.DataFrame], list[dict[str, Any]]]:
    variants = _candidate_variants()
    buckets: dict[tuple[str, str], list[pd.DataFrame]] = defaultdict(list)
    block_audit: list[dict[str, Any]] = []
    for number, data in enumerate(segments):
        start = pd.Timestamp(data.index[0])
        eligible_start = start + pd.Timedelta(seconds=FEATURE_WARMUP_SEC)
        if data.index[-1] < eligible_start:
            block_audit.append({
                "segment": number,
                "start": start,
                "end": data.index[-1],
                "rows": len(data),
                "skipped": "shorter_than_feature_warmup",
            })
            continue
        core_features = build_core_features(data)
        robust_features = build_robust_features(data)
        counts: dict[str, int] = {}
        for candidate in CANDIDATE_NAMES:
            features = robust_features if candidate == "V14-Robust" else core_features
            for label, rules in variants[candidate]:
                frame = generate_candidates_from_features(features, candidate, rules)
                frame = frame.loc[frame["time"].ge(eligible_start)].copy()
                if frame.empty:
                    continue
                frame["block"] = f"segment_{number:03d}"
                source_map = data["source"].reindex(pd.DatetimeIndex(frame["time"]))
                frame["source"] = source_map.to_numpy()
                buckets[(candidate, label)].append(frame)
                if label == "baseline":
                    counts[candidate] = len(frame)
        block_audit.append({
            "segment": number,
            "start": start,
            "end": data.index[-1],
            "rows": len(data),
            "eligibleStart": eligible_start,
            "rawBaselineCandidates": counts,
        })

    empty_columns = ["time", "signal", "family", "strategy_id", "branch", "reason", "priority", "block", "source", "regime"]
    combined: dict[tuple[str, str], pd.DataFrame] = {}
    for candidate, rows in variants.items():
        for label, _ in rows:
            parts = buckets.get((candidate, label), [])
            combined[(candidate, label)] = (
                pd.concat(parts, ignore_index=True).sort_values("time", kind="stable").reset_index(drop=True)
                if parts else pd.DataFrame(columns=empty_columns)
            )
    return combined, block_audit


def _ticks_from_canonical(frame: pd.DataFrame) -> pd.DataFrame:
    ticks = frame.reset_index()[["time", "close"]].rename(columns={"close": "price"})
    ticks["market"] = "futures"
    return ticks


def _empty_delay_metrics() -> dict[str, dict[str, Any]]:
    return {str(delay): summarize_metrics(pd.DataFrame()) for delay in DELAYS_SEC}


def validate_variant(
    candidates: pd.DataFrame,
    ticks: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if candidates.empty:
        return pd.DataFrame(), {
            "audit": {"inputCandidates": 0, "dedupedCandidates": 0, "cooldownCandidates": 0},
            "metricsByDelay": _empty_delay_metrics(),
            "metricsByBlock": {},
        }
    return run_validation(
        candidates,
        ticks,
        delays_sec=DELAYS_SEC,
        execution_base_lag_sec=EXECUTION_BASE_LAG_SEC,
        horizon_sec=HORIZON_SEC,
        cooldown_sec=FAMILY_COOLDOWN_SEC,
        amount_u=AMOUNT_U,
        payout_rate=PAYOUT_RATE,
        max_tick_lag_sec=MAX_TICK_LAG_SEC,
        require_futures=True,
        block_col="block",
    )


def _metrics_by_value(trades: pd.DataFrame, column: str) -> dict[str, Any]:
    if trades.empty or column not in trades.columns:
        return {}
    output: dict[str, Any] = {}
    for delay, delayed in trades.groupby("delay_sec", sort=True):
        output[str(int(delay))] = {
            str(value): summarize_metrics(group)
            for value, group in delayed.groupby(column, dropna=False, sort=True)
        }
    return output


def _metrics_by_period(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {}
    frame = trades.copy()
    signal_time = pd.to_datetime(frame["signal_time"], utc=True)
    frame["evidence_period"] = np.where(
        signal_time.lt(pd.Timestamp("2026-07-17T00:00:00Z")),
        "retrospective_development_20260705_16",
        "inspected_audit_20260728_29",
    )
    return _metrics_by_value(frame, "evidence_period")


def _expanding_walkforward(trades: pd.DataFrame) -> dict[str, list[dict[str, Any]]]:
    if trades.empty:
        return {}
    frame = trades.copy()
    frame["beijing_day"] = pd.to_datetime(frame["signal_time"], utc=True).dt.tz_convert(
        "Asia/Shanghai"
    ).dt.strftime("%Y-%m-%d")
    output: dict[str, list[dict[str, Any]]] = {}
    for delay, delayed in frame.groupby("delay_sec", sort=True):
        days = sorted(delayed["beijing_day"].unique())
        rows: list[dict[str, Any]] = []
        for position, day in enumerate(days):
            test = delayed.loc[delayed["beijing_day"].eq(day)]
            train = delayed.loc[delayed["beijing_day"].isin(days[:position])]
            rows.append({
                "testDay": day,
                "priorDays": position,
                "priorHistory": summarize_metrics(train),
                "nextBlock": summarize_metrics(test),
            })
        output[str(int(delay))] = rows
    return output


def _historical_screen(
    report: dict[str, Any],
    trades: pd.DataFrame,
    neighborhood_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    metrics = report["metricsByDelay"]
    settled = [int(metrics[str(delay)]["settled"]) for delay in DELAYS_SEC]
    pnl = [float(metrics[str(delay)]["pnlU"]) for delay in DELAYS_SEC]
    rates = [metrics[str(delay)]["winRatePct"] for delay in DELAYS_SEC]
    rates_finite = [float(value) for value in rates if value is not None]
    drawdowns = [float(metrics[str(delay)]["maxDrawdownU"]) for delay in DELAYS_SEC]
    thin = [metrics[str(delay)]["thinMarginPct"] for delay in DELAYS_SEC]
    thin_finite = [float(value) for value in thin if value is not None]

    variant_labels = sorted({str(row["variant"]) for row in neighborhood_rows})
    positive_labels = 0
    for label in variant_labels:
        rows = [row for row in neighborhood_rows if row["variant"] == label]
        if rows and all(float(row.get("pnlU") or 0.0) > 0.0 for row in rows):
            positive_labels += 1
    positive_pct = 100.0 * positive_labels / len(variant_labels) if variant_labels else 0.0

    direction = _metrics_by_value(trades, "signal").get("5", {})
    direction_ok = all(
        key in direction
        and int(direction[key]["settled"]) >= 5
        and float(direction[key]["pnlU"]) > 0.0
        for key in ("UP", "DOWN")
    )
    daily = metrics_by_block(trades, block_col=None, block_freq="1D", block_timezone="Asia/Shanghai")
    daily5 = daily.get("5", {})
    active_days = [row for row in daily5.values() if int(row.get("settled", 0)) > 0]
    positive_day_pct = (
        100.0 * sum(float(row["pnlU"]) > 0.0 for row in active_days) / len(active_days)
        if active_days else 0.0
    )

    gates = {
        "minimum30IndependentRetrospectiveTrades": min(settled, default=0) >= 30,
        "allDelaysPositiveExpectedValue": bool(pnl) and min(pnl) > 0.0,
        "allDelaysAboveBreakevenWinRate": bool(rates_finite) and min(rates_finite) > BREAKEVEN_WIN_RATE_PCT,
        "bothDirectionsAtLeast5AndProfitableAt5s": direction_ok,
        "parameterNeighborhoodPositiveAllDelaysPctGe60": positive_pct >= 60.0,
        "positiveActiveDayPctGe50": positive_day_pct >= 50.0,
        "maxDrawdownNoMoreThan25U": bool(drawdowns) and max(drawdowns) <= 25.0,
        "thinMarginPctNoMoreThan25": not thin_finite or max(thin_finite) <= 25.0,
    }
    return {
        "passed": all(gates.values()),
        "gates": gates,
        "minimumSettledAcrossDelays": min(settled, default=0),
        "minimumWinRateAcrossDelaysPct": min(rates_finite) if rates_finite else None,
        "minimumPnlAcrossDelaysU": min(pnl) if pnl else None,
        "maximumDrawdownAcrossDelaysU": max(drawdowns) if drawdowns else None,
        "maximumThinMarginPct": max(thin_finite) if thin_finite else None,
        "parameterVariants": len(variant_labels),
        "parameterVariantsPositiveAllDelays": positive_labels,
        "parameterNeighborhoodPositiveAllDelaysPct": round(positive_pct, 4),
        "activeDays": len(active_days),
        "positiveActiveDayPct": round(positive_day_pct, 4),
        "note": "retrospective screen only; never a real-trading promotion certificate",
    }


def _v13_reference() -> dict[str, Any]:
    folder = ROOT / "tmp" / "v14_forward_20260729"
    # The immutable price/book snapshot contains a historically corrupted
    # config copy (invalid JSON label encoding).  Use the valid local frozen
    # V13 parameters while keeping all replay prices in the immutable snapshot.
    config = json.loads((ROOT / "data" / "trade_config.json").read_text(encoding="utf-8-sig"))
    row = next(
        item
        for item in config.get("strategyVariants", [])
        if item.get("id") == "BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V13_SHADOW"
    )
    item = {
        "name": "tmp/v14_forward_20260729",
        "folder": folder,
        "seconds": folder / "btcusdt_1s_trades.csv",
        "orderbook": folder / "btcusdt_orderbook_1s.csv",
    }
    trades, audit = replay_v13_dataset(item, row, AugmentedV9Rules.from_config(row))
    metrics = {
        str(delay): v13_metric(trades, delay, float(audit.get("hours", 1.0)))
        for delay in (0, 5, 6, 10)
    }
    return {
        "role": "invalidated reference only; not a V14 candidate",
        "audit": audit,
        "metrics": metrics,
        "trades": trades.to_dict("records") if not trades.empty else [],
    }


def run(*, skip_neighborhood: bool = False) -> dict[str, Any]:
    print("[1/6] freezing source manifest", flush=True)
    sources, source_manifest = discover_frozen_sources()
    OUT_SOURCE_MANIFEST.write_text(
        json.dumps(_clean(source_manifest), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"[2/6] loading {len(sources)} unique paired snapshots", flush=True)
    canonical = load_canonical_dataset(sources)
    segments, segment_audit = densify_causal_segments(canonical.frame)
    ticks = _ticks_from_canonical(canonical.frame)

    print(f"[3/6] building features across {len(segments)} causal segments", flush=True)
    all_candidates, generation_audit = generate_all_candidates(segments)

    print("[4/6] replaying V13 invalidated reference", flush=True)
    v13 = _v13_reference()

    baseline_trades: list[pd.DataFrame] = []
    baseline_candidates: list[pd.DataFrame] = []
    neighborhood_table: list[dict[str, Any]] = []
    candidate_reports: dict[str, Any] = {}
    print("[5/6] validating candidates and parameter neighborhoods", flush=True)
    for candidate in CANDIDATE_NAMES:
        labels = [label for label, _ in _candidate_variants()[candidate]]
        if skip_neighborhood:
            labels = ["baseline"]
        report_for_candidate: dict[str, Any] | None = None
        trades_for_candidate = pd.DataFrame()
        candidate_neighborhood_rows: list[dict[str, Any]] = []
        for label in labels:
            raw = all_candidates[(candidate, label)]
            trades, validation = validate_variant(raw, ticks)
            for delay in DELAYS_SEC:
                metric_row = validation["metricsByDelay"][str(delay)]
                row = {"candidate": candidate, "variant": label, "delaySec": delay, **metric_row}
                neighborhood_table.append(row)
                candidate_neighborhood_rows.append(row)
            if label == "baseline":
                report_for_candidate = validation
                trades_for_candidate = trades
                if not trades.empty:
                    trades = trades.copy()
                    trades["candidate"] = candidate
                    trades["variant"] = "baseline"
                    baseline_trades.append(trades)
                if not raw.empty:
                    raw = raw.copy()
                    raw["candidate"] = candidate
                    raw["variant"] = "baseline"
                    baseline_candidates.append(raw)
        assert report_for_candidate is not None
        daily = metrics_by_block(
            trades_for_candidate,
            block_col=None,
            block_freq="1D",
            block_timezone="Asia/Shanghai",
        ) if not trades_for_candidate.empty else {}
        report_for_candidate.update({
            "metadata": {
                **candidate_metadata(candidate),
                "hardSafety": dict(shadow_only_candidate_metadata(
                    candidate.replace("-", "_").upper(),
                    candidate,
                    extra={"researchRunId": RUN_ID},
                )),
            },
            "dailyMetrics": daily,
            "directionMetrics": _metrics_by_value(trades_for_candidate, "signal"),
            "branchMetrics": _metrics_by_value(trades_for_candidate, "branch"),
            "regimeMetrics": _metrics_by_value(trades_for_candidate, "regime"),
            "evidencePeriodMetrics": _metrics_by_period(trades_for_candidate),
            "expandingWalkForward": _expanding_walkforward(trades_for_candidate),
        })
        report_for_candidate["retrospectiveScreen"] = _historical_screen(
            report_for_candidate,
            trades_for_candidate,
            candidate_neighborhood_rows,
        )
        candidate_reports[candidate] = report_for_candidate
        print(
            f"  {candidate}: raw={report_for_candidate['audit']['inputCandidates']} "
            f"cooled={report_for_candidate['audit']['cooldownCandidates']} "
            f"screen={report_for_candidate['retrospectiveScreen']['passed']}",
            flush=True,
        )

    screens = {
        candidate: report["retrospectiveScreen"]
        for candidate, report in candidate_reports.items()
    }
    passed = [candidate for candidate, screen in screens.items() if screen["passed"]]
    ranked = sorted(
        CANDIDATE_NAMES,
        key=lambda candidate: (
            screens[candidate]["passed"],
            screens[candidate]["parameterNeighborhoodPositiveAllDelaysPct"],
            screens[candidate]["minimumPnlAcrossDelaysU"] or -1e9,
            screens[candidate]["minimumWinRateAcrossDelaysPct"] or -1e9,
        ),
        reverse=True,
    )
    selected = ranked[0] if passed else None
    decision = {
        "retrospectiveCandidatesPassing": passed,
        "selectedForNewShadowEpoch": selected,
        "ranking": ranked,
        "realTradingAllowed": False,
        "promotionStatus": "PROHIBITED_PENDING_NEW_FORWARD_SHADOW",
        "prospectiveIndependentOpportunitiesSinceCodeFreeze": 0,
        "requiredProspectiveIndependentOpportunities": 150,
        "requiredWilson95LowerPct": round(BREAKEVEN_WIN_RATE_PCT, 4),
        "conclusion": (
            f"{selected} passed the predeclared retrospective screen and may start a new shadow-only epoch; real trading remains prohibited."
            if selected else
            "No V14 candidate passed the predeclared retrospective stability screen; deploy none."
        ),
    }

    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "researchRunId": RUN_ID,
        "safety": {
            "offlineHistoricalOnly": True,
            "productionConfigWritten": False,
            "deploymentPerformed": False,
            "realTradingEnabled": False,
        },
        "method": {
            "market": "Binance Futures for signal, entry and settlement",
            "candidateFeatureAccess": "current-and-past rows only",
            "entryDelaysSec": list(DELAYS_SEC),
            "executionBaseLagSec": EXECUTION_BASE_LAG_SEC,
            "horizonSecFromActualEntry": HORIZON_SEC,
            "familyCooldownSec": FAMILY_COOLDOWN_SEC,
            "shortGapDensification": f"causal forward fill only; orderbook max age 3s; split when missing run exceeds {MAX_INTERNAL_GAP_SEC}s",
            "featureWarmupSecAfterLongGap": FEATURE_WARMUP_SEC,
            "payoutRate": PAYOUT_RATE,
            "breakevenWinRatePct": round(BREAKEVEN_WIN_RATE_PCT, 4),
            "parameterNeighborhood": "one parameter at a time, -20%/-10%/+10%/+20%",
            "evidenceWarning": "all historical periods have been inspected and are retrospective robustness evidence, not a sealed holdout",
        },
        "sourceManifest": str(OUT_SOURCE_MANIFEST),
        "sourceSummary": source_manifest,
        "canonicalAudit": canonical.audit,
        "causalSegments": segment_audit,
        "generationAudit": generation_audit,
        "v13InvalidatedReference": v13,
        "candidates": candidate_reports,
        "decision": decision,
        "outputs": {
            "report": str(OUT_REPORT),
            "trades": str(OUT_TRADES),
            "candidates": str(OUT_CANDIDATES),
            "neighborhood": str(OUT_NEIGHBORHOOD),
            "sources": str(OUT_SOURCE_MANIFEST),
        },
    }

    print("[6/6] writing reports", flush=True)
    OUT_REPORT.write_text(json.dumps(_clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    if baseline_trades:
        pd.concat(baseline_trades, ignore_index=True).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    else:
        pd.DataFrame().to_csv(OUT_TRADES, index=False)
    if baseline_candidates:
        pd.concat(baseline_candidates, ignore_index=True).to_csv(OUT_CANDIDATES, index=False, encoding="utf-8-sig")
    else:
        pd.DataFrame().to_csv(OUT_CANDIDATES, index=False)
    pd.DataFrame(neighborhood_table).to_csv(OUT_NEIGHBORHOOD, index=False, encoding="utf-8-sig")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-neighborhood",
        action="store_true",
        help="Development smoke run only; final research must omit this flag.",
    )
    args = parser.parse_args()
    report = run(skip_neighborhood=args.skip_neighborhood)
    print(json.dumps(_clean({
        "decision": report["decision"],
        "outputs": report["outputs"],
    }), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
