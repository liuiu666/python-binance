"""Build the authoritative local research dataset and execution audit."""

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

from auction_event_dataset import FEATURE_COLUMNS, ROBUSTNESS_DELAYS, combine_event_samples  # noqa: E402
from research_auction_confirmation_router_v1 import load_forward_live  # noqa: E402
from research_multiscale_phase_gate import load_live_parity_sources  # noqa: E402
from research_normal_shape_1m_10m import clean  # noqa: E402


OUT_EVENTS = ROOT / "tmp" / "unified_auction_events_10m.csv"
OUT_ORDERS = ROOT / "tmp" / "unified_real_orders.csv"
OUT_REPORT = ROOT / "tmp" / "unified_auction_dataset_report.json"
HISTORY_FILES = (ROOT / "tmp" / "runtime_0.json", ROOT / "tmp" / "runtime_1.json")


def _timestamp_ms(value: Any) -> float:
    try:
        if value is None:
            return float("nan")
        if isinstance(value, (int, float)):
            return float(value)
        return float(pd.Timestamp(value).timestamp() * 1000.0)
    except (TypeError, ValueError):
        return float("nan")


def load_real_orders() -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in HISTORY_FILES:
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        for order in [*payload.get("recent", []), *payload.get("active", [])]:
            order_id = str(order.get("id") or "")
            if not order_id or order_id in seen:
                continue
            seen.add(order_id)
            signal_ms = _timestamp_ms(order.get("actionableTime") or order.get("signalTime"))
            open_ms = _timestamp_ms(order.get("openTime"))
            rows.append({
                "id": order_id,
                "strategy_id": order.get("strategyId"),
                "direction": order.get("direction"),
                "amount": order.get("amount"),
                "signal_time": order.get("signalTime"),
                "actionable_time": order.get("actionableTime"),
                "open_time": pd.to_datetime(order.get("openTime"), unit="ms", utc=True, errors="coerce"),
                "settle_time": pd.to_datetime(order.get("settleTime"), unit="ms", utc=True, errors="coerce"),
                "open_price": order.get("openPrice"),
                "close_price": order.get("closePrice"),
                "status": order.get("status"),
                "pnl": order.get("pnl"),
                "reason": order.get("reason") or order.get("decisionReason"),
                "signal_to_open_ms": open_ms - signal_ms if math.isfinite(signal_ms) and math.isfinite(open_ms) else np.nan,
            })
    return pd.DataFrame(rows).sort_values("open_time") if rows else pd.DataFrame()


def quantiles(values: pd.Series) -> dict[str, float | None]:
    clean_values = pd.to_numeric(values, errors="coerce").dropna()
    if clean_values.empty:
        return {"count": 0, "p50": None, "p90": None, "max": None}
    return {
        "count": int(len(clean_values)),
        "p50": round(float(clean_values.quantile(0.50)), 2),
        "p90": round(float(clean_values.quantile(0.90)), 2),
        "max": round(float(clean_values.max()), 2),
    }


def main() -> None:
    sources = [*load_live_parity_sources(), load_forward_live()]
    events = combine_event_samples(sources)
    events["validation_status"] = np.where(
        events["role"] == "history", "development", "reused_validation_not_untouched"
    )
    orders = load_real_orders()
    events.to_csv(OUT_EVENTS, index=False, encoding="utf-8-sig")
    orders.to_csv(OUT_ORDERS, index=False, encoding="utf-8-sig")
    report = {
        "method": {
            "spacingSec": 600,
            "entryDelaySec": 5,
            "robustnessEntryDelaysSec": ROBUSTNESS_DELAYS,
            "horizonFromEntrySec": 600,
            "causalFeatures": True,
            "nonOverlappingLabels": True,
            "featureColumns": FEATURE_COLUMNS,
            "warning": "All non-history periods have already been inspected and cannot be called untouched again.",
        },
        "events": {
            "rows": len(events),
            "start": events["time"].min() if not events.empty else None,
            "end": events["time"].max() if not events.empty else None,
            "byRole": events.groupby("role").size().to_dict() if not events.empty else {},
            "bySource": events.groupby("source").size().to_dict() if not events.empty else {},
        },
        "realOrders": {
            "rows": len(orders),
            "settled": int(orders["status"].isin(["won", "lost", "tie"]).sum()) if not orders.empty else 0,
            "wins": int((orders["status"] == "won").sum()) if not orders.empty else 0,
            "losses": int((orders["status"] == "lost").sum()) if not orders.empty else 0,
            "pnlU": round(float(pd.to_numeric(orders["pnl"], errors="coerce").fillna(0.0).sum()), 2) if not orders.empty else 0.0,
            "signalToOpenMs": quantiles(orders["signal_to_open_ms"]) if not orders.empty else quantiles(pd.Series(dtype=float)),
        },
    }
    OUT_REPORT.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(clean(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
