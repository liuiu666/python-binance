"""Persistent state helpers for signal windows and audit de-duplication."""

import json
import os

import pandas as pd

from signal_io import read_json_file, tail_jsonl_rows, write_json_atomic
from signal_paths import SIGNAL_AUDIT_FILE, SIGNAL_STATE_FILE


def load_strategy_window_state_from_audit(strategy_id):
    for row in reversed(tail_jsonl_rows(SIGNAL_AUDIT_FILE, limit=3000)):
        if row.get("event") != "signal_snapshot":
            continue
        if str(row.get("strategy_id")) != str(strategy_id):
            continue
        if not row.get("signal") and not row.get("quality_v2_veto"):
            continue
        raw_time = row.get("time")
        if not raw_time:
            continue
        item = {
            "strategy_id": str(strategy_id),
            "signal": row.get("blocked_signal") or row.get("signal"),
            "reason": row.get("reason"),
            "raw_signal": row.get("raw_signal"),
            "raw_reason": row.get("raw_reason"),
            "quality_v2_veto": bool(row.get("quality_v2_veto")),
            "min_gap_sec": row.get("min_gap_sec"),
            "last_emit_time": raw_time,
            "source": "signal_audit",
        }
        try:
            ts = pd.Timestamp(raw_time)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            return ts.tz_convert("UTC"), item
        except Exception:
            continue
    return None, {}


def load_strategy_window_state(strategy_id):
    data = read_json_file(SIGNAL_STATE_FILE, {}) or {}
    item = (data.get("strategy_windows") or {}).get(str(strategy_id)) or {}
    raw_time = item.get("last_emit_time")
    if not raw_time:
        return load_strategy_window_state_from_audit(strategy_id)
    try:
        ts = pd.Timestamp(raw_time)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.tz_convert("UTC"), item
    except Exception:
        return None, item


def persist_strategy_window_state(strategy_id, signal_time, payload=None):
    data = read_json_file(SIGNAL_STATE_FILE, {}) or {}
    windows = data.get("strategy_windows")
    if not isinstance(windows, dict):
        windows = {}
    ts = pd.Timestamp(signal_time)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    ts = ts.tz_convert("UTC")
    windows[str(strategy_id)] = {
        **(payload or {}),
        "strategy_id": str(strategy_id),
        "last_emit_time": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated_at": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    data["strategy_windows"] = windows
    write_json_atomic(SIGNAL_STATE_FILE, data)


def load_strategy_runtime_state(strategy_id):
    data = read_json_file(SIGNAL_STATE_FILE, {}) or {}
    runtime = data.get("strategy_runtime")
    if not isinstance(runtime, dict):
        return {}
    item = runtime.get(str(strategy_id))
    return item if isinstance(item, dict) else {}


def persist_strategy_runtime_state(strategy_id, payload=None):
    data = read_json_file(SIGNAL_STATE_FILE, {}) or {}
    runtime = data.get("strategy_runtime")
    if not isinstance(runtime, dict):
        runtime = {}
    runtime[str(strategy_id)] = {
        **(payload or {}),
        "strategy_id": str(strategy_id),
        "updated_at": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    data["strategy_runtime"] = runtime
    write_json_atomic(SIGNAL_STATE_FILE, data)


def load_audit_keys(path, limit=20000):
    if not os.path.exists(path):
        return set()
    keys = set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        for line in lines:
            try:
                row = json.loads(line)
            except Exception:
                continue
            event = row.get("event")
            if (
                event in ("signal_snapshot", "shadow_candidate")
                and row.get("strategy_id")
                and row.get("time")
                and row.get("actionable_time")
            ):
                keys.add(f"{event}|{row['strategy_id']}|{row['time']}")
    except Exception:
        return set()
    return keys
