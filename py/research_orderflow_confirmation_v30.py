"""Time-ordered order-flow confirmation research for frozen DOWN signals."""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research_archived_aggtrades_v23 import MAX_TICK_LAG_SEC
from research_minute_volatility_normal_v15 import BREAKEVEN_WR, clean
from research_second_reclaim_walkforward_v29 import (
    HORIZON_SEC,
    OUT_JSON as V29_JSON,
    OUT_TICKS as V29_TICKS,
    POST_CONFIRM_DELAYS,
    _resolve_tick,
    common_settled,
    load_signal_sets,
    summarize_rule,
)
from v14_validation import TickResolver


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "docs" / "v30_orderflow_confirmation_protocol_20260730.md"
OUT_JSON = ROOT / "tmp" / "v30_orderflow_confirmation_20260730.json"
OUT_FLOW = ROOT / "tmp" / "v30_orderflow_filtered_ticks_20260730.csv"
OUT_TRADES = ROOT / "tmp" / "v30_orderflow_confirmation_trades_20260730.csv"
RULES = (
    ("flow_le_0", 0.0, False),
    ("flow_le_m0p2", -0.20, False),
    ("flow_le_0_and_price_reclaim", 0.0, True),
)
MIN_DEVELOPMENT = 50
MIN_COMBINED_TEST = 60


def feature_ranges(signals: pd.DataFrame) -> dict[str, list[tuple[int, int]]]:
    output: dict[str, list[tuple[int, int]]] = {}
    for signal_time in pd.to_datetime(signals["signal_time"], utc=True):
        start_ms = int(signal_time.timestamp() * 1000)
        end_ms = int((signal_time + pd.Timedelta(seconds=5)).timestamp() * 1000)
        output.setdefault(signal_time.strftime("%Y-%m-%d"), []).append(
            (start_ms, end_ms)
        )
    return output


def _read_orderflow_archive(
    archive: dict[str, Any], ranges: list[tuple[int, int]]
) -> pd.DataFrame:
    path = Path(str(archive["path"]))
    retained = []
    with zipfile.ZipFile(path) as bundle:
        members = [name for name in bundle.namelist() if name.lower().endswith(".csv")]
        if len(members) != 1:
            raise ValueError(f"{path} expected one CSV member, got {members}")
        member = members[0]
        with bundle.open(member) as stream:
            header = pd.read_csv(stream, nrows=0)
        columns = list(header.columns)
        if "transact_time" in columns:
            time_col = "transact_time"
            price_col = "price"
            qty_col = "quantity"
            maker_col = "is_buyer_maker"
            id_col = "agg_trade_id" if "agg_trade_id" in columns else columns[0]
            read_kwargs: dict[str, Any] = {}
        else:
            time_col = "transact_time"
            price_col = "price"
            qty_col = "quantity"
            maker_col = "is_buyer_maker"
            id_col = "agg_trade_id"
            read_kwargs = {
                "header": None,
                "names": [
                    "agg_trade_id",
                    "price",
                    "quantity",
                    "first_trade_id",
                    "last_trade_id",
                    "transact_time",
                    "is_buyer_maker",
                ],
            }
        with bundle.open(member) as stream:
            for chunk in pd.read_csv(
                stream,
                usecols=[id_col, price_col, qty_col, time_col, maker_col],
                chunksize=500_000,
                low_memory=False,
                **read_kwargs,
            ):
                timestamp = pd.to_numeric(chunk[time_col], errors="coerce")
                mask = pd.Series(False, index=chunk.index)
                for start_ms, end_ms in ranges:
                    mask |= timestamp.between(start_ms, end_ms, inclusive="both")
                if not mask.any():
                    continue
                part = chunk.loc[
                    mask, [id_col, price_col, qty_col, time_col, maker_col]
                ].copy()
                part.columns = [
                    "agg_trade_id",
                    "price",
                    "quantity",
                    "time_ms",
                    "is_buyer_maker",
                ]
                part["archive_date"] = archive["date"]
                retained.append(part)
    if not retained:
        return pd.DataFrame(
            columns=[
                "agg_trade_id",
                "price",
                "quantity",
                "time_ms",
                "is_buyer_maker",
                "archive_date",
            ]
        )
    return pd.concat(retained, ignore_index=True)


def load_orderflow_rows(
    archives: list[dict[str, Any]], ranges_by_date: dict[str, list[tuple[int, int]]]
) -> pd.DataFrame:
    parts = []
    for archive in archives:
        ranges = ranges_by_date.get(str(archive["date"]), [])
        if not ranges:
            continue
        part = _read_orderflow_archive(archive, ranges)
        print(f"orderflow {archive['date']} {len(part)} rows", flush=True)
        if not part.empty:
            parts.append(part)
    if not parts:
        raise ValueError("no V30 order-flow rows matched frozen feature windows")
    frame = pd.concat(parts, ignore_index=True)
    frame["agg_trade_id"] = pd.to_numeric(frame["agg_trade_id"], errors="coerce")
    frame["price"] = pd.to_numeric(frame["price"], errors="coerce")
    frame["quantity"] = pd.to_numeric(frame["quantity"], errors="coerce")
    frame["time"] = pd.to_datetime(
        pd.to_numeric(frame["time_ms"], errors="coerce"), unit="ms", utc=True
    )
    maker = frame["is_buyer_maker"].astype(str).str.strip().str.lower()
    frame["is_buyer_maker"] = maker.map(
        {"true": True, "false": False, "1": True, "0": False}
    )
    frame = frame.dropna(
        subset=["agg_trade_id", "price", "quantity", "time", "is_buyer_maker"]
    )
    frame = frame.loc[
        np.isfinite(frame["price"])
        & frame["price"].gt(0.0)
        & np.isfinite(frame["quantity"])
        & frame["quantity"].gt(0.0)
    ]
    return frame.sort_values(["time", "agg_trade_id"], kind="stable").drop_duplicates(
        ["time", "agg_trade_id"], keep="last"
    ).reset_index(drop=True)


def build_features(signals: pd.DataFrame, flow: pd.DataFrame, price_ticks: pd.DataFrame) -> pd.DataFrame:
    resolver = TickResolver(
        pd.DataFrame(
            {
                "time": pd.to_datetime(price_ticks["time"], utc=True).to_numpy(),
                "price": pd.to_numeric(price_ticks["price"], errors="coerce").to_numpy(),
            }
        ).sort_values("time", kind="stable")
    )
    time_ns = flow["time"].to_numpy(dtype="datetime64[ns]").astype(np.int64)
    qty = flow["quantity"].to_numpy(float)
    buyer_maker = flow["is_buyer_maker"].to_numpy(bool)
    rows = []
    for candidate in signals.to_dict("records"):
        start = pd.Timestamp(candidate["signal_time"])
        end = start + pd.Timedelta(seconds=5)
        left = int(np.searchsorted(time_ns, start.value, side="left"))
        right = int(np.searchsorted(time_ns, end.value, side="right"))
        local_qty = qty[left:right]
        local_maker = buyer_maker[left:right]
        sell_qty = float(local_qty[local_maker].sum())
        buy_qty = float(local_qty[~local_maker].sum())
        total = buy_qty + sell_qty
        imbalance = (buy_qty - sell_qty) / total if total > 0.0 else np.nan
        p0 = _resolve_tick(resolver, start)
        p5 = _resolve_tick(resolver, end)
        price_change = (
            (p5["price"] / p0["price"] - 1.0) * 10_000.0
            if p0 is not None and p5 is not None
            else np.nan
        )
        rows.append(
            {
                **candidate,
                "buy_taker_qty_5s": buy_qty,
                "sell_taker_qty_5s": sell_qty,
                "total_qty_5s": total,
                "flow_imbalance_5s": imbalance,
                "price_change_5s_bps": price_change,
                "flow_trade_rows_5s": int(right - left),
            }
        )
    return pd.DataFrame(rows)


def build_flow_trades(
    features: pd.DataFrame, price_ticks: pd.DataFrame
) -> pd.DataFrame:
    canonical = pd.DataFrame(
        {
            "time": pd.to_datetime(price_ticks["time"], utc=True).to_numpy(),
            "price": pd.to_numeric(price_ticks["price"], errors="coerce").to_numpy(),
        }
    ).sort_values("time", kind="stable")
    resolver = TickResolver(canonical)
    rows: list[dict[str, Any]] = []
    for candidate in features.to_dict("records"):
        signal_time = pd.Timestamp(candidate["signal_time"])
        fixed_target = signal_time + pd.Timedelta(seconds=HORIZON_SEC)
        fixed_tick = _resolve_tick(resolver, fixed_target)
        for rule, threshold, require_price_reclaim in RULES:
            confirmed = bool(
                pd.notna(candidate["flow_imbalance_5s"])
                and candidate["flow_imbalance_5s"] <= threshold
                and (
                    not require_price_reclaim
                    or (
                        pd.notna(candidate["price_change_5s_bps"])
                        and candidate["price_change_5s_bps"] <= 0.0
                    )
                )
            )
            for delay in POST_CONFIRM_DELAYS:
                entry_target = signal_time + pd.Timedelta(seconds=5 + delay)
                entry = _resolve_tick(resolver, entry_target)
                for settlement_mode in ("entry_plus_600s", "fixed_boundary"):
                    base = {
                        "candidate_id": candidate["candidate_id"],
                        "signal_time": signal_time,
                        "period": candidate["period"],
                        "year": signal_time.year,
                        "rule": rule,
                        "flow_threshold": threshold,
                        "require_price_reclaim": require_price_reclaim,
                        "flow_imbalance_5s": candidate["flow_imbalance_5s"],
                        "price_change_5s_bps": candidate["price_change_5s_bps"],
                        "confirmed": confirmed,
                        "post_confirm_delay_sec": int(delay),
                        "total_delay_sec": int(5 + delay),
                        "settlement_mode": settlement_mode,
                    }
                    if not confirmed:
                        rows.append({**base, "status": "rejected", "pnl_u": np.nan})
                        continue
                    if entry is None:
                        rows.append({**base, "status": "missing_entry", "pnl_u": np.nan})
                        continue
                    if settlement_mode == "entry_plus_600s":
                        settle_target = entry["time"] + pd.Timedelta(seconds=HORIZON_SEC)
                        settle = _resolve_tick(resolver, settle_target)
                    else:
                        settle_target = fixed_target
                        settle = fixed_tick
                    if settle is None:
                        rows.append({**base, "status": "missing_settlement", "pnl_u": np.nan})
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


def development_eligibility(report: dict[str, Any]) -> tuple[bool, tuple[float, ...]]:
    slices = report["slices"]
    eligible = bool(
        report["commonCoverageSignals"] >= MIN_DEVELOPMENT
        and len(slices) == 6
        and all(
            row["pnlU"] > 0.0
            and row["winRatePct"] is not None
            and row["winRatePct"] > BREAKEVEN_WR
            and row["bootstrap"]["lower90EvU"] is not None
            and row["bootstrap"]["lower90EvU"] > 0.0
            and row["maxDrawdownU"] <= 30.0
            and row["maxLossStreak"] <= 3
            for row in slices.values()
        )
    )
    score = (
        min((row["expectedValueU"] or -99.0) for row in slices.values()),
        min((row["wilson95LowerPct"] or 0.0) for row in slices.values()),
        float(report["commonCoverageSignals"]),
    ) if slices else (-99.0, 0.0, 0.0)
    return eligible, score


def select_development_rule(results: dict[str, Any]) -> dict[str, Any] | None:
    ranked = []
    simplicity = {"flow_le_0": 2, "flow_le_m0p2": 1, "flow_le_0_and_price_reclaim": 0}
    for rule in (name for name, _, _ in RULES):
        eligible, score = development_eligibility(
            results[rule]["development_2020_2022"]
        )
        if eligible:
            ranked.append(((*score, simplicity[rule]), rule))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    score, rule = ranked[0]
    return {"rule": rule, "score": list(score)}


def run() -> dict[str, Any]:
    signals, _ = load_signal_sets()
    v29 = json.loads(V29_JSON.read_text(encoding="utf-8"))
    archives = v29["ticks"]["archives"]
    ranges = feature_ranges(signals)
    if OUT_FLOW.exists():
        flow = pd.read_csv(OUT_FLOW)
        flow["time"] = pd.to_datetime(
            flow["time"], utc=True, errors="raise", format="mixed"
        )
        maker = flow["is_buyer_maker"].astype(str).str.strip().str.lower()
        flow["is_buyer_maker"] = maker.map(
            {"true": True, "false": False, "1": True, "0": False}
        )
        if flow["is_buyer_maker"].isna().any():
            raise ValueError("cached V30 order-flow side column is invalid")
    else:
        flow = load_orderflow_rows(archives, ranges)
        flow.to_csv(OUT_FLOW, index=False, encoding="utf-8-sig")
    price_ticks = pd.read_csv(V29_TICKS)
    price_ticks["time"] = pd.to_datetime(
        price_ticks["time"], utc=True, errors="raise", format="mixed"
    )
    features = build_features(signals, flow, price_ticks)
    trades = build_flow_trades(features, price_ticks)
    trades.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    periods = {
        "development_2020_2022": trades["period"].eq("development_2020_2022"),
        "test_2023": trades["period"].eq("test_2023"),
        "reused_2024_2025": trades["period"].eq("reused_2024_2025"),
        "reused_2026": trades["period"].eq("reused_2026"),
        "combined_test_2023_2026": trades["period"].ne("development_2020_2022"),
    }
    results = {
        rule: {
            period: summarize_rule(trades.loc[mask], rule, period)
            for period, mask in periods.items()
        }
        for rule, _, _ in RULES
    }
    selection = select_development_rule(results)
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "status": "V30_FROZEN_ORDERFLOW_CONFIRMATION",
        "safety": {
            "researchOnly": True,
            "tradeEnabled": False,
            "deploymentPerformed": False,
            "onlineConfigurationChanged": False,
            "realTradingAllowed": False,
        },
        "protocol": {
            "file": str(PROTOCOL.resolve()),
            "rules": [
                {
                    "name": name,
                    "flowThreshold": threshold,
                    "requirePriceReclaim": require_price,
                }
                for name, threshold, require_price in RULES
            ],
        },
        "signals": {
            "total": int(len(signals)),
            "validOrderflowFeatures": int(features["flow_imbalance_5s"].notna().sum()),
        },
        "orderflow": {
            "filteredRows": int(len(flow)),
            "start": flow["time"].min(),
            "end": flow["time"].max(),
        },
        "results": results,
        "developmentSelection": selection,
        "decision": {
            "passedDevelopment": selection is not None,
            "action": "continue_test" if selection is not None else "no_trade",
            "deployment": "none",
            "realTradingAllowed": False,
        },
        "outputs": {
            "json": str(OUT_JSON.resolve()),
            "orderflowTicks": str(OUT_FLOW.resolve()),
            "trades": str(OUT_TRADES.resolve()),
        },
    }
    OUT_JSON.write_text(
        json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    report = run()
    print(
        json.dumps(
            clean(
                {
                    "signals": report["signals"],
                    "orderflow": report["orderflow"],
                    "developmentSelection": report["developmentSelection"],
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
