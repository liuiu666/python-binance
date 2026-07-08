from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_normal_state_v1 as v1
import research_normal_state_v3 as v3
import research_normal_state_v6 as v6
import research_normal_state_v7_confirm_reentry as v7
import research_normal_state_v15_ob_state_gate as v15


OUT_JSON = ROOT / "tmp" / "normal_state_v16_frequency_frontier.json"
OUT_CSV = ROOT / "tmp" / "normal_state_v16_frequency_frontier.csv"
OUT_TRADES = ROOT / "tmp" / "normal_state_v16_frequency_frontier_trades.csv"

COOLDOWNS = [0, 60, 120, 180, 300, 600, 900]
VARIANT_KEYS = [
    "capacity_bw_lt6",
    "v14_sigma_gt10",
    "quality_bw_3_5",
    "v15_bw35_or_early_sigma18",
]


def signature_rows(df: pd.DataFrame) -> list[tuple[int, int, str, float]]:
    if df.empty:
        return []
    return list(zip(df["idx"].astype(int), df["settle_idx"].astype(int), df["signal"].astype(str), df["entry"].astype(float).round(2)))


def incremental_summary(df: pd.DataFrame, base_df: pd.DataFrame) -> dict:
    if df.empty:
        return v15.summarize(df)
    base_sig = set(signature_rows(base_df))
    mask = [sig not in base_sig for sig in signature_rows(df)]
    return v15.summarize(df[mask].copy())


def split_summary(df: pd.DataFrame) -> dict:
    return {
        "summary": v15.summarize(df),
        "train_to_0630": v15.summarize(df[df["day_cn"] <= v15.TRAIN_CUTOFF]) if not df.empty else v15.summarize(df),
        "recent_0701_plus": v15.summarize(df[df["day_cn"] >= v15.RECENT_START]) if not df.empty else v15.summarize(df),
    }


def max_concurrent(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    events: list[tuple[int, int]] = []
    for _, row in df.iterrows():
        events.append((int(row["idx"]), 1))
        events.append((int(row["settle_idx"]), -1))
    active = 0
    peak = 0
    for _, delta in sorted(events):
        active += delta
        peak = max(peak, active)
    return peak


def worst_day(df: pd.DataFrame) -> dict:
    days = v15.summarize(df).get("days", [])
    if not days:
        return {"day": "", "n": 0, "wr": 0.0, "pnl": 0.0, "max_dd": 0.0}
    return min(days, key=lambda x: float(x.get("pnl", 0.0)))


def build_candidates(base_rows: list[dict], variant_key: str) -> list[dict]:
    spec = v15.selected_rule()
    rows = []
    for row in base_rows:
        ok, detail = v6.rule_allows(row, spec)
        if not ok:
            continue
        if not v15.gate_allows(row, variant_key):
            continue
        out = dict(row)
        votes_n, votes = v6.consensus_votes(row)
        out["variant"] = variant_key
        out["rule_filter_detail"] = detail
        out["consensus_votes"] = votes_n
        out["consensus_vote_names"] = ",".join(votes)
        rows.append(out)
    return rows


def run() -> dict:
    bars, second_sources = v3.load_merged_bars_v3()
    minute = v1.load_minute_features(bars.index)
    orderbook, orderbook_sources = v3.load_orderbook_features_v3(bars.index)
    features = pd.concat(
        [
            minute.drop(columns=["minute_source"], errors="ignore"),
            orderbook.drop(columns=["orderbook_sources"], errors="ignore"),
        ],
        axis=1,
    )
    ctx = v1.build_second_context(bars, 180 * 60)
    base_rows = v7.prepare_base_rows(bars, features, ctx)

    rows = []
    trades = []
    by_variant_cd: dict[tuple[str, int], pd.DataFrame] = {}
    candidate_counts = {}
    for variant_key in VARIANT_KEYS:
        candidates = build_candidates(base_rows, variant_key)
        candidate_counts[variant_key] = len(candidates)
        for cooldown in COOLDOWNS:
            confirmed, meta = v7.apply_confirmation(candidates, bars, delay_sec=5, max_adverse_bps=5.0, cooldown_sec=cooldown)
            df = pd.DataFrame(confirmed)
            if not df.empty:
                df["variant"] = variant_key
                df["cooldown_sec"] = cooldown
                trades.append(df)
            by_variant_cd[(variant_key, cooldown)] = df

    for variant_key in VARIANT_KEYS:
        base600 = by_variant_cd.get((variant_key, 600), pd.DataFrame())
        base_s = split_summary(base600)
        for cooldown in COOLDOWNS:
            df = by_variant_cd[(variant_key, cooldown)]
            parts = split_summary(df)
            inc = incremental_summary(df, base600)
            train = parts["train_to_0630"]
            recent = parts["recent_0701_plus"]
            summary = parts["summary"]
            wday = worst_day(df)
            inc_vs_600 = int(summary["n"] - base_s["summary"]["n"])
            rows.append(
                {
                    "variant": variant_key,
                    "cooldown_sec": cooldown,
                    "candidate_n": candidate_counts[variant_key],
                    "n": summary["n"],
                    "wr": summary["wr"],
                    "pnl": summary["pnl"],
                    "ev": summary["ev"],
                    "max_dd": summary["max_dd"],
                    "active_days": summary["active_days"],
                    "avg_per_active_day": summary["avg_per_active_day"],
                    "max_concurrent_10m": max_concurrent(df),
                    "train_n": train["n"],
                    "train_wr": train["wr"],
                    "train_pnl": train["pnl"],
                    "recent_n": recent["n"],
                    "recent_wr": recent["wr"],
                    "recent_pnl": recent["pnl"],
                    "incremental_n_vs_600": inc_vs_600,
                    "incremental_wr_vs_600": inc["wr"],
                    "incremental_pnl_vs_600": inc["pnl"],
                    "incremental_ev_vs_600": inc["ev"],
                    "worst_day": wday["day"],
                    "worst_day_n": wday["n"],
                    "worst_day_wr": wday["wr"],
                    "worst_day_pnl": wday["pnl"],
                    "risk_flags": ";".join(
                        flag
                        for flag, bad in [
                            ("sample_under_50", summary["n"] < 50),
                            ("recent_under_15", recent["n"] < 15),
                            ("train_weak", train["n"] < 15 or train["pnl"] <= 0.0 or train["wr"] < 60.0),
                            ("recent_weak", recent["n"] < 5 or recent["pnl"] <= 0.0 or recent["wr"] < 60.0),
                            ("max_dd_worse_than_3u", summary["max_dd"] < -3.0),
                            ("incremental_bad_vs_600", cooldown < 600 and inc["n"] > 0 and (inc["wr"] < 60.0 or inc["pnl"] <= 0.0)),
                            ("overlapping_positions", max_concurrent(df) > 1),
                        ]
                        if bad
                    ),
                }
            )

    table = pd.DataFrame(rows).sort_values(["variant", "cooldown_sec"])
    trades_df = pd.concat(trades, ignore_index=True) if trades else pd.DataFrame()
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    trades_df.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")

    result = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "data": {
            "rows_dense": int(len(bars)),
            "rows_observed": int(bars["observed"].sum()),
            "observed_pct": round(float(bars["observed"].mean() * 100.0), 4),
            "first": bars.index.min().isoformat(),
            "last": bars.index.max().isoformat(),
            "second_sources_count": len(second_sources),
            "minute_source": minute["minute_source"].iloc[0] if "minute_source" in minute else "",
            "orderbook_sources_count": len(orderbook_sources),
        },
        "method": {
            "question": "Can we increase trade count by reducing the 10-minute de-dup cooldown without destroying edge?",
            "fixed_logic": "Same false-break, same D5/A5 delayed entry, same 10-minute expiry. Only cooldown_sec changes.",
            "important": "cooldown_sec < 600 means overlapping 10-minute positions can exist; this may not match one-at-a-time binary-option execution.",
        },
        "frontier": table.to_dict("records"),
        "decision": {
            "current_best_non_overlap": "v15_bw35_or_early_sigma18 cooldown=600",
            "frequency_test_interpretation": "Use incremental_n/wr/pnl_vs_600 to judge whether extra trades are real edge or only lower-quality overlap.",
        },
        "outputs": {"json": str(OUT_JSON), "csv": str(OUT_CSV), "trades_csv": str(OUT_TRADES)},
    }
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    out = run()
    print(
        json.dumps(
            {
                "data": out["data"],
                "frontier": out["frontier"],
                "outputs": out["outputs"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
