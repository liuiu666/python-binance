"""Summarize collected BTC order-book features.

The report is intentionally conservative: one or a few snapshots prove the
pipeline works, but they are not enough to train or validate a strategy.
"""
import json
import os
import time

import pandas as pd

OUT = "E:/codex/data"
SNAPSHOT_FILE = os.path.join(OUT, "orderbook_snapshots.jsonl")
TRADE_CONFIG_FILE = os.path.join(OUT, "trade_config.json")
REPORT_FILE = os.path.join(OUT, "orderbook_feature_report.json")


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def read_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def describe_series(df, col):
    if col not in df.columns or df.empty:
        return None
    s = pd.to_numeric(df[col], errors="coerce").dropna()
    if s.empty:
        return None
    return {
        "min": round(float(s.min()), 8),
        "p50": round(float(s.quantile(0.5)), 8),
        "max": round(float(s.max()), 8),
        "mean": round(float(s.mean()), 8),
    }


def main():
    rows = read_jsonl(SNAPSHOT_FILE)
    trade_cfg = read_json(TRADE_CONFIG_FILE, {})
    now = int(time.time() * 1000)
    report = {
        "method": {
            "type": "orderbook_feature_pipeline_status",
            "snapshot_file": SNAPSHOT_FILE,
            "note": "Order-book features are collected for future research only; they are not used by live trading yet.",
        },
        "safety": {
            "autoTrade": trade_cfg.get("autoTrade"),
            "verdict": "research_only_do_not_resume_real_auto_trading",
        },
        "sample_count": len(rows),
        "status": "missing_samples",
    }
    if rows:
        df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
        latest = rows[-1]
        age_ms = now - int(latest.get("time") or now)
        report.update({
            "status": "pipeline_ready_collect_more" if len(rows) < 500 else "enough_samples_for_research",
            "latest_age_ms": age_ms,
            "latest_age_sec": round(age_ms / 1000, 1),
            "latest": {
                k: latest.get(k)
                for k in [
                    "time", "best_bid", "best_ask", "mid", "spread_bps",
                    "imbalance_1", "imbalance_5", "imbalance_20",
                    "microprice_deviation_bps",
                ]
            },
            "feature_summary": {
                col: describe_series(df, col)
                for col in [
                    "spread_bps", "imbalance_1", "imbalance_5", "imbalance_20",
                    "microprice_deviation_bps", "bid_notional_mid_20", "ask_notional_mid_20",
                ]
            },
            "research_gate": {
                "min_samples_for_first_read": 500,
                "min_samples_for_training": 5000,
                "current_samples": len(rows),
                "can_train": len(rows) >= 5000,
            },
        })
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Saved {REPORT_FILE}")


if __name__ == "__main__":
    main()
