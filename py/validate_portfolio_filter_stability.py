"""Stability audit for the best portfolio-level filter candidate.

The filter search finds a holdout winner. This script checks whether that
winner is broadly stable across chronological blocks or just improves a few
segments while hiding weak ones.
"""
import json
import os
import sys

import numpy as np

sys.path.insert(0, "E:/codex/py")
from optimize_portfolio_risk_filters import load_combined_trades, mask_for_candidate
from validate_strategy_candidates import PAYOUT, STAKE, metric

OUT = "E:/codex/data"
SEARCH_FILE = os.path.join(OUT, "portfolio_risk_filter_search.json")
REPORT_FILE = os.path.join(OUT, "portfolio_filter_stability.json")
BREAKEVEN_WR = 100 / (1 + PAYOUT)


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def block_rows(df, candidate, blocks=10):
    keep = mask_for_candidate(df, candidate)
    rows = []
    for i, idx in enumerate(np.array_split(np.arange(len(df)), blocks), start=1):
        base = df.iloc[idx].copy()
        filtered = df.iloc[idx][keep.iloc[idx].to_numpy()].copy()
        base_m = metric(base["win"].to_numpy())
        filter_m = metric(filtered["win"].to_numpy())
        rows.append({
            "slice": f"block_{i:02d}",
            "start": str(base["time"].iloc[0]),
            "end": str(base["time"].iloc[-1]),
            "baseline": base_m,
            "candidate": filter_m,
            "wr_delta_pp": round(float(filter_m["wr"]) - float(base_m["wr"]), 2),
            "trade_retention_pct": round(int(filter_m["trades"]) / max(1, int(base_m["trades"])) * 100, 2),
            "max_loss_delta": int(filter_m["max_loss"]) - int(base_m["max_loss"]),
        })
    return rows


def compact(rows):
    improved = [r for r in rows if r["wr_delta_pp"] > 0]
    worsened = [r for r in rows if r["wr_delta_pp"] < 0]
    non_positive = [
        r for r in rows
        if float((r["candidate"] or {}).get("wr") or 0) <= BREAKEVEN_WR
    ]
    retained = [r["trade_retention_pct"] for r in rows]
    return {
        "improved_blocks": len(improved),
        "worsened_blocks": len(worsened),
        "non_positive_candidate_blocks": len(non_positive),
        "min_candidate_wr": min((float(r["candidate"]["wr"]) for r in rows), default=0),
        "min_baseline_wr": min((float(r["baseline"]["wr"]) for r in rows), default=0),
        "avg_trade_retention_pct": round(float(np.mean(retained)), 2) if retained else 0,
        "min_trade_retention_pct": min(retained, default=0),
        "blocks_with_higher_max_loss": sum(1 for r in rows if r["max_loss_delta"] > 0),
    }


def decision(summary, candidate):
    if not candidate or candidate.get("name") == "baseline_parallel":
        return {
            "status": "no_candidate",
            "reason": "No non-baseline candidate was selected by portfolio filter search.",
        }
    if summary["non_positive_candidate_blocks"] > 0:
        return {
            "status": "reject_for_production",
            "reason": "At least one chronological block falls at or below binary-options breakeven.",
        }
    if summary["worsened_blocks"] > summary["improved_blocks"]:
        return {
            "status": "reject_for_production",
            "reason": "Candidate worsens more blocks than it improves.",
        }
    if summary["blocks_with_higher_max_loss"] > 2:
        return {
            "status": "shadow_only",
            "reason": "Candidate improves WR but increases local loss-streak risk in several blocks.",
        }
    if summary["min_trade_retention_pct"] < 65:
        return {
            "status": "shadow_only",
            "reason": "Candidate cuts too many trades in at least one block for immediate production.",
        }
    return {
        "status": "production_candidate",
        "reason": "Candidate is positive across blocks and does not materially worsen local loss-streak risk.",
    }


def main():
    search = read_json(SEARCH_FILE)
    ranked = search.get("ranked") or []
    if not ranked:
        raise SystemExit("No portfolio filter candidates found")
    df = load_combined_trades()
    audits = []
    for candidate in ranked:
        if candidate.get("name") == "baseline_parallel":
            continue
        rows = block_rows(df, candidate, 10)
        summary = compact(rows)
        audits.append({
            "candidate": {
                "name": candidate.get("name"),
                "kind": candidate.get("kind"),
                "skip_hours_by_strategy": candidate.get("skip_hours_by_strategy"),
                "skip_hours_utc": candidate.get("skip_hours_utc"),
                "keep_strategy": candidate.get("keep_strategy"),
                "validation": candidate.get("validation"),
                "full": candidate.get("full"),
                "validation_trade_retention_pct": candidate.get("validation_trade_retention_pct"),
                "score": candidate.get("score"),
            },
            "summary": summary,
            "decision": decision(summary, candidate),
            "blocks": rows,
        })

    audits.sort(
        key=lambda r: (
            r["decision"].get("status") == "production_candidate",
            r["decision"].get("status") == "shadow_only",
            float(((r["candidate"].get("full") or {}).get("metrics") or {}).get("wr") or 0),
            int(((r["candidate"].get("full") or {}).get("metrics") or {}).get("trades") or 0),
            float(r["candidate"].get("score") or 0),
        ),
        reverse=True,
    )
    selected = audits[0] if audits else {
        "candidate": {"name": None},
        "summary": {},
        "decision": {"status": "no_candidate", "reason": "No non-baseline candidate was available."},
        "blocks": [],
    }
    production_candidates = [
        r for r in audits if r["decision"].get("status") == "production_candidate"
    ]
    shadow_candidates = [
        r for r in audits if r["decision"].get("status") == "shadow_only"
    ]
    report = {
        "method": {
            "type": "chronological_block_stability",
            "blocks": 10,
            "stake": STAKE,
            "payout": PAYOUT,
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "note": "Audits every non-baseline portfolio filter candidate across fixed chronological blocks; no new search is performed here.",
        },
        "candidate": selected["candidate"],
        "summary": selected["summary"],
        "decision": selected["decision"],
        "blocks": selected["blocks"],
        "best_production_candidate": production_candidates[0] if production_candidates else None,
        "best_shadow_candidate": shadow_candidates[0] if shadow_candidates else None,
        "candidate_audits": audits,
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Saved {REPORT_FILE}")


if __name__ == "__main__":
    main()
