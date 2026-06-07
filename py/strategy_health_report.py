"""Operational health checks for the BTC binary-options system."""
import json
import os
import time
from datetime import datetime, timezone

OUT = "E:/codex/data"
FILES = {
    "signals": os.path.join(OUT, "live_signals.json"),
    "price": os.path.join(OUT, "current_price.json"),
    "signal_audit": os.path.join(OUT, "signal_audit.jsonl"),
    "trade_audit": os.path.join(OUT, "trade_audit.jsonl"),
    "price_ticks": os.path.join(OUT, "price_ticks.jsonl"),
}
REPORT_FILE = os.path.join(OUT, "strategy_health_report.json")


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def tail_jsonl(path, limit=20):
    if not os.path.exists(path):
        return []
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        for line in lines:
            try:
                rows.append(json.loads(line))
            except Exception:
                rows.append({"raw": line.strip()})
    except Exception:
        return []
    return rows


def parse_ms(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def age_ms(ts):
    if ts is None:
        return None
    return int(time.time() * 1000) - int(ts)


def status_for_age(age, warn_ms, fail_ms):
    if age is None:
        return "fail"
    if age > fail_ms:
        return "fail"
    if age > warn_ms:
        return "warn"
    return "ok"


def main():
    now = int(time.time() * 1000)
    signals = read_json(FILES["signals"], {})
    price = read_json(FILES["price"], {})
    signal_audit_tail = tail_jsonl(FILES["signal_audit"], 50)
    trade_audit_tail = tail_jsonl(FILES["trade_audit"], 50)
    price_tick_tail = tail_jsonl(FILES["price_ticks"], 5)

    price_age = age_ms(parse_ms(price.get("time")))
    price_tick_age = age_ms(parse_ms(price_tick_tail[-1].get("time") if price_tick_tail else None))

    strategy_status = {}
    for strategy_id, sig in signals.items():
        actionable = parse_ms(sig.get("actionable_time") or sig.get("candle_close_time") or sig.get("time"))
        action_age = age_ms(actionable)
        strategy_status[strategy_id] = {
            "signal": sig.get("signal"),
            "time": sig.get("time"),
            "actionable_time": sig.get("actionable_time"),
            "actionable_age_ms": action_age,
            "freshness": status_for_age(action_age, 7 * 60 * 1000, 12 * 60 * 1000),
            "reason": {
                "agree": sig.get("agree"),
                "high_conf": sig.get("high_conf"),
                "rsi_extreme": sig.get("rsi_extreme"),
                "vol_ok": sig.get("vol_ok"),
                "session_ok": sig.get("session_ok"),
                "skip_hours_utc": sig.get("skip_hours_utc"),
            },
        }

    latest_signal_audit_ms = None
    for row in reversed(signal_audit_tail):
        latest_signal_audit_ms = parse_ms(row.get("serverTime"))
        if latest_signal_audit_ms:
            break

    autojs_event_names = {
        "autojs_loader_start", "autojs_loader_exec", "autojs_loader_error",
        "autojs_start", "autojs_heartbeat", "signal_tradeable", "signal_skipped",
        "order_attempt", "order_abort", "order_done",
    }
    autojs_events = [r for r in trade_audit_tail if r.get("event") in autojs_event_names]
    tablet_page_pings = [
        r for r in trade_audit_tail
        if r.get("event") == "tablet_page_ping" and r.get("source") != "codex_local_probe"
    ]
    order_done = [r for r in autojs_events if r.get("event") == "order_done"]
    loader_events = [r for r in autojs_events if str(r.get("event") or "").startswith("autojs_loader_")]
    loader_errors = [r for r in loader_events if r.get("event") == "autojs_loader_error"]
    latest_autojs_event = autojs_events[-1] if autojs_events else None
    latest_heartbeat = next((r for r in reversed(autojs_events) if r.get("event") == "autojs_heartbeat"), None)
    latest_tablet_page_ping = tablet_page_pings[-1] if tablet_page_pings else None
    latest_autojs_age = age_ms(parse_ms((latest_autojs_event or {}).get("serverTime")))
    latest_heartbeat_age = age_ms(parse_ms((latest_heartbeat or {}).get("serverTime")))
    latest_tablet_page_ping_age = age_ms(parse_ms((latest_tablet_page_ping or {}).get("serverTime")))
    if order_done:
        trade_readiness = "has_live_orders"
    elif latest_heartbeat and latest_heartbeat_age is not None and latest_heartbeat_age <= 2 * 60 * 1000:
        trade_readiness = "autojs_online_waiting_for_order_done"
    elif loader_errors and not any(r.get("event") == "autojs_start" for r in autojs_events):
        trade_readiness = "autojs_loader_error"
    elif loader_events and not any(r.get("event") == "autojs_start" for r in autojs_events):
        trade_readiness = "loader_seen_waiting_for_autojs"
    elif autojs_events:
        trade_readiness = "autojs_seen_waiting_for_order_done"
    elif latest_tablet_page_ping and latest_tablet_page_ping_age is not None and latest_tablet_page_ping_age <= 2 * 60 * 1000:
        trade_readiness = "tablet_page_seen_waiting_for_autojs"
    else:
        trade_readiness = "waiting_for_autojs_events"
    health = {
        "generated_at": now,
        "price": {
            "price": price.get("price"),
            "age_ms": price_age,
            "freshness": status_for_age(price_age, 15 * 1000, 60 * 1000),
        },
        "price_ticks": {
            "latest_age_ms": price_tick_age,
            "freshness": status_for_age(price_tick_age, 15 * 1000, 60 * 1000),
        },
        "signals": strategy_status,
        "signal_audit": {
            "latest_age_ms": age_ms(latest_signal_audit_ms),
            "freshness": status_for_age(age_ms(latest_signal_audit_ms), 7 * 60 * 1000, 15 * 60 * 1000),
            "recent_rows": len(signal_audit_tail),
        },
        "trade_audit": {
            "recent_rows": len(trade_audit_tail),
            "recent_tablet_page_pings": len(tablet_page_pings),
            "latest_tablet_page_ping": latest_tablet_page_ping,
            "latest_tablet_page_ping_age_ms": latest_tablet_page_ping_age,
            "recent_autojs_events": len(autojs_events),
            "recent_loader_events": len(loader_events),
            "recent_loader_errors": len(loader_errors),
            "latest_loader_event": loader_events[-1] if loader_events else None,
            "recent_order_done": len(order_done),
            "latest_autojs_event": latest_autojs_event,
            "latest_autojs_event_age_ms": latest_autojs_age,
            "latest_heartbeat": latest_heartbeat,
            "latest_heartbeat_age_ms": latest_heartbeat_age,
            "latest_device": (latest_autojs_event or {}).get("device"),
            "latest_version": (latest_autojs_event or {}).get("version"),
            "freshness": status_for_age(latest_heartbeat_age, 2 * 60 * 1000, 5 * 60 * 1000) if latest_heartbeat else "fail",
            "readiness": trade_readiness,
        },
    }

    statuses = [health["price"]["freshness"], health["price_ticks"]["freshness"], health["signal_audit"]["freshness"]]
    statuses.extend(v["freshness"] for v in strategy_status.values())
    if "fail" in statuses:
        overall = "fail"
    elif "warn" in statuses:
        overall = "warn"
    else:
        overall = "ok"
    health["overall"] = overall

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(health, f, indent=2, ensure_ascii=False)
    print(json.dumps(health, indent=2, ensure_ascii=False))
    print(f"Saved {REPORT_FILE}")


if __name__ == "__main__":
    main()
