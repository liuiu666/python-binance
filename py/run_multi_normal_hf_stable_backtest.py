"""Backtest the shared adaptive multi-normal strategy on local data.

The primary report assumes a two-second execution delay after a completed
minute. A delay sweep is included to expose signals that only win at an exact
historical close and would therefore be fragile in live trading.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from multi_normal_hf_stable_core import (  # noqa: E402
    STRATEGY_ID,
    MultiNormalHFStableConfig,
    build_snapshots,
    iter_signal_decisions,
)
from research_normal_liquidity_orderbook import load_local_data  # noqa: E402


@dataclass(frozen=True)
class SourceSpec:
    name: str
    seconds: Path
    orderbook: Path
    start: str | None = None
    end: str | None = None
    role: str = "history"


DEFAULT_SOURCES = (
    SourceSpec(
        "2026-07-05_06",
        ROOT / "tmp" / "latest_pull_20260706_2130" / "data" / "btcusdt_1s_trades.csv",
        ROOT / "tmp" / "latest_pull_20260706_2130" / "data" / "btcusdt_orderbook_1s.csv",
    ),
    SourceSpec(
        "2026-07-08_09",
        ROOT / "tmp" / "latest_live_pull_20260709_101331" / "data" / "btcusdt_1s_trades.csv",
        ROOT / "tmp" / "latest_live_pull_20260709_101331" / "data" / "btcusdt_orderbook_1s.csv",
        end="2026-07-09T02:02:00Z",
    ),
    SourceSpec(
        "2026-07-09_10",
        ROOT / "tmp" / "latest_pull_20260710_203217" / "data" / "btcusdt_1s_trades.csv",
        ROOT / "tmp" / "latest_pull_20260710_203217" / "data" / "btcusdt_orderbook_1s.csv",
        start="2026-07-09T02:14:00Z",
    ),
    SourceSpec(
        "latest_independent",
        ROOT / "data" / "server_latest" / "btcusdt_1s_trades.csv",
        ROOT / "data" / "server_latest" / "btcusdt_orderbook_1s.csv",
        start="2026-07-10T20:30:00Z",
        role="independent",
    ),
)


@dataclass
class LoadedSource:
    spec: SourceSpec
    data: pd.DataFrame
    snapshots: pd.DataFrame
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    hours: float


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return value


def utc(value: str | pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def load_sources(
    specs: tuple[SourceSpec, ...],
    cfg: MultiNormalHFStableConfig,
) -> list[LoadedSource]:
    loaded = []
    for spec in specs:
        if not spec.seconds.exists() or not spec.orderbook.exists():
            raise FileNotFoundError(f"missing source files for {spec.name}")
        data = load_local_data(spec.seconds, spec.orderbook)
        snapshots = build_snapshots(data, spec.name, cfg, include_future=False).reset_index(drop=True)
        snapshots["time"] = pd.to_datetime(snapshots["time"], utc=True)
        start = utc(spec.start) if spec.start else utc(snapshots["time"].min())
        end = utc(spec.end) if spec.end else utc(snapshots["time"].max())
        snapshots = snapshots[(snapshots["time"] >= start) & (snapshots["time"] <= end)].copy()
        if snapshots.empty:
            raise ValueError(f"no usable snapshots for {spec.name}: {start} -> {end}")
        test_start = utc(snapshots["time"].min())
        test_end = utc(snapshots["time"].max()) + pd.Timedelta(minutes=1)
        loaded.append(
            LoadedSource(
                spec=spec,
                data=data,
                snapshots=snapshots,
                test_start=test_start,
                test_end=test_end,
                hours=max(1.0 / 60.0, (test_end - test_start).total_seconds() / 3600.0),
            )
        )
    return loaded


def price_at_or_after(
    close: pd.Series,
    target: pd.Timestamp,
    max_age_sec: int = 3,
) -> tuple[pd.Timestamp, float] | None:
    idx = int(close.index.searchsorted(target))
    if idx >= len(close):
        return None
    timestamp = pd.Timestamp(close.index[idx])
    if (timestamp - target).total_seconds() > max_age_sec:
        return None
    return timestamp, float(close.iloc[idx])


def replay_source(
    source: LoadedSource,
    cfg: MultiNormalHFStableConfig,
    execution_delay_sec: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    close = source.data["close"].astype(float)
    rows = []
    last_emit_time: pd.Timestamp | None = None
    diagnostics = {"candidates": 0, "cooldownRejected": 0, "priceRejected": 0}
    for candidate in iter_signal_decisions(source.snapshots, cfg):
        diagnostics["candidates"] += 1
        detected_time = utc(candidate["detected_time"])
        if last_emit_time is not None and (detected_time - last_emit_time).total_seconds() < cfg.min_gap_sec:
            diagnostics["cooldownRejected"] += 1
            continue
        entry_target = detected_time + pd.Timedelta(seconds=execution_delay_sec)
        entry = price_at_or_after(close, entry_target)
        settle = price_at_or_after(close, entry_target + pd.Timedelta(seconds=cfg.horizon_sec))
        if entry is None or settle is None:
            diagnostics["priceRejected"] += 1
            continue
        entry_time, entry_price = entry
        settle_time, settle_price = settle
        signal = str(candidate["signal"])
        signal_sign = 1.0 if signal == "UP" else -1.0
        signed_outcome_bps = (settle_price / entry_price - 1.0) * 10000.0 * signal_sign
        rows.append(
            {
                "strategy_id": STRATEGY_ID,
                "source": source.spec.name,
                "role": source.spec.role,
                "minute_time": utc(candidate["minute_time"]),
                "detected_time": detected_time,
                "entry_time": entry_time,
                "settle_time": settle_time,
                "execution_delay_sec": execution_delay_sec,
                "signal": signal,
                "module": candidate["module"],
                "reason": candidate["reason"],
                "reason_zh": candidate["reason_zh"],
                "entry": entry_price,
                "settle": settle_price,
                "signed_outcome_bps": signed_outcome_bps,
                "won": bool(signed_outcome_bps > 0.0),
                "z": candidate.get("z"),
                "z_required": candidate.get("z_required"),
                "sigma10_bps": candidate.get("sigma10_bps"),
                "range10_bps": candidate.get("range10_bps"),
                "ret10_bps": candidate.get("ret10_bps"),
                "ret30_bps": candidate.get("ret30_bps"),
                "ret60_bps": candidate.get("ret60_bps"),
                "flow5": candidate.get("flow5"),
                "imb20": candidate.get("imb20"),
                "signed_flow": candidate.get("signed_flow"),
                "signed_book": candidate.get("signed_book"),
                "trend": candidate.get("trend"),
                "sprint": candidate.get("sprint"),
                "normal_quality": candidate.get("normal_quality"),
                "normal_pos": candidate.get("normal_pos"),
                "high_volatility": candidate.get("high_volatility"),
            }
        )
        last_emit_time = detected_time
    return rows, diagnostics


def metrics(
    rows: pd.DataFrame,
    hours: float,
    amount: float = 5.0,
    payout_rate: float = 0.8,
) -> dict[str, Any]:
    if rows.empty:
        return {
            "trades": 0,
            "wins": 0,
            "winRate": 0.0,
            "pnlU": 0.0,
            "maxDrawdownU": 0.0,
            "maxLossStreak": 0,
            "tradesPerDay": 0.0,
            "avgSignedBps": None,
            "medianSignedBps": None,
        }
    ordered = rows.sort_values("entry_time")
    pnls = [amount * payout_rate if bool(won) else -amount for won in ordered["won"]]
    equity = peak = max_drawdown = 0.0
    loss_streak = max_loss_streak = 0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        loss_streak = 0 if pnl > 0.0 else loss_streak + 1
        max_loss_streak = max(max_loss_streak, loss_streak)
    wins = int(ordered["won"].astype(bool).sum())
    return {
        "trades": int(len(ordered)),
        "wins": wins,
        "winRate": round(wins / len(ordered) * 100.0, 2),
        "pnlU": round(float(sum(pnls)), 2),
        "maxDrawdownU": round(max_drawdown, 2),
        "maxLossStreak": max_loss_streak,
        "tradesPerDay": round(len(ordered) / max(hours, 1e-9) * 24.0, 2),
        "avgSignedBps": round(float(ordered["signed_outcome_bps"].mean()), 4),
        "medianSignedBps": round(float(ordered["signed_outcome_bps"].median()), 4),
    }


def grouped_metrics(
    rows: pd.DataFrame,
    key: str,
    hours: float,
    amount: float,
    payout_rate: float,
) -> dict[str, Any]:
    if rows.empty:
        return {}
    return {
        str(name): metrics(group, hours, amount, payout_rate)
        for name, group in rows.groupby(key, dropna=False)
    }


def replay_all(
    loaded: list[LoadedSource],
    cfg: MultiNormalHFStableConfig,
    execution_delay_sec: int,
) -> tuple[pd.DataFrame, dict[str, dict[str, int]]]:
    rows = []
    diagnostics = {}
    for source in loaded:
        source_rows, source_diagnostics = replay_source(source, cfg, execution_delay_sec)
        rows.extend(source_rows)
        diagnostics[source.spec.name] = source_diagnostics
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.sort_values(["entry_time", "source"]).reset_index(drop=True)
    return frame, diagnostics


def robustness_report(
    loaded: list[LoadedSource],
    base_cfg: MultiNormalHFStableConfig,
    amount: float,
    payout_rate: float,
) -> list[dict[str, Any]]:
    rows = []
    for sigma_gate in (7.0, 8.0, 10.0, 12.0):
        for high_vol_z in (0.5, 0.8):
            cfg = replace(
                base_cfg,
                trend_high_vol_sigma_min_bps=sigma_gate,
                trend_high_vol_z_min=high_vol_z,
            )
            for delay in (0, 1, 2, 3, 5, 10):
                trades, _ = replay_all(loaded, cfg, delay)
                history = trades[trades["role"] == "history"] if not trades.empty else trades
                independent = trades[trades["role"] == "independent"] if not trades.empty else trades
                history_hours = sum(source.hours for source in loaded if source.spec.role == "history")
                independent_hours = sum(source.hours for source in loaded if source.spec.role == "independent")
                rows.append(
                    {
                        "sigmaGateBps": sigma_gate,
                        "highVolZMin": high_vol_z,
                        "delaySec": delay,
                        "history": metrics(history, history_hours, amount, payout_rate),
                        "independent": metrics(independent, independent_hours, amount, payout_rate),
                    }
                )
    return rows


def run(args: argparse.Namespace) -> tuple[dict[str, Any], pd.DataFrame]:
    cfg = MultiNormalHFStableConfig()
    loaded = load_sources(DEFAULT_SOURCES, cfg)
    trades, diagnostics = replay_all(loaded, cfg, args.execution_delay_sec)
    total_hours = sum(source.hours for source in loaded)
    history_hours = sum(source.hours for source in loaded if source.spec.role == "history")
    independent_hours = sum(source.hours for source in loaded if source.spec.role == "independent")
    history = trades[trades["role"] == "history"] if not trades.empty else trades
    independent = trades[trades["role"] == "independent"] if not trades.empty else trades
    if not trades.empty:
        trades["day_shanghai"] = trades["entry_time"].dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
    source_report = {}
    for source in loaded:
        subset = trades[trades["source"] == source.spec.name] if not trades.empty else trades
        source_report[source.spec.name] = {
            "role": source.spec.role,
            "seconds": source.spec.seconds,
            "orderbook": source.spec.orderbook,
            "testStart": source.test_start,
            "testEnd": source.test_end,
            "hours": round(source.hours, 4),
            "diagnostics": diagnostics[source.spec.name],
            "result": metrics(subset, source.hours, args.amount, args.payout_rate),
        }
    report = {
        "strategyId": STRATEGY_ID,
        "method": (
            "Causal completed-minute replay. The same evaluate_snapshot function is used for every source; "
            "entry and settlement are both shifted by the configured execution delay."
        ),
        "config": asdict(cfg),
        "execution": {
            "delaySec": args.execution_delay_sec,
            "amountU": args.amount,
            "payoutRate": args.payout_rate,
            "oneOpenWindowSec": cfg.min_gap_sec,
        },
        "sources": source_report,
        "history": metrics(history, history_hours, args.amount, args.payout_rate),
        "independent": metrics(independent, independent_hours, args.amount, args.payout_rate),
        "combined": metrics(trades, total_hours, args.amount, args.payout_rate),
        "byModule": grouped_metrics(trades, "module", total_hours, args.amount, args.payout_rate),
        "byDirection": grouped_metrics(trades, "signal", total_hours, args.amount, args.payout_rate),
        "byShanghaiDay": grouped_metrics(trades, "day_shanghai", 24.0, args.amount, args.payout_rate),
        "robustness": [] if args.no_sensitivity else robustness_report(loaded, cfg, args.amount, args.payout_rate),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    trades.to_csv(args.trades_out, index=False, encoding="utf-8-sig")
    return report, trades


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-delay-sec", type=int, default=2)
    parser.add_argument("--amount", type=float, default=5.0)
    parser.add_argument("--payout-rate", type=float, default=0.8)
    parser.add_argument("--no-sensitivity", action="store_true")
    parser.add_argument(
        "--out",
        default=str(ROOT / "tmp" / "multi_normal_hf_stable_v1_backtest.json"),
    )
    parser.add_argument(
        "--trades-out",
        default=str(ROOT / "tmp" / "multi_normal_hf_stable_v1_trades.csv"),
    )
    args = parser.parse_args()
    report, _ = run(args)
    print(
        json.dumps(
            clean(
                {
                    "strategyId": report["strategyId"],
                    "history": report["history"],
                    "independent": report["independent"],
                    "combined": report["combined"],
                    "byModule": report["byModule"],
                    "byDirection": report["byDirection"],
                    "byShanghaiDay": report["byShanghaiDay"],
                }
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
