"""Hard safety contracts for offline V14 candidate research.

This module intentionally contains no deployment or network code.  It provides
two small primitives that research scripts can reuse without gaining any path
to real trading:

* explicit, validated shadow-only candidate metadata; and
* prefix-only replay, so a candidate evaluator never receives rows after the
  timestamp it is deciding.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pandas as pd


V14_FAMILY = "V14"

_SAFETY_FIELDS: dict[str, Any] = {
    "researchOnly": True,
    "observationMode": "shadow",
    "shadowOnly": True,
    "tradeEnabled": False,
    "realTradingEnabled": False,
    "realTradingAllowed": False,
    "autoTrade": False,
    "deployable": False,
}


class _FrozenMetadata(dict[str, Any]):
    """JSON-compatible dict that cannot be changed after construction."""

    @staticmethod
    def _immutable(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("V14 research metadata is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


def shadow_only_candidate_metadata(
    candidate_id: str,
    label: str,
    *,
    family: str = V14_FAMILY,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build metadata for a research candidate without unsafe overrides.

    Safety fields cannot be supplied through ``extra`` even when the proposed
    value appears harmless.  Keeping one authoritative source prevents later
    callers from accidentally turning a research report into deployment
    metadata through a dictionary merge.
    """

    candidate = str(candidate_id).strip()
    candidate_label = str(label).strip()
    candidate_family = str(family).strip()
    if not candidate or not candidate_label or not candidate_family:
        raise ValueError("candidate_id, label, and family must be non-empty")

    additions = dict(extra or {})
    collisions = sorted(set(additions).intersection(_SAFETY_FIELDS))
    if collisions:
        raise ValueError(f"research safety fields cannot be overridden: {collisions!r}")

    metadata = _FrozenMetadata({
        "candidateId": candidate,
        "label": candidate_label,
        "family": candidate_family,
        "status": "research_shadow_only",
        **additions,
        **_SAFETY_FIELDS,
    })
    assert_shadow_only_candidate(metadata)
    return metadata


def assert_shadow_only_candidate(metadata: Mapping[str, Any]) -> None:
    """Reject metadata that could be interpreted as live or deployable."""

    mismatches = {
        key: {"required": required, "actual": metadata.get(key)}
        for key, required in _SAFETY_FIELDS.items()
        if metadata.get(key) != required
    }
    if metadata.get("status") != "research_shadow_only":
        mismatches["status"] = {
            "required": "research_shadow_only",
            "actual": metadata.get("status"),
        }
    if mismatches:
        raise ValueError(f"unsafe V14 research metadata: {mismatches!r}")


def normalize_causal_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate and copy a deterministic UTC-indexed research timeline."""

    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError("frame must be a non-empty pandas DataFrame")
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("frame must use a DatetimeIndex")

    normalized = frame.copy(deep=True)
    index = pd.to_datetime(normalized.index, utc=True, errors="coerce")
    if index.isna().any():
        raise ValueError("frame index contains invalid timestamps")
    normalized.index = pd.DatetimeIndex(index, name=frame.index.name or "time")
    if not normalized.index.is_monotonic_increasing:
        raise ValueError("frame index must be monotonic increasing")
    if not normalized.index.is_unique:
        raise ValueError("frame index must be unique before causal replay")
    return normalized


def replay_prefix_only(
    frame: pd.DataFrame,
    evaluator: Callable[[pd.DataFrame], Mapping[str, Any] | None],
    *,
    start_pos: int = 0,
    end_pos: int | None = None,
) -> pd.DataFrame:
    """Evaluate each timestamp with a frame ending exactly at that timestamp.

    The evaluator receives a deep copy of ``frame.iloc[:position + 1]``.  It
    therefore cannot inspect settlement rows or any other future observation.
    Outcome attachment belongs in the separate validation layer.
    """

    data = normalize_causal_frame(frame)
    start = int(start_pos)
    end = len(data) - 1 if end_pos is None else int(end_pos)
    if start < 0 or end < start or end >= len(data):
        raise ValueError("invalid causal replay position range")

    rows: list[dict[str, Any]] = []
    for position in range(start, end + 1):
        as_of = pd.Timestamp(data.index[position])
        prefix = data.iloc[: position + 1].copy(deep=True)
        decision = evaluator(prefix)
        if decision is None:
            continue
        if not isinstance(decision, Mapping):
            raise TypeError("causal evaluator must return a mapping or None")
        row = dict(decision)
        decision_time = pd.Timestamp(row.get("time", as_of))
        if decision_time.tzinfo is None:
            decision_time = decision_time.tz_localize("UTC")
        else:
            decision_time = decision_time.tz_convert("UTC")
        if decision_time > as_of:
            raise ValueError("causal evaluator returned a future decision timestamp")
        row["time"] = decision_time
        row["as_of"] = as_of
        row["history_rows"] = len(prefix)
        rows.append(row)
    return pd.DataFrame(rows)
