from __future__ import annotations

from collections import defaultdict


def execute_signals(
    signals: list[dict],
    *,
    per_strategy_lock: bool = True,
    global_lock_sec: int = 0,
    cooldown_sec: int = 600,
    use_horizon_as_lock: bool = True,
) -> tuple[list[dict], list[dict]]:
    """Apply executable order rules to raw strategy signals.

    The production tablet currently locks by strategy, not globally. Passing a
    non-zero `global_lock_sec` is useful only for research comparisons.
    """

    accepted: list[dict] = []
    rejected: list[dict] = []
    active_until_by_strategy = defaultdict(lambda: None)
    global_until = None
    ordered = sorted(
        signals,
        key=lambda row: (
            row["time"],
            str(row.get("strategy_id") or ""),
            str(row.get("signal") or ""),
        ),
    )
    for row in ordered:
        time = row["time"]
        strategy_id = str(row.get("strategy_id") or "default")
        if global_until is not None and time < global_until:
            skipped = dict(row)
            skipped["skipReason"] = "global_lock"
            rejected.append(skipped)
            continue
        strategy_until = active_until_by_strategy[strategy_id]
        if per_strategy_lock and strategy_until is not None and time < strategy_until:
            skipped = dict(row)
            skipped["skipReason"] = "strategy_lock"
            rejected.append(skipped)
            continue
        trade = dict(row)
        trade["executed"] = True
        accepted.append(trade)
        horizon = int(row.get("horizon_sec") or cooldown_sec)
        lock_sec = max(int(cooldown_sec), horizon if use_horizon_as_lock else 0)
        if per_strategy_lock:
            active_until_by_strategy[strategy_id] = time + _seconds(lock_sec)
        if global_lock_sec:
            global_until = time + _seconds(int(global_lock_sec))
    return accepted, rejected


def apply_signal_gap(signals: list[dict], gap_sec: int) -> list[dict]:
    if gap_sec <= 0:
        return list(sorted(signals, key=lambda row: row["time"]))
    out: list[dict] = []
    last_by_strategy: dict[str, object] = {}
    for row in sorted(signals, key=lambda item: item["time"]):
        strategy_id = str(row.get("strategy_id") or "default")
        last = last_by_strategy.get(strategy_id)
        if last is not None and (row["time"] - last).total_seconds() < gap_sec:
            continue
        out.append(row)
        last_by_strategy[strategy_id] = row["time"]
    return out


def _seconds(value: int):
    import pandas as pd

    return pd.Timedelta(seconds=int(value))
