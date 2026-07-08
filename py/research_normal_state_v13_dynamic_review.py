from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "tmp" / "normal_state_v13_dynamic_review.json"


def read_json(path: str) -> dict:
    p = ROOT / path
    if not p.exists():
        return {"missing": str(p)}
    return json.loads(p.read_text(encoding="utf-8-sig"))


def pick_keys(row: dict, keys: list[str]) -> dict:
    return {key: row.get(key) for key in keys if key in row}


def daily_summary(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows or []:
        out.append(
            pick_keys(
                row,
                ["day", "n", "wr", "pnl", "max_dd", "dd"],
            )
        )
    return out


def summarize_v12(report: dict) -> dict:
    conclusion = report.get("conclusion", {})
    capacity = conclusion.get("best_capacity_summary", {})
    quality = conclusion.get("best_quality_summary", {})
    dynamic = conclusion.get("dynamic_selector_result", {})
    return {
        "capacity_v11_like": pick_keys(
            capacity,
            [
                "variant",
                "n",
                "wr",
                "pnl",
                "ev",
                "max_dd",
                "train_n",
                "train_wr",
                "train_pnl",
                "recent_n",
                "recent_wr",
                "recent_pnl",
                "recent_ev",
                "wilson_low",
                "recent_wilson_low",
            ],
        ),
        "quality_mode": pick_keys(
            quality,
            [
                "variant",
                "n",
                "wr",
                "pnl",
                "ev",
                "max_dd",
                "train_n",
                "train_wr",
                "train_pnl",
                "recent_n",
                "recent_wr",
                "recent_pnl",
                "recent_ev",
                "wilson_low",
                "recent_wilson_low",
            ],
        ),
        "walkforward_selector": pick_keys(
            dynamic,
            [
                "selector",
                "n",
                "wr",
                "pnl",
                "ev",
                "max_dd",
                "train_n",
                "train_wr",
                "train_pnl",
                "recent_n",
                "recent_wr",
                "recent_pnl",
                "recent_ev",
                "wilson_low",
                "recent_wilson_low",
            ],
        ),
    }


def summarize_frontier(report: dict) -> list[dict]:
    rows = report.get("frontier_table") or report.get("all_reports") or []
    keys = [
        "name",
        "kind",
        "n",
        "wr",
        "pnl",
        "ev",
        "max_dd",
        "train_n",
        "train_wr",
        "train_pnl",
        "recent_n",
        "recent_wr",
        "recent_pnl",
        "fit_risk",
        "oos_ok",
        "risk_flags",
    ]
    return [pick_keys(row, keys) for row in rows[:12]]


def summarize_v13_scan(report: dict) -> dict:
    reports = report.get("reports") or []
    if not reports:
        return {"available": False, "reason": "no v13 scan reports found"}
    df = pd.DataFrame(reports)
    ranked = df.sort_values(
        ["recent_pnl", "train_pnl", "all_pnl", "all_n"],
        ascending=[False, False, False, False],
    )
    survivors = df[
        (df["train_n"] >= 20)
        & (df["recent_n"] >= 5)
        & (df["train_wr"] >= 55.56)
        & (df["recent_wr"] >= 55.56)
        & (df["train_pnl"] > 0)
        & (df["recent_pnl"] > 0)
    ]
    top = ranked.head(8).to_dict("records")
    keys = [
        "name",
        "modekind",
        "all_n",
        "all_wr",
        "all_pnl",
        "all_dd",
        "train_n",
        "train_wr",
        "train_pnl",
        "recent_n",
        "recent_wr",
        "recent_pnl",
    ]
    return {
        "available": True,
        "tested_variants": int(len(df)),
        "survivors_count": int(len(survivors)),
        "best_attempts": [pick_keys(row, keys) for row in top],
        "best_attempt_recent_days": daily_summary(top[0].get("summary", {}).get("recent", {}).get("days", [])) if top else [],
        "finding": "The raw high-frequency normal-band scan did not produce a train+recent survivor. Raising trade count by polling z-score directly damaged the train split and drawdown.",
    }


def run() -> dict:
    v11 = read_json("tmp/normal_state_v11_capacity_frontier.json")
    v12 = read_json("tmp/normal_state_v12_walkforward_state_selector.json")
    v13 = read_json("tmp/normal_state_v13_revert_combo_scan.json")

    data = {}
    for report in (v12, v11, v13):
        if isinstance(report.get("data"), dict):
            data = report["data"]
            break

    review = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "data": {
            "first": data.get("first"),
            "last": data.get("last"),
            "rows_dense": data.get("rows_dense") or data.get("rows"),
            "observed_pct": data.get("observed_pct"),
            "note": "Data is local research data only; no server backtest was run.",
        },
        "payoff": {
            "win": 0.8,
            "loss": -1.0,
            "breakeven_wr_pct": 55.56,
        },
        "v11_frontier": summarize_frontier(v11),
        "v12_state_selector": summarize_v12(v12),
        "v13_high_frequency_scan": summarize_v13_scan(v13),
        "decision": {
            "do_not_promote": "Do not promote the v13 raw high-frequency z-score scan. It increases trades but fails train/recent robustness.",
            "best_live_candidate": "Keep the event-based V11/V12 family: confirmed upper false-break reversion with bandwalk<6, D5/A5 confirmation.",
            "capacity_choice": "2OF5_bw_lt6 has more trades than quality mode and still positive recent split.",
            "quality_choice": "2OF5_bw_3_5 has better win rate and drawdown but fewer trades.",
            "why_trade_count_is_limited": "Only upper-band false-break events with confirmation and non-persistent bandwalk survived. Lower-band and raw interval scans failed, so more trades are not currently free edge.",
            "next_research": [
                "Collect more July data before accepting any high-frequency extension.",
                "Research a separate continuation strategy only if it has an independent edge; current bandwalk trend add-on failed.",
                "If deploying now, prefer V11 capacity mode plus shadow-only monitoring of candidate extensions.",
            ],
        },
    }
    OUT_JSON.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    return review


if __name__ == "__main__":
    result = run()
    print(
        json.dumps(
            {
                "data": result["data"],
                "v12_state_selector": result["v12_state_selector"],
                "v13_high_frequency_scan": result["v13_high_frequency_scan"],
                "decision": result["decision"],
                "output": str(OUT_JSON),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
