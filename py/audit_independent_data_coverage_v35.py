"""V35 audit of local independent information available for further research.

This is a read-only inventory.  It never downloads data and never changes a
strategy or runtime configuration.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "tmp" / "v35_independent_data_coverage_audit_20260730.json"
MIN_LONGITUDINAL_DAYS = 730.0

CSV_SOURCES = (
    ("taker_buy_sell", ROOT / "data/server_latest/btcusdt_taker.csv", "timestamp", "5m"),
    ("account_long_short", ROOT / "data/server_latest/btcusdt_lsratio.csv", "timestamp", "5m"),
    ("global_long_short", ROOT / "data/server_latest/btcusdt_global_lsratio.csv", "timestamp", "5m"),
    ("top_account_long_short", ROOT / "data/server_latest/btcusdt_top_account_lsratio.csv", "timestamp", "5m"),
    ("open_interest", ROOT / "data/server_latest/btcusdt_open_interest.csv", "timestamp", "5m"),
    ("funding", ROOT / "data/server_latest/btcusdt_funding.csv", "fundingTime", "8h"),
    ("orderbook_1s", ROOT / "data/server_latest/btcusdt_orderbook_1s.csv", "timestamp", "1s"),
)


def _clean_text(value: str) -> str:
    return value.replace("\x00", "").strip()


def csv_time_bounds(path: Path, timestamp_column: str) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        return {"exists": False, "rows": 0, "start": None, "end": None}
    first_timestamp: pd.Timestamp | None = None
    last_timestamp: pd.Timestamp | None = None
    valid_rows = 0
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.DictReader((_clean_text(line) for line in handle))
        for row in reader:
            raw = _clean_text(str(row.get(timestamp_column, "")))
            if not raw:
                continue
            timestamp = pd.to_datetime(raw, utc=True, errors="coerce")
            if pd.isna(timestamp):
                continue
            parsed = pd.Timestamp(timestamp)
            first_timestamp = parsed if first_timestamp is None else min(first_timestamp, parsed)
            last_timestamp = parsed if last_timestamp is None else max(last_timestamp, parsed)
            valid_rows += 1
    duration_days = (
        (last_timestamp - first_timestamp).total_seconds() / 86_400.0
        if first_timestamp is not None and last_timestamp is not None
        else 0.0
    )
    return {
        "exists": True,
        "bytes": int(path.stat().st_size),
        "rows": int(valid_rows),
        "start": first_timestamp,
        "end": last_timestamp,
        "durationDays": round(float(duration_days), 4),
    }


def qualifies_longitudinal(source: dict[str, Any]) -> bool:
    return bool(
        source.get("exists")
        and float(source.get("durationDays") or 0.0) >= MIN_LONGITUDINAL_DAYS
        and source.get("sampling") == "continuous_or_regular"
        and not source.get("targetSelected", False)
    )


def aggtrade_archive_audit(cache: Path) -> dict[str, Any]:
    pattern = re.compile(r"(\d{4}-\d{2}-\d{2})\.zip$")
    dates = sorted(
        {
            pd.Timestamp(match.group(1), tz="UTC")
            for path in cache.glob("*.zip")
            if (match := pattern.search(path.name)) is not None
        }
    )
    if not dates:
        return {
            "exists": cache.exists(),
            "uniqueDates": 0,
            "start": None,
            "end": None,
            "calendarCoveragePct": 0.0,
            "targetSelected": True,
            "sampling": "signal_targeted_days",
        }
    span_days = int((dates[-1] - dates[0]).days) + 1
    return {
        "exists": True,
        "zipFiles": int(len(list(cache.glob("*.zip")))),
        "uniqueDates": int(len(dates)),
        "start": dates[0],
        "end": dates[-1],
        "durationDays": round(float(span_days - 1), 4),
        "calendarSpanDays": span_days,
        "calendarCoveragePct": round(100.0 * len(dates) / span_days, 4),
        "targetSelected": True,
        "sampling": "signal_targeted_days",
        "note": "archives were downloaded around previously selected signals, not as a continuous market history",
    }


def run() -> dict[str, Any]:
    sources: dict[str, Any] = {}
    for name, path, timestamp_column, cadence in CSV_SOURCES:
        audit = csv_time_bounds(path, timestamp_column)
        audit.update(
            {
                "path": str(path.resolve()),
                "cadence": cadence,
                "sampling": "continuous_or_regular",
                "targetSelected": False,
            }
        )
        audit["eligible"] = qualifies_longitudinal(audit)
        sources[name] = audit

    aggtrades = aggtrade_archive_audit(ROOT / "tmp/v23_aggtrades_daily_cache")
    aggtrades["path"] = str((ROOT / "tmp/v23_aggtrades_daily_cache").resolve())
    aggtrades["eligible"] = qualifies_longitudinal(aggtrades)
    sources["archived_aggtrades"] = aggtrades

    cross_market_files = sorted(
        str(path.resolve())
        for path in (ROOT / "data").rglob("*")
        if path.is_file() and re.search(r"(?i)(eth|bnb|sol|spot.*futures|futures.*spot)", path.name)
    )
    eligible = [name for name, value in sources.items() if value.get("eligible")]
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "status": "V35_LOCAL_INDEPENDENT_INFORMATION_COVERAGE_AUDIT",
        "safety": {
            "readOnly": True,
            "networkUsed": False,
            "downloadPerformed": False,
            "deploymentPerformed": False,
            "onlineConfigurationChanged": False,
            "realTradingAllowed": False,
        },
        "qualification": {
            "minimumDurationDays": MIN_LONGITUDINAL_DAYS,
            "requiresContinuousOrRegularSampling": True,
            "rejectsSignalTargetedSampling": True,
            "reason": "must support multi-year chronological walk-forward without conditioning on already selected signals",
        },
        "sources": sources,
        "crossMarketFiles": cross_market_files,
        "eligibleIndependentDatasets": eligible,
        "decision": {
            "eligible": bool(eligible or cross_market_files),
            "action": "new_information_research_available"
            if eligible or cross_market_files
            else "no_eligible_local_independent_information",
            "blocker": (
                None
                if eligible or cross_market_files
                else "local independent context is too short or signal-targeted; further same-source OHLCV tuning would add selection bias"
            ),
        },
    }
    OUT_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return report


def main() -> int:
    report = run()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
