from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_normal_state_v1 as v1
import research_normal_state_v3 as v3
import research_normal_state_v6 as v6
import research_normal_state_v7_confirm_reentry as v7


OUT_JSON = ROOT / "tmp" / "normal_state_v12_walkforward_state_selector.json"
OUT_VARIANTS = ROOT / "tmp" / "normal_state_v12_variants.csv"
OUT_STATE_BUCKETS = ROOT / "tmp" / "normal_state_v12_state_buckets.csv"
OUT_WF = ROOT / "tmp" / "normal_state_v12_walkforward.csv"
OUT_WF_LOG = ROOT / "tmp" / "normal_state_v12_walkforward_daily_log.csv"
OUT_WF_TRADES = ROOT / "tmp" / "normal_state_v12_walkforward_trades.csv"

WIN_PAY = 0.8
LOSS_PAY = -1.0
BREAKEVEN_WR = abs(LOSS_PAY) / (WIN_PAY + abs(LOSS_PAY)) * 100.0
HORIZON_SEC = 600


@dataclass(frozen=True)
class VariantSpec:
    key: str
    rule_name: str
    gate: str
    description: str


VARIANTS = [
    VariantSpec(
        "2OF5_bw_lt6",
        "V6_CONSENSUS_2OF5_UPPER",
        "bw_lt6",
        "Capacity mode: upper false-break, at least 2/5 clues, avoid recent 1m bandwalk>=6.",
    ),
    VariantSpec(
        "2OF5_bw_3_5",
        "V6_CONSENSUS_2OF5_UPPER",
        "bw_3_5",
        "Quality mode: only fade when bandwalk is present but not persistent, 3<=bandwalk<6.",
    ),
    VariantSpec(
        "2OF5_bw_lt6_sig_gt10",
        "V6_CONSENSUS_2OF5_UPPER",
        "bw_lt6_sig_gt10",
        "Noise filter diagnostic: capacity mode plus sigma10_bps>10.",
    ),
    VariantSpec(
        "3OF5_bw_lt6",
        "V6_CONSENSUS_3OF5_UPPER",
        "bw_lt6",
        "Stricter clue mode: at least 3/5 clues, avoid recent 1m bandwalk>=6.",
    ),
    VariantSpec(
        "3OF5_bw_3_5",
        "V6_CONSENSUS_3OF5_UPPER",
        "bw_3_5",
        "Strict quality mode: 3/5 clues and 3<=bandwalk<6.",
    ),
]

BASELINE_VARIANT = VariantSpec(
    "2OF5_none",
    "V6_CONSENSUS_2OF5_UPPER",
    "none",
    "Diagnostic baseline with no state gate.",
)


def payout(won: bool) -> float:
    return WIN_PAY if bool(won) else LOSS_PAY


def finite(value: object) -> float:
    return v6.finite(value)


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 0.0
    p = wins / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    half = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n)) / n) / denom
    return round((center - half) * 100.0, 2), round((center + half) * 100.0, 2)


def max_drawdown(wons: list[bool]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for won in wons:
        equity += payout(won)
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return round(worst, 4)


def summarize(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"n": 0, "wins": 0, "wr": 0.0, "pnl": 0.0, "ev": 0.0, "max_dd": 0.0, "wilson95": [0.0, 0.0], "days": []}
    ordered = df.sort_values("idx")
    wons = [bool(x) for x in ordered["won"].tolist()]
    wins = int(sum(wons))
    pnl = round(sum(payout(w) for w in wons), 4)
    days = []
    for day, group in ordered.groupby("day_cn", sort=True):
        gw = [bool(x) for x in group["won"].tolist()]
        gpnl = round(sum(payout(w) for w in gw), 4)
        days.append(
            {
                "day": str(day),
                "n": int(len(group)),
                "wr": round(sum(gw) / len(gw) * 100.0, 2),
                "pnl": gpnl,
                "max_dd": max_drawdown(gw),
            }
        )
    return {
        "n": int(len(ordered)),
        "wins": wins,
        "wr": round(wins / len(ordered) * 100.0, 2),
        "pnl": pnl,
        "ev": round(pnl / len(ordered), 5),
        "max_dd": max_drawdown(wons),
        "wilson95": list(wilson_interval(wins, len(ordered))),
        "days": days,
    }


def split_summary(df: pd.DataFrame) -> dict:
    return {
        "summary": summarize(df),
        "train_to_0630": summarize(df[df["day_cn"] <= "2026-06-30"]) if not df.empty else summarize(df),
        "recent_0701_plus": summarize(df[df["day_cn"] >= "2026-07-01"]) if not df.empty else summarize(df),
    }


def gate_allows(row: dict, gate: str) -> bool:
    bandwalk = finite(row.get("m_bandwalk10"))
    sigma10 = finite(row.get("sigma10_bps"))
    if gate == "none":
        return True
    if gate == "bw_lt6":
        return np.isfinite(bandwalk) and bandwalk < 6.0
    if gate == "bw_3_5":
        return np.isfinite(bandwalk) and 3.0 <= bandwalk < 6.0
    if gate == "bw_lt6_sig_gt10":
        return np.isfinite(bandwalk) and bandwalk < 6.0 and np.isfinite(sigma10) and sigma10 > 10.0
    raise ValueError(f"unknown gate: {gate}")


def build_variant_rows(base_rows: list[dict], bars: pd.DataFrame, variant: VariantSpec) -> tuple[pd.DataFrame, dict]:
    spec = next((s for s in v6.rule_specs() if s.name == variant.rule_name), None)
    if spec is None:
        raise ValueError(f"unknown rule: {variant.rule_name}")
    candidates = []
    skipped = {"rule": 0, "gate": 0}
    for row in base_rows:
        ok, detail = v6.rule_allows(row, spec)
        if not ok:
            skipped["rule"] += 1
            continue
        if not gate_allows(row, variant.gate):
            skipped["gate"] += 1
            continue
        out = dict(row)
        votes_n, votes = v6.consensus_votes(row)
        out["variant"] = variant.key
        out["variant_rule"] = variant.rule_name
        out["variant_gate"] = variant.gate
        out["variant_description"] = variant.description
        out["rule_filter_detail"] = detail
        out["consensus_votes"] = votes_n
        out["consensus_vote_names"] = ",".join(votes)
        candidates.append(out)
    confirmed, confirm_meta = v7.apply_confirmation(candidates, bars, delay_sec=5, max_adverse_bps=5.0)
    df = pd.DataFrame(confirmed)
    if not df.empty:
        df["variant"] = variant.key
        df["variant_rule"] = variant.rule_name
        df["variant_gate"] = variant.gate
        df["variant_description"] = variant.description
    return df, {"candidate_n": len(candidates), "skipped": skipped, "confirm": confirm_meta}


def state_bucket_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if out.empty:
        return out
    out["bandwalk_state"] = np.where(out["m_bandwalk10"].astype(float) < 6.0, "bandwalk_lt6", "bandwalk_ge6")
    out["bandwalk_bucket"] = pd.cut(
        out["m_bandwalk10"].astype(float),
        [-1.0, 2.0, 5.0, 99.0],
        labels=["bandwalk_0_2", "bandwalk_3_5", "bandwalk_6p"],
    ).astype(str)
    out["votes_bucket"] = np.where(out["consensus_votes"].astype(float) >= 3.0, "votes_3p", "votes_2")
    out["half_life_bucket"] = pd.cut(
        out["m_half_life_min"].astype(float),
        [-999.0, 8.0, 20.0, 999.0],
        labels=["half_life_le8", "half_life_8_20", "half_life_gt20"],
    ).astype(str)
    out["sigma_bucket"] = pd.cut(
        out["sigma10_bps"].astype(float),
        [-999.0, 10.0, 18.0, 999.0],
        labels=["sigma_le10", "sigma_10_18", "sigma_gt18"],
    ).astype(str)
    out["flow_state"] = np.where(out["flow60"].astype(float) <= 0.0, "flow_rejects_breakout", "flow_supports_breakout")
    out["coarse_state"] = out["bandwalk_state"] + "|" + out["votes_bucket"]
    return out


def state_bucket_report(df: pd.DataFrame, columns: list[str]) -> list[dict]:
    rows = []
    enriched = state_bucket_columns(df)
    for col in columns:
        if col not in enriched:
            continue
        for value, group in enriched.groupby(col, sort=True, observed=False):
            if str(value) == "nan":
                continue
            parts = split_summary(group)
            rows.append(
                {
                    "feature": col,
                    "bucket": str(value),
                    "n": parts["summary"]["n"],
                    "wr": parts["summary"]["wr"],
                    "pnl": parts["summary"]["pnl"],
                    "max_dd": parts["summary"]["max_dd"],
                    "train_n": parts["train_to_0630"]["n"],
                    "train_wr": parts["train_to_0630"]["wr"],
                    "train_pnl": parts["train_to_0630"]["pnl"],
                    "recent_n": parts["recent_0701_plus"]["n"],
                    "recent_wr": parts["recent_0701_plus"]["wr"],
                    "recent_pnl": parts["recent_0701_plus"]["pnl"],
                    "recent_ev": parts["recent_0701_plus"]["ev"],
                }
            )
    return rows


def variant_report(variant: VariantSpec, df: pd.DataFrame, meta: dict) -> dict:
    parts = split_summary(df)
    return {
        "variant": variant.key,
        "rule": variant.rule_name,
        "gate": variant.gate,
        "description": variant.description,
        "candidate_n": meta["candidate_n"],
        "confirm_cooldown_skipped": meta["confirm"]["cooldown_skipped"],
        "confirm_rejected_adverse": meta["confirm"]["rejected"]["adverse_confirmation"],
        "n": parts["summary"]["n"],
        "wr": parts["summary"]["wr"],
        "pnl": parts["summary"]["pnl"],
        "ev": parts["summary"]["ev"],
        "max_dd": parts["summary"]["max_dd"],
        "wilson_low": parts["summary"]["wilson95"][0],
        "wilson_high": parts["summary"]["wilson95"][1],
        "train_n": parts["train_to_0630"]["n"],
        "train_wr": parts["train_to_0630"]["wr"],
        "train_pnl": parts["train_to_0630"]["pnl"],
        "recent_n": parts["recent_0701_plus"]["n"],
        "recent_wr": parts["recent_0701_plus"]["wr"],
        "recent_pnl": parts["recent_0701_plus"]["pnl"],
        "recent_ev": parts["recent_0701_plus"]["ev"],
        "recent_wilson_low": parts["recent_0701_plus"]["wilson95"][0],
        "days": parts["summary"]["days"],
    }


def score_prior(df: pd.DataFrame) -> float:
    s = summarize(df)
    if s["n"] <= 0:
        return -9999.0
    return float(s["pnl"]) - abs(float(s["max_dd"])) * 0.35 + min(int(s["n"]), 30) * 0.03


def walkforward_select(all_rows: pd.DataFrame, *, min_prior_trades: int) -> tuple[pd.DataFrame, list[dict]]:
    accepted = []
    logs = []
    days = sorted(all_rows["day_cn"].dropna().unique().tolist())
    for day in days:
        history = all_rows[all_rows["day_cn"] < day]
        best_variant = ""
        best_score = -9999.0
        best_summary: dict | None = None
        for variant, group in history.groupby("variant", sort=True):
            s = summarize(group)
            if s["n"] < min_prior_trades or s["pnl"] <= 0.0 or s["wr"] < 60.0:
                continue
            candidate_score = score_prior(group)
            if candidate_score > best_score:
                best_variant = str(variant)
                best_score = candidate_score
                best_summary = s
        if not best_variant:
            # The default is the predeclared structural capacity gate. It avoids using
            # same-day or future outcomes before the selector has enough settled history.
            best_variant = "2OF5_bw_lt6"
            reason = "default_capacity_mode"
            prior_n = 0
            prior_wr = 0.0
            prior_pnl = 0.0
        else:
            reason = "prior_days_score"
            prior_n = int(best_summary["n"]) if best_summary else 0
            prior_wr = float(best_summary["wr"]) if best_summary else 0.0
            prior_pnl = float(best_summary["pnl"]) if best_summary else 0.0

        day_rows = all_rows[(all_rows["day_cn"] == day) & (all_rows["variant"] == best_variant)].copy()
        if not day_rows.empty:
            accepted.append(day_rows)
        day_summary = summarize(day_rows)
        logs.append(
            {
                "min_prior_trades": int(min_prior_trades),
                "day": str(day),
                "selected_variant": best_variant,
                "reason": reason,
                "prior_n": prior_n,
                "prior_wr": prior_wr,
                "prior_pnl": prior_pnl,
                "day_n": day_summary["n"],
                "day_wr": day_summary["wr"],
                "day_pnl": day_summary["pnl"],
            }
        )

    selected = pd.concat(accepted, ignore_index=True) if accepted else pd.DataFrame()
    if not selected.empty:
        selected = selected.sort_values("idx").drop_duplicates(["idx", "settle_idx", "signal", "entry"], keep="last")
    return selected, logs


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

    baseline_df, baseline_meta = build_variant_rows(base_rows, bars, BASELINE_VARIANT)
    variant_rows = []
    variant_reports = []
    variant_meta = {}
    for variant in VARIANTS:
        df, meta = build_variant_rows(base_rows, bars, variant)
        variant_meta[variant.key] = meta
        variant_reports.append(variant_report(variant, df, meta))
        if not df.empty:
            variant_rows.append(df)

    all_variants = pd.concat(variant_rows, ignore_index=True) if variant_rows else pd.DataFrame()
    variants_table = pd.DataFrame(variant_reports).sort_values(["recent_ev", "pnl", "n"], ascending=[False, False, False])
    variants_table.to_csv(OUT_VARIANTS, index=False, encoding="utf-8-sig")

    state_rows = state_bucket_report(
        baseline_df,
        ["bandwalk_state", "bandwalk_bucket", "votes_bucket", "half_life_bucket", "sigma_bucket", "flow_state", "coarse_state"],
    )
    state_table = pd.DataFrame(state_rows).sort_values(["feature", "recent_ev", "pnl"], ascending=[True, False, False])
    state_table.to_csv(OUT_STATE_BUCKETS, index=False, encoding="utf-8-sig")

    wf_reports = []
    wf_logs = []
    wf_trade_frames = []
    for min_prior in [0, 5, 8, 12, 15]:
        selected, logs = walkforward_select(all_variants, min_prior_trades=min_prior)
        parts = split_summary(selected)
        wf_reports.append(
            {
                "selector": f"wf_min_prior_{min_prior}",
                "min_prior_trades": int(min_prior),
                "n": parts["summary"]["n"],
                "wr": parts["summary"]["wr"],
                "pnl": parts["summary"]["pnl"],
                "ev": parts["summary"]["ev"],
                "max_dd": parts["summary"]["max_dd"],
                "wilson_low": parts["summary"]["wilson95"][0],
                "wilson_high": parts["summary"]["wilson95"][1],
                "train_n": parts["train_to_0630"]["n"],
                "train_wr": parts["train_to_0630"]["wr"],
                "train_pnl": parts["train_to_0630"]["pnl"],
                "recent_n": parts["recent_0701_plus"]["n"],
                "recent_wr": parts["recent_0701_plus"]["wr"],
                "recent_pnl": parts["recent_0701_plus"]["pnl"],
                "recent_ev": parts["recent_0701_plus"]["ev"],
                "recent_wilson_low": parts["recent_0701_plus"]["wilson95"][0],
                "days": parts["summary"]["days"],
            }
        )
        wf_logs.extend(logs)
        if not selected.empty:
            trade_df = selected.copy()
            trade_df["selector"] = f"wf_min_prior_{min_prior}"
            wf_trade_frames.append(trade_df)

    wf_table = pd.DataFrame(wf_reports).sort_values(["recent_ev", "pnl", "n"], ascending=[False, False, False])
    wf_table.to_csv(OUT_WF, index=False, encoding="utf-8-sig")
    pd.DataFrame(wf_logs).to_csv(OUT_WF_LOG, index=False, encoding="utf-8-sig")
    if wf_trade_frames:
        pd.concat(wf_trade_frames, ignore_index=True).to_csv(OUT_WF_TRADES, index=False, encoding="utf-8-sig")

    report = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "data": {
            "rows_dense": int(len(bars)),
            "rows_observed": int(bars["observed"].sum()),
            "observed_pct": round(float(bars["observed"].mean() * 100.0), 4),
            "first": bars.index.min().isoformat(),
            "last": bars.index.max().isoformat(),
            "second_sources": second_sources,
            "minute_source": minute["minute_source"].iloc[0] if "minute_source" in minute else "",
            "orderbook_sources": orderbook_sources,
            "baseline_candidate_n": int(len(baseline_df)),
            "baseline_meta": baseline_meta,
        },
        "payoff": {"win": WIN_PAY, "loss": LOSS_PAY, "breakeven_wr_pct": round(BREAKEVEN_WR, 2)},
        "method": {
            "state_principle": "Bollinger/normal bands are not direct reversal signals; persistent bandwalk is treated as a continuation state and avoided.",
            "walkforward_principle": "For each day, selector ranks variants using prior days only; current-day and future outcomes are not used.",
            "selection_scope": [asdict(v) for v in VARIANTS],
            "default_before_enough_history": "2OF5_bw_lt6",
        },
        "baseline_no_state": variant_report(BASELINE_VARIANT, baseline_df, baseline_meta),
        "variant_table": variants_table.to_dict("records"),
        "state_bucket_table": state_table.to_dict("records"),
        "walkforward_table": wf_table.to_dict("records"),
        "daily_walkforward_log": wf_logs,
        "conclusion": {
            "best_capacity_variant": "2OF5_bw_lt6",
            "best_capacity_summary": next((r for r in variant_reports if r["variant"] == "2OF5_bw_lt6"), {}),
            "best_quality_variant": "2OF5_bw_3_5",
            "best_quality_summary": next((r for r in variant_reports if r["variant"] == "2OF5_bw_3_5"), {}),
            "dynamic_selector_result": wf_table.to_dict("records")[0] if len(wf_table) else {},
            "recommendation": "Keep V11 capacity mode as the main candidate; use 2OF5_bw_3_5 only as an optional conservative mode. The walk-forward selector lowers risk but does not beat V11 capacity on total PnL.",
        },
        "outputs": {
            "json": str(OUT_JSON),
            "variants_csv": str(OUT_VARIANTS),
            "state_buckets_csv": str(OUT_STATE_BUCKETS),
            "walkforward_csv": str(OUT_WF),
            "walkforward_log_csv": str(OUT_WF_LOG),
            "walkforward_trades_csv": str(OUT_WF_TRADES),
        },
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    result = run()
    print(
        json.dumps(
            {
                "data": {k: result["data"][k] for k in ("rows_dense", "rows_observed", "observed_pct", "first", "last", "baseline_candidate_n")},
                "variants": result["variant_table"][:6],
                "state_buckets_top": result["state_bucket_table"][:10],
                "walkforward": result["walkforward_table"],
                "conclusion": result["conclusion"]["recommendation"],
                "outputs": result["outputs"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
