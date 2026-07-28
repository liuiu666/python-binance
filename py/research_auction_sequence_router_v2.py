"""Chronological test of a persistent auction-response router.

This version deliberately avoids endpoint-only confirmation.  A migration
candidate is latched, then each following second is observed for at most 30
seconds.  A trade is emitted only after price, aggressive flow, depth and
microprice form a persistent reversal or continuation sequence.  Entry is
delayed another five seconds to approximate the measured live order path.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from multiscale_phase_gate_core import MultiscalePhaseGateConfig, build_snapshots  # noqa: E402
from research_auction_confirmation_router_v1 import load_forward_live  # noqa: E402
from research_multiscale_phase_gate import load_live_parity_sources  # noqa: E402
from research_normal_shape_1m_10m import clean  # noqa: E402
from run_multi_normal_hf_stable_backtest import LoadedSource, metrics, price_at_or_after, utc  # noqa: E402


OBSERVE_SEC = 30
EXECUTION_SEC = 5
HORIZON_SEC = 600
GAP_SEC = 600
OUT_JSON = ROOT / "tmp" / "auction_sequence_router_v2_latest.json"
OUT_CSV = ROOT / "tmp" / "auction_sequence_router_v2_trades.csv"


def _flow_ratio(frame: pd.DataFrame) -> float:
    buy = float(frame["buy_qty"].fillna(0.0).sum())
    sell = float(frame["sell_qty"].fillna(0.0).sum())
    total = buy + sell
    return (buy - sell) / total if total > 0.0 else 0.0


def _persistent(values: list[bool], required: int = 4) -> bool:
    return len(values) >= 5 and sum(values[-5:]) >= required


def sequence_decision(data: pd.DataFrame, detected: pd.Timestamp, crowd: int) -> dict[str, Any] | None:
    start = int(data.index.searchsorted(detected))
    end_time = detected + pd.Timedelta(seconds=OBSERVE_SEC)
    end = int(data.index.searchsorted(end_time))
    if start >= len(data) or end >= len(data):
        return None
    if abs((data.index[start] - detected).total_seconds()) > 1:
        return None

    pre = data["close"].iloc[max(0, start - 59):start + 1].astype(float)
    noise = float((pre.pct_change().dropna() * 10000.0).std(ddof=0)) * math.sqrt(5.0)
    noise5 = max(0.5, noise if math.isfinite(noise) else 0.5)
    initial = float(data["close"].iloc[start])
    if initial <= 0.0:
        return None

    progress_path: list[float] = []
    flow_aligned_path: list[float] = []
    book_aligned_path: list[float] = []
    micro_aligned_path: list[float] = []
    peak_progress = -math.inf

    for pos in range(start + 5, min(end, len(data) - 1) + 1):
        row = data.iloc[pos]
        current = float(row["close"])
        progress = crowd * (current / initial - 1.0) * 10000.0
        peak_progress = max(peak_progress, progress)
        flow = crowd * _flow_ratio(data.iloc[pos - 4:pos + 1])
        book = crowd * float(row.get("imbalance_20", np.nan))
        micro = crowd * float(row.get("microprice_edge_bps", np.nan))
        if not all(math.isfinite(x) for x in (progress, flow, book, micro)):
            continue
        progress_path.append(progress)
        flow_aligned_path.append(flow)
        book_aligned_path.append(book)
        micro_aligned_path.append(micro)

        opposite_votes = [
            flow_aligned_path[i] < 0.0
            and book_aligned_path[i] < 0.0
            and micro_aligned_path[i] < 0.0
            for i in range(len(progress_path))
        ]
        aligned_votes = [
            flow_aligned_path[i] > 0.10
            and book_aligned_path[i] > 0.05
            and micro_aligned_path[i] > 0.0
            for i in range(len(progress_path))
        ]
        price_opposite = len(progress_path) >= 5 and progress_path[-1] < progress_path[-5]
        price_aligned = len(progress_path) >= 5 and progress_path[-1] > progress_path[-5]
        rejected = peak_progress - progress >= noise5 and progress <= 0.5 * noise5
        sustained_break = (
            progress >= 2.0 * noise5
            and min(progress_path[-5:]) >= noise5
            and peak_progress - progress <= noise5
        )

        action = None
        if rejected and price_opposite and _persistent(opposite_votes):
            action = "fade"
        elif sustained_break and price_aligned and _persistent(aligned_votes):
            action = "follow"
        if action is None:
            continue

        signal_direction = -crowd if action == "fade" else crowd
        return {
            "confirm_time": data.index[pos],
            "signal": "UP" if signal_direction > 0 else "DOWN",
            "action": action,
            "noise5_bps": noise5,
            "confirm_after_sec": float((data.index[pos] - detected).total_seconds()),
            "progress_bps": progress,
            "peak_progress_bps": peak_progress,
            "rejection_bps": peak_progress - progress,
            "flow5_aligned": flow,
            "imbalance_aligned": book,
            "micro_aligned_bps": micro,
        }
    return None


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
    last_entry: pd.Timestamp | None = None
    for candidate in candidates.to_dict("records"):
        detected = utc(candidate["detected_time"])
        crowd = 1 if candidate["crowd_direction"] == "UP" else -1
        decision = sequence_decision(data, detected, crowd)
        if decision is None:
            counts["skip"] += 1
            continue
        counts[decision["action"]] += 1
        execution_time = utc(decision["confirm_time"]) + pd.Timedelta(seconds=EXECUTION_SEC)
        if last_entry is not None and (execution_time - last_entry).total_seconds() < GAP_SEC:
            continue
        entry = price_at_or_after(close, execution_time)
        settle = price_at_or_after(close, execution_time + pd.Timedelta(seconds=HORIZON_SEC))
        if entry is None or settle is None:
            continue
        direction = 1.0 if decision["signal"] == "UP" else -1.0
        outcome = direction * (settle[1] / entry[1] - 1.0) * 10000.0
        rows.append({
            "source": source.spec.name,
            "role": source.spec.role,
            "detected_time": detected,
            "confirm_time": decision["confirm_time"],
            "entry_time": entry[0],
            "settle_time": settle[0],
            "candidate_phase": candidate.get("phase"),
            "crowd_direction": candidate.get("crowd_direction"),
            "signal": decision["signal"],
            "action": decision["action"],
            "entry": entry[1],
            "settle": settle[1],
            "signed_outcome_bps": outcome,
            "won": bool(outcome > 0.0),
            **{k: v for k, v in decision.items() if k not in {"signal", "action", "confirm_time"}},
        })
        last_entry = entry[0]
    return pd.DataFrame(rows), counts


def _summary(frame: pd.DataFrame, hours: float) -> dict[str, Any]:
    result = metrics(frame, hours)
    result["byAction"] = {
        str(name): metrics(group, hours) for name, group in frame.groupby("action")
    } if not frame.empty else {}
    return result


def main() -> None:
    sources = [*load_live_parity_sources(), load_forward_live()]
    frames: list[pd.DataFrame] = []
    dispositions: dict[str, dict[str, int]] = {}
    for source in sources:
        frame, disposition = replay_source(source)
        frames.append(frame)
        dispositions[source.spec.name] = disposition
    trades = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    report = {
        "method": {
            "parameterSearch": False,
            "observeSec": OBSERVE_SEC,
            "executionDelaySec": EXECUTION_SEC,
            "settlementFromActualEntrySec": HORIZON_SEC,
            "rule": "Persistent five-second price/flow/book/microprice sequence; endpoint snapshots are not accepted.",
        },
        "candidateDisposition": dispositions,
        "roles": {
            role: _summary(group, sum(s.hours for s in sources if s.spec.role == role))
            for role, group in trades.groupby("role")
        } if not trades.empty else {},
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(json.dumps(clean(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
