"""Audit position-build-up continuations without treating discovery as validation."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_position_auction_v1 import merge_context, metrics  # noqa: E402


EVENTS = ROOT / "tmp" / "unified_auction_events_10m.csv"
CONTEXT = ROOT / "tmp" / "position_context_20260713"
FROZEN_CONFIG = ROOT / "data" / "frozen_position_build_up_v1.json"
OUT_JSON = ROOT / "tmp" / "position_build_up_v2_discovery.json"
OUT_CSV = ROOT / "tmp" / "position_build_up_v2_discovery_trades.csv"
DELAYS = (0, 5, 6, 10)


def finite_positive(row: pd.Series, column: str) -> bool:
    value = row.get(column, np.nan)
    return bool(np.isfinite(value) and value > 0.0)


def base_build_up(row: pd.Series) -> bool:
    return bool(row.ret_300 > 0.0 and finite_positive(row, "oi_sumOpenInterest_change_15m"))


def original_v1(row: pd.Series) -> str | None:
    return "UP" if base_build_up(row) else None


def pullback_build(row: pd.Series) -> str | None:
    if not base_build_up(row):
        return None
    return "UP" if row.ret_60 <= 0.0 else None


def avoid_crowded_chase(row: pd.Series) -> str | None:
    if not base_build_up(row):
        return None
    crowded_chase = (
        row.ret_60 > 0.0
        and row.flow_60 > 0.0
        and finite_positive(row, "global_longShortRatio_change_15m")
        and finite_positive(row, "top_account_longShortRatio_change_15m")
    )
    return None if crowded_chase else "UP"


CANDIDATES: dict[str, Callable[[pd.Series], str | None]] = {
    "original_v1": original_v1,
    "pullback_build": pullback_build,
    "avoid_crowded_chase": avoid_crowded_chase,
}


def evaluate(frame: pd.DataFrame) -> dict:
    return {f"delay{delay}s": metrics(frame, delay) for delay in DELAYS}


def chronological_halves(frame: pd.DataFrame) -> dict[str, pd.DataFrame]:
    if frame.empty:
        return {"early": frame, "late": frame}
    midpoint = frame.time.min() + (frame.time.max() - frame.time.min()) / 2
    return {
        "early": frame[frame.time < midpoint],
        "late": frame[frame.time >= midpoint],
    }


def main() -> None:
    frozen = json.loads(FROZEN_CONFIG.read_text(encoding="utf-8"))
    cutoff = pd.Timestamp(frozen["frozenAt"])
    events = pd.read_csv(EVENTS, parse_dates=["time", "entry_time", "settle_time"])
    data = merge_context(events, CONTEXT)
    discovery = data[(data.time < cutoff) & data.oi_sumOpenInterest.notna()].copy()

    reports: dict[str, dict] = {}
    all_trades: list[pd.DataFrame] = []
    for name, rule in CANDIDATES.items():
        selected = discovery.copy()
        selected["signal"] = selected.apply(rule, axis=1)
        selected = selected[selected.signal.isin(["UP", "DOWN"])].copy()
        selected["candidate"] = name
        all_trades.append(selected)
        reports[name] = {
            "overall": evaluate(selected),
            "chronologicalHalves": {
                part: evaluate(group) for part, group in chronological_halves(selected).items()
            },
            "byUtcDateDelay6s": {
                str(day): metrics(group, 6)
                for day, group in selected.groupby(selected.time.dt.date)
            },
        }

    report = {
        "status": "discovery_only_not_validation",
        "createdAfterInspectingV1ForwardOutcomes": True,
        "forwardOutcomesMustNotBeCounted": True,
        "discoveryCutoffExclusive": cutoff.isoformat(),
        "discoveryCoverage": {
            "events": int(len(discovery)),
            "start": discovery.time.min().isoformat() if not discovery.empty else None,
            "end": discovery.time.max().isoformat() if not discovery.empty else None,
        },
        "hypotheses": {
            "original_v1": "过去5分钟上涨且15分钟总持仓增加，预测上涨。",
            "pullback_build": "持仓扩张趋势只在最近1分钟回踩时入场，避免追逐末端加速。",
            "avoid_crowded_chase": "价格、主动成交、全市场账户和顶级账户同时偏多时视为拥挤追涨并跳过。",
        },
        "parameterSearch": False,
        "results": reports,
        "nextStep": "Only a structurally sound candidate may be frozen at a new timestamp; all earlier and already inspected rows remain discovery data.",
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.concat(all_trades, ignore_index=True).to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
