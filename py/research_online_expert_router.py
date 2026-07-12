from __future__ import annotations

import json
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_normal_liquidity_orderbook import load_local_data  # noqa: E402
import research_adaptive_regime_switch as adaptive  # noqa: E402
import research_detailed_trend_states as detailed  # noqa: E402
from research_parameter_stability_audit import SOURCES  # noqa: E402


OUT_JSON = ROOT / "tmp" / "online_expert_router_backtest.json"
OUT_CSV = ROOT / "tmp" / "online_expert_router_trades.csv"


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if np.isfinite(number) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def state_family(state: str) -> str:
    if state == "flat":
        return "flat"
    if state.startswith("drift_"):
        return "drift"
    if state.startswith("trend_"):
        return "trend"
    return "other"


def reversion_signal(edge: str) -> str:
    return "DOWN" if edge == "upper" else "UP"


def won_for_signal(move_bps: float, signal: str) -> bool:
    return move_bps > 0.0 if signal == "UP" else move_bps < 0.0


def sample_events(data: pd.DataFrame) -> pd.DataFrame:
    events = adaptive.build_candidates(data, "trend_all")
    if events.empty:
        return events
    # The shadow learner gets one independent opportunity per minute. Real
    # orders still use a separate 600-second cooldown.
    accepted = []
    last_time: pd.Timestamp | None = None
    for row in events.sort_values("time").to_dict("records"):
        timestamp = pd.Timestamp(row["time"])
        if last_time is not None and (timestamp - last_time).total_seconds() < 60:
            continue
        row["family"] = state_family(str(row["state"]))
        row["reversion_signal"] = reversion_signal(str(row["edge"]))
        if str(row["state"]) in {"trend_up", "drift_up"}:
            row["trend_signal"] = "UP"
        elif str(row["state"]) in {"trend_down", "drift_down"}:
            row["trend_signal"] = "DOWN"
        else:
            continue
        # Only disagreement events identify whether the current market rewards
        # normal reversion or macro-trend continuation. Same-direction events
        # cannot rank the two experts and previously polluted the score.
        if row["trend_signal"] == row["reversion_signal"]:
            continue
        row["reversion_won"] = won_for_signal(float(row["move_bps"]), row["reversion_signal"])
        accepted.append(row)
        last_time = timestamp
    return pd.DataFrame(accepted)


def trim_history(history: deque[tuple[pd.Timestamp, bool]], now: pd.Timestamp, hours: float, max_samples: int) -> None:
    cutoff = now - pd.Timedelta(hours=hours)
    while history and (history[0][0] < cutoff or len(history) > max_samples):
        history.popleft()


def online_route(
    events: pd.DataFrame,
    history_hours: float,
    min_local: int,
    min_global: int,
    margin: float,
) -> pd.DataFrame:
    if events.empty:
        return events
    local_history: dict[str, deque[tuple[pd.Timestamp, bool]]] = defaultdict(deque)
    global_history: deque[tuple[pd.Timestamp, bool]] = deque()
    pending: deque[dict[str, Any]] = deque()
    trades = []
    last_trade: pd.Timestamp | None = None
    break_even = 5.0 / 9.0

    for row in events.sort_values("time").to_dict("records"):
        now = pd.Timestamp(row["time"])
        while pending and pd.Timestamp(pending[0]["settle_time"]) <= now:
            resolved = pending.popleft()
            settled_at = pd.Timestamp(resolved["settle_time"])
            outcome = bool(resolved["reversion_won"])
            local_history[str(resolved["key"])].append((settled_at, outcome))
            global_history.append((settled_at, outcome))

        key = f"{row['family']}|{row['edge']}"
        row["key"] = key
        row["settle_time"] = now + pd.Timedelta(seconds=600)
        pending.append(row)

        trim_history(local_history[key], now, history_hours, 24)
        trim_history(global_history, now, history_hours, 48)
        local = list(local_history[key])
        global_rows = list(global_history)
        chosen = local if len(local) >= min_local else global_rows
        minimum = min_local if len(local) >= min_local else min_global
        if len(chosen) < minimum:
            continue
        reversion_rate = sum(outcome for _, outcome in chosen) / len(chosen)
        if reversion_rate >= break_even + margin:
            expert = "reversion"
            signal = str(row["reversion_signal"])
        elif reversion_rate <= (1.0 - break_even) - margin:
            expert = "trend"
            signal = str(row["trend_signal"])
        else:
            continue
        if last_trade is not None and (now - last_trade).total_seconds() < 600:
            continue

        won = won_for_signal(float(row["move_bps"]), signal)
        item = dict(row)
        item.update(
            {
                "expert": expert,
                "signal": signal,
                "won": won,
                "pnl": 4 if won else -5,
                "history_n": len(chosen),
                "history_scope": "local" if chosen is local else "global",
                "reversion_rate": round(reversion_rate, 6),
                "history_hours": history_hours,
                "min_local": min_local,
                "min_global": min_global,
                "margin": margin,
            }
        )
        trades.append(item)
        last_trade = now
    return pd.DataFrame(trades)


def run() -> dict[str, Any]:
    variants = (
        ("router_6h_m6_g10_e3", 6.0, 6, 10, 0.03),
        ("router_12h_m6_g10_e3", 12.0, 6, 10, 0.03),
        ("router_12h_m8_g12_e3", 12.0, 8, 12, 0.03),
        ("router_12h_m8_g12_e5", 12.0, 8, 12, 0.05),
    )
    reports = []
    all_trades = []
    for source_name, seconds, orderbook in SOURCES:
        data = load_local_data(seconds, orderbook)
        hours = (data.index.max() - data.index.min()).total_seconds() / 3600.0
        events = sample_events(data)
        source_report = {
            "source": source_name,
            "start": data.index.min(),
            "end": data.index.max(),
            "hours": round(hours, 4),
            "shadowEvents": len(events),
            "variants": {},
        }
        for name, history_hours, min_local, min_global, margin in variants:
            trades = online_route(events, history_hours, min_local, min_global, margin)
            trades["variant"] = name
            trades["source"] = source_name
            source_report["variants"][name] = detailed.metrics(trades, hours)
            if not trades.empty:
                source_report["variants"][name]["byExpert"] = {
                    str(expert): detailed.metrics(group, hours)
                    for expert, group in trades.groupby("expert")
                }
            all_trades.append(trades)
        reports.append(source_report)

    trades_out = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    trades_out.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "method": "Online no-lookahead expert router. Only outcomes settled before the current event enter the rolling reversion-vs-momentum score. Real orders require a 600-second cooldown.",
        "reports": reports,
        "tradesCsv": str(OUT_CSV),
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    result = run()
    print(json.dumps(clean(result), ensure_ascii=False, indent=2))
