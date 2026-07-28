"""Test a causal post-candidate auction confirmation router.

The multi-scale migration candidate is observed at a completed minute. The
router waits ten seconds, then uses only those newly observed seconds to choose
fade, follow, or no trade. Rules are fixed from auction mechanics; this script
does not search thresholds.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from multiscale_phase_gate_core import MultiscalePhaseGateConfig, build_snapshots  # noqa: E402
from research_multiscale_phase_gate import load_live_parity_sources  # noqa: E402
from research_normal_liquidity_orderbook import read_orderbook  # noqa: E402
from research_normal_shape_1m_10m import clean  # noqa: E402
from run_multi_normal_hf_stable_backtest import LoadedSource, SourceSpec, metrics, price_at_or_after, utc  # noqa: E402
from second_backtest.data import load_second_bars  # noqa: E402


CONFIRM_SEC = 10
HORIZON_SEC = 600
GAP_SEC = 600
OUT_JSON = ROOT / "tmp" / "auction_confirmation_router_v1_latest.json"
OUT_CSV = ROOT / "tmp" / "auction_confirmation_router_v1_trades.csv"


def flow_ratio(data: pd.DataFrame) -> float:
    buy = float(data["buy_qty"].fillna(0.0).sum())
    sell = float(data["sell_qty"].fillna(0.0).sum())
    total = buy + sell
    return (buy - sell) / total if total > 0.0 else 0.0


def signed(value: float, direction: int) -> float:
    return float(value) * direction


def auction_decision(
    data: pd.DataFrame,
    detected: pd.Timestamp,
    crowd_direction: int,
) -> dict[str, Any] | None:
    start_pos = int(data.index.searchsorted(detected))
    confirm_time = detected + pd.Timedelta(seconds=CONFIRM_SEC)
    end_pos = int(data.index.searchsorted(confirm_time))
    if start_pos >= len(data) or end_pos >= len(data):
        return None
    if abs((data.index[start_pos] - detected).total_seconds()) > 1 or abs((data.index[end_pos] - confirm_time).total_seconds()) > 1:
        return None
    recent_start = max(0, start_pos - 59)
    recent_close = data["close"].iloc[recent_start:start_pos + 1].astype(float)
    one_sec_returns = recent_close.pct_change().dropna() * 10000.0
    noise10_bps = max(0.5, float(one_sec_returns.std(ddof=0)) * math.sqrt(CONFIRM_SEC))
    path = data.iloc[start_pos:end_pos + 1]
    prices = path["close"].astype(float)
    start_price = float(prices.iloc[0])
    current_price = float(prices.iloc[-1])
    if start_price <= 0.0 or current_price <= 0.0:
        return None
    progress_bps = crowd_direction * (current_price / start_price - 1.0) * 10000.0
    extreme = float(prices.max()) if crowd_direction > 0 else float(prices.min())
    rejection_bps = -crowd_direction * (current_price / extreme - 1.0) * 10000.0
    flow10 = crowd_direction * flow_ratio(path)
    row = data.iloc[end_pos]
    imbalance = crowd_direction * float(row.get("imbalance_20", np.nan))
    micro = crowd_direction * float(row.get("microprice_edge_bps", np.nan))
    if not all(math.isfinite(value) for value in (progress_bps, rejection_bps, flow10, imbalance, micro, noise10_bps)):
        return None

    stalled_or_rejected = progress_bps <= 0.25 * noise10_bps or rejection_bps >= 0.75 * noise10_bps
    fade_confirmed = stalled_or_rejected and flow10 <= 0.0 and imbalance <= 0.0 and micro <= 0.0
    follow_confirmed = (
        progress_bps >= noise10_bps
        and flow10 >= 0.10
        and imbalance >= 0.05
        and micro >= 0.0
    )
    action = "fade" if fade_confirmed else "follow" if follow_confirmed else "skip"
    signal = None
    if action == "fade":
        signal = "DOWN" if crowd_direction > 0 else "UP"
    elif action == "follow":
        signal = "UP" if crowd_direction > 0 else "DOWN"
    return {
        "confirm_time": data.index[end_pos],
        "signal": signal,
        "action": action,
        "noise10_bps": noise10_bps,
        "progress_bps": progress_bps,
        "rejection_bps": rejection_bps,
        "flow10_aligned": flow10,
        "imbalance_aligned": imbalance,
        "micro_aligned_bps": micro,
    }


def replay_source(source: LoadedSource) -> tuple[pd.DataFrame, dict[str, int]]:
    data = source.data
    snapshots = build_snapshots(data, MultiscalePhaseGateConfig())
    if snapshots.empty:
        return pd.DataFrame(), {"candidates": 0, "fade": 0, "follow": 0, "skip": 0}
    snapshots["detected_time"] = pd.to_datetime(snapshots["detected_time"], utc=True)
    candidates = snapshots[
        (snapshots["detected_time"] >= source.test_start)
        & (snapshots["detected_time"] <= source.test_end)
        & snapshots["crowd_direction"].isin(["UP", "DOWN"])
    ]
    counts = {"candidates": len(candidates), "fade": 0, "follow": 0, "skip": 0}
    close = data["close"].astype(float)
    rows: list[dict[str, Any]] = []
    last_emit: pd.Timestamp | None = None
    for candidate in candidates.to_dict("records"):
        detected = utc(candidate["detected_time"])
        crowd = 1 if candidate["crowd_direction"] == "UP" else -1
        decision = auction_decision(data, detected, crowd)
        if decision is None:
            continue
        counts[decision["action"]] += 1
        signal = decision["signal"]
        if signal not in {"UP", "DOWN"}:
            continue
        confirm_time = utc(decision["confirm_time"])
        if last_emit is not None and (confirm_time - last_emit).total_seconds() < GAP_SEC:
            continue
        entry = price_at_or_after(close, confirm_time)
        settle = price_at_or_after(close, confirm_time + pd.Timedelta(seconds=HORIZON_SEC))
        if entry is None or settle is None:
            continue
        direction = 1.0 if signal == "UP" else -1.0
        outcome = (settle[1] / entry[1] - 1.0) * 10000.0 * direction
        rows.append({
            "source": source.spec.name,
            "role": source.spec.role,
            "detected_time": detected,
            "entry_time": entry[0],
            "settle_time": settle[0],
            "candidate_phase": candidate.get("phase"),
            "crowd_direction": candidate.get("crowd_direction"),
            "signal": signal,
            "action": decision["action"],
            "entry": entry[1],
            "settle": settle[1],
            "signed_outcome_bps": outcome,
            "won": bool(outcome > 0.0),
            **{key: value for key, value in decision.items() if key not in {"signal", "confirm_time", "action"}},
        })
        last_emit = confirm_time
    return pd.DataFrame(rows), counts


def load_forward_live() -> LoadedSource:
    folder = ROOT / "tmp" / "phase_live_audit_20260713"
    seconds = folder / "btcusdt_1s_trades.csv"
    orderbook = folder / "btcusdt_orderbook_1s.csv"
    bars = load_second_bars(seconds, include_shards=False)
    data = bars.join(read_orderbook(orderbook, bars.index), how="left").sort_index()
    spec = SourceSpec(
        "forward_live",
        seconds,
        orderbook,
        start="2026-07-12T15:20:00Z",
        role="forward_live",
    )
    start = utc(spec.start)
    end = utc(data.index.max())
    return LoadedSource(spec, data, pd.DataFrame(), start, end, (end - start).total_seconds() / 3600.0)


def summarize(frame: pd.DataFrame, hours: float) -> dict[str, Any]:
    result = metrics(frame, hours)
    result["byAction"] = {
        str(name): metrics(group, hours) for name, group in frame.groupby("action")
    } if not frame.empty else {}
    return result


def main() -> None:
    sources = [*load_live_parity_sources(), load_forward_live()]
    frames: list[pd.DataFrame] = []
    counts: dict[str, dict[str, int]] = {}
    for source in sources:
        frame, source_counts = replay_source(source)
        frames.append(frame)
        counts[source.spec.name] = source_counts
    trades = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    report = {
        "method": {
            "confirmSec": CONFIRM_SEC,
            "parameterSearch": False,
            "fade": "Stalled/rejected progress plus opposite 10s flow, depth imbalance and microprice.",
            "follow": "Noise-adjusted progress plus aligned 10s flow, depth imbalance and microprice.",
            "otherwise": "Skip.",
        },
        "candidateDisposition": counts,
        "roles": {
            role: summarize(group, sum(source.hours for source in sources if source.spec.role == role))
            for role, group in trades.groupby("role")
        } if not trades.empty else {},
        "forwardLive": summarize(
            trades[trades["role"] == "forward_live"],
            sum(source.hours for source in sources if source.spec.role == "forward_live"),
        ),
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(json.dumps(clean(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
