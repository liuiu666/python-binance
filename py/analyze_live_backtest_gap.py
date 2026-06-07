"""Compare current live behavior with strict walk-forward backtest buckets.

This report explains live/backtest drift. It does not promote or enable any
strategy; it highlights regimes, repeated exposure, and simple safety filters
that deserve live shadow validation.
"""
import json
import os
import sys
from collections import defaultdict

import pandas as pd

sys.path.insert(0, "E:/codex/py")
from analyze_signal_audit import (  # noqa: E402
    DEFAULT_STAKE,
    PAYOUT,
    build_trend_lookup,
    dedupe_rows,
    load_price_series,
    read_jsonl,
    settle,
)
from backtest_enhanced import load_symbol  # noqa: E402
from research_strategy_lab import build_oos_frame, candidate_signals  # noqa: E402

OUT = "E:/codex/data"
CONFIG_FILE = os.path.join(OUT, "prod_config.json")
TRADE_CONFIG_FILE = os.path.join(OUT, "trade_config.json")
SIGNAL_AUDIT_FILE = os.path.join(OUT, "signal_audit.jsonl")
REPORT_FILE = os.path.join(OUT, "live_backtest_gap_report.json")
BREAKEVEN_WR = 100 / (1 + PAYOUT)


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def max_loss_streak(statuses):
    best = cur = 0
    for status in statuses:
        if status == "lost":
            cur += 1
            best = max(best, cur)
        elif status in ("won", "tie"):
            cur = 0
    return int(best)


def metrics(items):
    rows = list(items)
    wins = sum(1 for x in rows if bool(x.get("win")))
    losses = sum(1 for x in rows if x.get("win") is False)
    ties = sum(1 for x in rows if x.get("status") == "tie")
    pnl = wins * DEFAULT_STAKE * PAYOUT - losses * DEFAULT_STAKE
    return {
        "trades": len(rows),
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "wr": round(wins / max(1, wins + losses) * 100, 2),
        "edge_over_breakeven": round(wins / max(1, wins + losses) * 100 - BREAKEVEN_WR, 2),
        "pnl_5u": round(float(pnl), 2),
        "max_loss": max_loss_streak([x.get("status") for x in rows]),
    }


def chronological_blocks(rows, blocks=10, min_active_trades=5):
    ordered = sorted(rows, key=lambda r: pd.to_datetime(r["time"], utc=True))
    if not ordered:
        return [], {
            "active_blocks": 0,
            "positive_blocks": 0,
            "min_block_wr": None,
            "worst_block": None,
        }
    block_rows = []
    size = len(ordered)
    for i in range(blocks):
        start = round(i * size / blocks)
        end = round((i + 1) * size / blocks)
        part = ordered[start:end]
        if not part:
            continue
        m = metrics(part)
        block_rows.append({
            "slice": f"block_{i + 1:02d}",
            "start": part[0]["time"],
            "end": part[-1]["time"],
            **m,
        })
    active = [b for b in block_rows if int(b.get("trades") or 0) >= min_active_trades]
    if not active:
        summary = {
            "active_blocks": 0,
            "positive_blocks": 0,
            "min_block_wr": None,
            "worst_block": None,
        }
    else:
        worst = min(active, key=lambda b: float(b.get("wr") or 0))
        summary = {
            "active_blocks": len(active),
            "positive_blocks": sum(1 for b in active if float(b.get("pnl_5u") or 0) > 0),
            "min_block_wr": worst.get("wr"),
            "worst_block": worst.get("slice"),
        }
    return block_rows, summary


def align_bucket(align_score):
    if align_score >= 3:
        return "strong_aligned"
    if align_score > 0:
        return "mild_aligned"
    if align_score == 0:
        return "neutral"
    if align_score <= -3:
        return "strong_countertrend"
    return "mild_countertrend"


def rsi_zone(value):
    if value < 30:
        return "rsi_lt30"
    if value < 45:
        return "rsi_30_45"
    if value <= 55:
        return "rsi_45_55"
    if value <= 70:
        return "rsi_55_70"
    return "rsi_gt70"


def confidence_bin(value):
    if value is None:
        return "unknown"
    value = float(value)
    if value < 20:
        return "0_20"
    if value < 30:
        return "20_30"
    if value < 40:
        return "30_40"
    if value < 50:
        return "40_50"
    return "50_plus"


def trend6_bin(value):
    value = abs(float(value or 0))
    if value < 0.001:
        return "0_10bp"
    if value < 0.0025:
        return "10_25bp"
    if value < 0.005:
        return "25_50bp"
    if value < 0.01:
        return "50_100bp"
    return "100bp_plus"


def enrich_trade(row):
    direction = row["direction"]
    trend_score = int(row.get("trend_score") or 0)
    align_score = trend_score if direction == "UP" else -trend_score
    return {
        **row,
        "align_score": int(align_score),
        "align_bucket": align_bucket(int(align_score)),
        "rsi_zone": rsi_zone(float(row.get("rsi_value") or 50)),
        "confidence_bin": confidence_bin(row.get("confidence")),
        "trend6_bin": trend6_bin(row.get("trend6")),
        "hour_utc": int(pd.to_datetime(row["time"], utc=True).hour),
    }


def live_signal_trades():
    all_rows = read_jsonl(SIGNAL_AUDIT_FILE)
    rows = dedupe_rows([r for r in all_rows if r.get("event") == "signal_snapshot"])
    times, prices = load_price_series()
    trend_lookup = build_trend_lookup(times, prices)
    trades = []
    for row in rows:
        if not row.get("signal"):
            continue
        strategy = row.get("strategy_id") or row.get("label") or "unknown"
        signal_time = pd.to_datetime(row.get("time"), utc=True)
        duration = int(float(row.get("duration") or row.get("interval_min") or 0))
        if duration <= 0:
            continue
        entry_time = (
            pd.to_datetime(row.get("actionable_time"), utc=True)
            if row.get("actionable_time")
            else signal_time + pd.Timedelta(minutes=5)
        )
        expiry = entry_time + pd.Timedelta(minutes=duration)
        idx = times.searchsorted(expiry, side="left")
        if idx >= len(prices):
            continue
        open_price = float(row.get("price"))
        close_price = float(prices[idx])
        status = settle(row.get("signal"), open_price, close_price)
        trend = trend_lookup.get(signal_time.floor("5min"), {})
        trend_score_value = row.get("trend_score", trend.get("trend_score"))
        trend6_value = row.get("trend6", trend.get("trend6"))
        trades.append(enrich_trade({
            "source": "live_signal_audit",
            "strategy": strategy,
            "time": str(signal_time),
            "entry_time": str(entry_time),
            "expiry_time": str(expiry),
            "direction": row.get("signal"),
            "duration": str(duration),
            "open_price": open_price,
            "close_price": close_price,
            "status": status,
            "win": status == "won",
            "confidence": row.get("confidence"),
            "avg_prob": row.get("avg_prob"),
            "rsi_value": row.get("rsi_value"),
            "trend_score": trend_score_value,
            "trend6": trend6_value,
            "amount": row.get("amount") or DEFAULT_STAKE,
        }))
    return trades


def offline_trades(strategy, cfg, df5):
    frame = build_oos_frame(df5, strategy, int(cfg["horizon"]))
    cand = {
        "kind": "ml_rsi",
        "threshold": cfg["threshold"],
        "rsi": [cfg.get("rsi_lo", 30), cfg.get("rsi_hi", 70)],
        "agree_mode": cfg.get("agree_mode", "majority"),
        "trend_gate": "none",
        "skip_hours_utc": cfg.get("skip_hours_utc", []),
    }
    direction, mask = candidate_signals(frame, cand)
    part = frame.loc[mask].copy().reset_index(drop=True)
    used_direction = direction[mask]
    rows = []
    for i, r in part.iterrows():
        direction_text = "UP" if int(used_direction[i]) == 1 else "DOWN"
        status = "won" if int(used_direction[i]) == int(r["target"]) else "lost"
        rows.append(enrich_trade({
            "source": "walkforward_oos",
            "strategy": strategy,
            "time": str(pd.to_datetime(r["time"], utc=True)),
            "entry_time": None,
            "expiry_time": None,
            "direction": direction_text,
            "duration": str(int(cfg.get("interval_min", int(cfg["horizon"]) * 5))),
            "open_price": None,
            "close_price": None,
            "status": status,
            "win": status == "won",
            "confidence": round(abs(float(r["avg"]) - 0.5) * 200, 1),
            "avg_prob": round(float(r["avg"]), 4),
            "rsi_value": round(float(r["rsi14"]), 1),
            "trend_score": int(r["trend_score"]),
            "trend6": round(float(r["trend6"]), 6),
            "amount": DEFAULT_STAKE,
        }))
    return rows


def compare_bucket_field(live_rows, offline_rows, field):
    keys = sorted({str(r.get(field, "unknown")) for r in live_rows} | {str(r.get(field, "unknown")) for r in offline_rows})
    out = {}
    for key in keys:
        live_part = [r for r in live_rows if str(r.get(field, "unknown")) == key]
        offline_part = [r for r in offline_rows if str(r.get(field, "unknown")) == key]
        live_m = metrics(live_part)
        offline_m = metrics(offline_part)
        out[key] = {
            "live": live_m,
            "offline": offline_m,
            "wr_gap_live_minus_offline_pp": round(live_m["wr"] - offline_m["wr"], 2)
                if live_m["trades"] and offline_m["trades"] else None,
            "sample_warning": live_m["trades"] < 10 or offline_m["trades"] < 20,
        }
    return out


def short_strategy(strategy):
    if strategy == "BTC_10min":
        return "10m"
    if strategy == "BTC_30min":
        return "30m"
    return strategy.replace("BTC_", "").replace("min", "m")


def policy_id(strategy, name):
    return f"POLICY_{short_strategy(strategy)}_{name}"


def filter_defs():
    return [
        {
            "name": "baseline_all_signals",
            "description": "Keep every production signal.",
            "fn": lambda r, s: True,
        },
        {
            "name": "one_open_position_per_strategy",
            "description": "Skip signals while the same strategy has an unsettled option.",
            "fn": None,
        },
        {
            "name": "same_direction_gap_1x_duration",
            "description": "Skip a signal if the same strategy already selected the same direction within one option duration.",
            "fn": None,
        },
        {
            "name": "same_direction_gap_2x_duration",
            "description": "Skip a signal if the same strategy already selected the same direction within two option durations.",
            "fn": None,
        },
        {
            "name": "cooldown_after_loss_1x_duration",
            "description": "After a loss, pause the same strategy for one option duration.",
            "fn": None,
        },
        {
            "name": "skip_confidence_50_plus",
            "description": "Skip unusually strong model-disagreement/overextension confidence.",
            "fn": lambda r, s: float(r.get("confidence") or 0) < 50,
        },
        {
            "name": "skip_strong_aligned",
            "description": "Skip rare signals that align with an already strong trend.",
            "fn": lambda r, s: r.get("align_bucket") != "strong_aligned",
        },
    ]


def apply_stateful_filter(rows, name):
    selected = []
    active_until = {}
    cooldown_until = {}
    last_selected_same_direction = {}
    for row in sorted(rows, key=lambda r: (pd.to_datetime(r["time"], utc=True), r["strategy"])):
        strategy = row["strategy"]
        start = pd.to_datetime(row["entry_time"] or row["time"], utc=True)
        duration = pd.Timedelta(minutes=int(float(row.get("duration") or 0)))
        expiry = pd.to_datetime(row["expiry_time"], utc=True) if row.get("expiry_time") else start + duration
        if name == "one_open_position_per_strategy":
            if strategy in active_until and start < active_until[strategy]:
                continue
            selected.append(row)
            active_until[strategy] = expiry
        elif name in ("same_direction_gap_1x_duration", "same_direction_gap_2x_duration"):
            key = (strategy, row.get("direction"))
            gap_mult = 1 if name == "same_direction_gap_1x_duration" else 2
            previous = last_selected_same_direction.get(key)
            if previous is not None and start < previous + duration * gap_mult:
                continue
            selected.append(row)
            last_selected_same_direction[key] = start
        elif name == "cooldown_after_loss_1x_duration":
            if strategy in active_until and start < active_until[strategy]:
                continue
            if strategy in cooldown_until and start < cooldown_until[strategy]:
                continue
            selected.append(row)
            active_until[strategy] = expiry
            if row.get("status") == "lost":
                cooldown_until[strategy] = expiry + duration
        else:
            raise ValueError(name)
    return selected


def filter_screen(live_rows, offline_rows):
    rows = []
    for item in filter_defs():
        name = item["name"]
        if item["fn"] is None:
            live_keep = apply_stateful_filter(live_rows, name)
            offline_keep = apply_stateful_filter(offline_rows, name)
        else:
            live_keep = [r for r in live_rows if item["fn"](r, None)]
            offline_keep = [r for r in offline_rows if item["fn"](r, None)]
        live_m = metrics(live_keep)
        offline_m = metrics(offline_keep)
        _, live_block_summary = chronological_blocks(live_keep, min_active_trades=3)
        _, offline_block_summary = chronological_blocks(offline_keep, min_active_trades=20)
        base_live = metrics(live_rows)
        base_offline = metrics(offline_rows)
        rows.append({
            "id": policy_id(live_rows[0]["strategy"] if live_rows else offline_rows[0]["strategy"], name)
                if (live_rows or offline_rows) else name,
            "name": name,
            "description": item["description"],
            "live": live_m,
            "live_block_summary": live_block_summary,
            "offline": offline_m,
            "offline_block_summary": offline_block_summary,
            "live_trades_delta": live_m["trades"] - base_live["trades"],
            "offline_trades_delta": offline_m["trades"] - base_offline["trades"],
            "live_wr_delta_pp": round(live_m["wr"] - base_live["wr"], 2) if base_live["trades"] else None,
            "offline_wr_delta_pp": round(offline_m["wr"] - base_offline["wr"], 2) if base_offline["trades"] else None,
            "offline_retention_pct": round(offline_m["trades"] / max(1, base_offline["trades"]) * 100, 2),
            "evidence": "live_sample_too_small" if live_m["trades"] < 50 else "live_readable",
        })
    return rows


def loss_clusters(rows):
    clusters = []
    cur = []
    for row in sorted(rows, key=lambda r: pd.to_datetime(r["time"], utc=True)):
        if row["status"] == "lost":
            cur.append(row)
            continue
        if cur:
            clusters.append(cur)
            cur = []
    if cur:
        clusters.append(cur)
    out = []
    for cluster in sorted(clusters, key=len, reverse=True)[:8]:
        out.append({
            "length": len(cluster),
            "start": cluster[0]["time"],
            "end": cluster[-1]["time"],
            "strategies": sorted({r["strategy"] for r in cluster}),
            "directions": sorted({r["direction"] for r in cluster}),
            "align_buckets": sorted({r["align_bucket"] for r in cluster}),
            "hours_utc": sorted({r["hour_utc"] for r in cluster}),
            "min_rsi": round(min(float(r.get("rsi_value") or 0) for r in cluster), 1),
            "max_rsi": round(max(float(r.get("rsi_value") or 0) for r in cluster), 1),
        })
    return out


def repeated_exposure(rows):
    out = {}
    for strategy in sorted({r["strategy"] for r in rows}):
        part = sorted([r for r in rows if r["strategy"] == strategy], key=lambda r: pd.to_datetime(r["time"], utc=True))
        repeats = []
        previous = None
        for row in part:
            if previous:
                gap = pd.to_datetime(row["time"], utc=True) - pd.to_datetime(previous["time"], utc=True)
                same_direction = row["direction"] == previous["direction"]
                if same_direction and gap <= pd.Timedelta(minutes=int(float(row["duration"]))):
                    repeats.append(row)
            previous = row
        out[strategy] = {
            "signals": len(part),
            "repeated_same_direction_within_duration": len(repeats),
            "repeat_rate_pct": round(len(repeats) / max(1, len(part)) * 100, 2),
            "repeat_metrics": metrics(repeats),
        }
    return out


def main():
    config = read_json(CONFIG_FILE, {})
    trade_config = read_json(TRADE_CONFIG_FILE, {})
    df5 = load_symbol("btcusdt")
    if df5 is None:
        raise SystemExit("No BTC data found")

    live_rows = live_signal_trades()
    offline_by_strategy = {}
    live_by_strategy = {}
    strategies = sorted(config.keys())
    for strategy in strategies:
        live_part = [r for r in live_rows if r["strategy"] == strategy]
        offline_part = offline_trades(strategy, config[strategy], df5)
        live_by_strategy[strategy] = live_part
        offline_by_strategy[strategy] = offline_part

    strategy_reports = {}
    fields = ["align_bucket", "trend6_bin", "confidence_bin", "rsi_zone", "hour_utc"]
    for strategy in strategies:
        live_part = live_by_strategy[strategy]
        offline_part = offline_by_strategy[strategy]
        strategy_reports[strategy] = {
            "live": metrics(live_part),
            "offline": metrics(offline_part),
            "wr_gap_live_minus_offline_pp": round(metrics(live_part)["wr"] - metrics(offline_part)["wr"], 2)
                if live_part and offline_part else None,
            "sample_warning": {
                "live": "too_small_for_promotion" if len(live_part) < 50 else "readable",
                "offline": "ok" if len(offline_part) >= 100 else "small",
            },
            "bucket_comparison": {
                field: compare_bucket_field(live_part, offline_part, field)
                for field in fields
            },
            "filter_screen": filter_screen(live_part, offline_part),
            "loss_clusters": loss_clusters(live_part),
            "repeated_exposure": repeated_exposure(live_part).get(strategy, {}),
            "recent_live": live_part[-20:],
        }

    combined_live = [r for rows in live_by_strategy.values() for r in rows]
    combined_offline = [r for rows in offline_by_strategy.values() for r in rows]
    report = {
        "method": {
            "type": "live_backtest_gap_audit",
            "payout": PAYOUT,
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "note": (
                "Live signal audit is replayed against recorded/live prices and compared with "
                "strict walk-forward OOS production logic. Small live samples are diagnostic only."
            ),
        },
        "safety": {
            "autoTrade": trade_config.get("autoTrade"),
            "verdict": "diagnostic_only_do_not_resume_real_auto_trading",
        },
        "data": {
            "offline_5m_start": str(df5["time"].min()),
            "offline_5m_end": str(df5["time"].max()),
            "offline_5m_rows": int(len(df5)),
            "live_signal_trades": len(combined_live),
        },
        "overall": {
            "live": metrics(combined_live),
            "offline": metrics(combined_offline),
            "wr_gap_live_minus_offline_pp": round(metrics(combined_live)["wr"] - metrics(combined_offline)["wr"], 2)
                if combined_live and combined_offline else None,
        },
        "policy_candidates": [
            {
                "id": row["id"],
                "strategy": strategy,
                "name": row["name"],
                "description": row["description"],
                "live": row["live"],
                "live_block_summary": row["live_block_summary"],
                "offline": row["offline"],
                "offline_block_summary": row["offline_block_summary"],
                "live_wr_delta_pp": row["live_wr_delta_pp"],
                "offline_wr_delta_pp": row["offline_wr_delta_pp"],
                "offline_retention_pct": row["offline_retention_pct"],
                "evidence": row["evidence"],
            }
            for strategy, data in strategy_reports.items()
            for row in data["filter_screen"]
            if row["name"] != "baseline_all_signals"
        ],
        "repeated_exposure": repeated_exposure(combined_live),
        "strategies": strategy_reports,
        "diagnosis": [],
    }

    if combined_live and metrics(combined_live)["trades"] < 50:
        report["diagnosis"].append(
            "Live sample is too small for promotion or rejection, but it is large enough to identify failure patterns."
        )
    for strategy, data in strategy_reports.items():
        live_m = data["live"]
        offline_m = data["offline"]
        if live_m["trades"] and live_m["wr"] + 5 < offline_m["wr"]:
            report["diagnosis"].append(
                f"{strategy}: live WR {live_m['wr']}% is far below offline {offline_m['wr']}%; treat as drift until more shadow data confirms recovery."
            )
        align_live = data["bucket_comparison"]["align_bucket"].get("strong_countertrend", {}).get("live", {})
        align_offline = data["bucket_comparison"]["align_bucket"].get("strong_countertrend", {}).get("offline", {})
        if align_live.get("trades"):
            report["diagnosis"].append(
                f"{strategy}: live signals are concentrated in strong_countertrend "
                f"({align_live.get('trades')} trades, WR {align_live.get('wr')}%) vs offline WR {align_offline.get('wr')}%."
            )
        repeat = data["repeated_exposure"]
        if repeat.get("repeat_rate_pct", 0) >= 50:
            report["diagnosis"].append(
                f"{strategy}: {repeat.get('repeat_rate_pct')}% of live signals repeat the same direction within one duration; cooldown/non-overlap needs live shadow validation."
            )

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Saved {REPORT_FILE}")


if __name__ == "__main__":
    main()
