"""Live data freshness checks for signal generation."""

import pandas as pd

from signal_io import age_status, csv_tail_time
from signal_paths import FUNDING_FILE, HISTORY_1M_FILE, LS_RATIO_FILE, TAKER_FILE


HISTORY_1M_MAX_AGE = pd.Timedelta(minutes=15)
EXTERNAL_RATIO_MAX_AGE = pd.Timedelta(minutes=30)
FUNDING_MAX_AGE = pd.Timedelta(hours=12)
LIVE_1M_MAX_AGE = pd.Timedelta(minutes=3)
MAX_HISTORY_LIVE_GAP = pd.Timedelta(minutes=2)


def build_live_data_health(live_1m):
    now = pd.Timestamp.now(tz="UTC")
    checks = {
        "history_1m": age_status(now, "history_1m", HISTORY_1M_FILE, "open_time", HISTORY_1M_MAX_AGE),
        "taker": age_status(now, "taker", TAKER_FILE, "timestamp", EXTERNAL_RATIO_MAX_AGE),
        "lsratio": age_status(now, "lsratio", LS_RATIO_FILE, "timestamp", EXTERNAL_RATIO_MAX_AGE),
        "funding": age_status(now, "funding", FUNDING_FILE, "fundingTime", FUNDING_MAX_AGE),
    }
    reasons = []
    for item in checks.values():
        reasons.extend(item["reasons"])

    live_info = {"first_time": None, "last_time": None, "age_seconds": None}
    if live_1m is None or len(live_1m) == 0:
        reasons.append("live_1m_missing")
    else:
        live_times = pd.to_datetime(live_1m["open_time"], utc=True).sort_values().reset_index(drop=True)
        live_first = live_times.iloc[0]
        live_last = live_times.iloc[-1]
        live_age = now - live_last
        live_info = {
            "first_time": str(live_first),
            "last_time": str(live_last),
            "age_seconds": round(live_age.total_seconds(), 3),
        }
        if live_age > LIVE_1M_MAX_AGE:
            reasons.append("live_1m_stale")
        gaps = live_times.diff().dropna()
        if len(gaps) and gaps.max() > MAX_HISTORY_LIVE_GAP:
            reasons.append("live_1m_recent_gap")
            live_info["max_gap_seconds"] = round(gaps.max().total_seconds(), 3)
        hist_last = csv_tail_time(HISTORY_1M_FILE, "open_time")
        if hist_last is not None and hist_last + MAX_HISTORY_LIVE_GAP < live_first:
            reasons.append("history_live_gap")
            live_info["history_live_gap_seconds"] = round((live_first - hist_last).total_seconds(), 3)

    unique_reasons = sorted(set(reasons))
    return {
        "blocked": bool(unique_reasons),
        "reasons": unique_reasons,
        "checks": checks,
        "live_1m": live_info,
    }


def signal_data_health_reasons(sig, health):
    reasons = list(health.get("reasons", []))
    if not reasons:
        return []
    model_type = str(sig.get("model_type") or "").lower()
    second_only = model_type.startswith("second_")
    taker_filter = str(sig.get("taker_filter") or "none").lower()
    needs_taker = taker_filter not in ("", "none", "off", "false")
    blocking = []
    for reason in reasons:
        if reason.startswith("taker_"):
            if needs_taker:
                blocking.append(reason)
            continue
        if reason.startswith("lsratio_") or reason.startswith("funding_"):
            continue
        if second_only and reason in ("history_1m_stale", "live_1m_stale", "history_live_gap", "live_1m_missing"):
            continue
        blocking.append(reason)
    return sorted(set(blocking))


def apply_signal_data_health(signals, health):
    if not health.get("blocked"):
        for sig in signals.values():
            if isinstance(sig, dict) and not sig.get("shadow"):
                sig["data_health_blocked"] = False
        return signals
    out = {}
    for strategy_id, sig in signals.items():
        if not isinstance(sig, dict) or sig.get("shadow"):
            out[strategy_id] = sig
            continue
        blocking_reasons = signal_data_health_reasons(sig, health)
        if not blocking_reasons:
            next_sig = dict(sig)
            next_sig["data_health_blocked"] = False
            next_sig["data_health_warning_reasons"] = health.get("reasons", [])
            out[strategy_id] = next_sig
            continue
        blocked = dict(sig)
        blocked["signal"] = None
        blocked["confidence"] = None
        blocked["data_health_blocked"] = True
        blocked["data_health_block_reasons"] = blocking_reasons
        blocked["data_health"] = health
        blocked["blocked_signal"] = sig.get("signal")
        blocked["blocked_confidence"] = sig.get("confidence")
        out[strategy_id] = blocked
    return out
