from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_normal_state_v1 as v1
import research_normal_state_v3 as v3
import research_normal_state_v6 as v6
import research_normal_state_v7_confirm_reentry as v7


OUT_JSON = ROOT / "tmp" / "normal_state_v15_ob_state_gate.json"
OUT_VARIANTS = ROOT / "tmp" / "normal_state_v15_ob_state_gate_variants.csv"
OUT_TRADES = ROOT / "tmp" / "normal_state_v15_ob_state_gate_trades.csv"

WIN_PAY = 0.8
LOSS_PAY = -1.0
BREAKEVEN_WR = abs(LOSS_PAY) / (WIN_PAY + abs(LOSS_PAY)) * 100.0
HORIZON_SEC = 600
TRAIN_CUTOFF = "2026-06-30"
RECENT_START = "2026-07-01"


@dataclass(frozen=True)
class Variant:
    key: str
    description: str
    rule_name: str = "V6_CONSENSUS_2OF5_UPPER"


VARIANTS = [
    Variant(
        "capacity_bw_lt6",
        "V11 capacity baseline: upper false break, 2/5 rejection clues, bandwalk10 < 6.",
    ),
    Variant(
        "quality_bw_3_5",
        "Quality baseline: only fade when price has walked the band mildly, 3 <= bandwalk10 < 6.",
    ),
    Variant(
        "v14_sigma_gt10",
        "V14 simple volatility veto: capacity baseline plus sigma10_bps > 10.",
    ),
    Variant(
        "v15_bw35_or_early_sigma18",
        "State gate: fade mild bandwalk; if bandwalk10 < 3, require high realized volatility sigma10_bps > 18.",
    ),
    Variant(
        "v15_bw35_or_early_sigma18_ob_soft",
        "V15 state gate plus soft orderbook veto: if orderbook is available, skip when book imbalance supports the breakout.",
    ),
    Variant(
        "v15_capacity_ob_soft",
        "Capacity baseline plus soft orderbook veto.",
    ),
    Variant(
        "v15_require_ob_reject",
        "Diagnostic only: require orderbook to be available and rejecting the upper breakout.",
    ),
]


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
        return {
            "n": 0,
            "wins": 0,
            "wr": 0.0,
            "pnl": 0.0,
            "ev": 0.0,
            "max_dd": 0.0,
            "wilson95": [0.0, 0.0],
            "active_days": 0,
            "avg_per_active_day": 0.0,
            "days": [],
        }
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
        "active_days": len(days),
        "avg_per_active_day": round(len(ordered) / len(days), 3) if days else 0.0,
        "days": days,
    }


def split_summary(df: pd.DataFrame) -> dict:
    return {
        "summary": summarize(df),
        "train_to_0630": summarize(df[df["day_cn"] <= TRAIN_CUTOFF]) if not df.empty else summarize(df),
        "recent_0701_plus": summarize(df[df["day_cn"] >= RECENT_START]) if not df.empty else summarize(df),
    }


def side_ob_imbalance(row: dict) -> float:
    side = finite(row.get("breakout_side"))
    imb = finite(row.get("ob_imb20"))
    if not np.isfinite(side) or not np.isfinite(imb):
        return float("nan")
    return side * imb


def orderbook_rejects_breakout(row: dict) -> bool:
    if not bool(row.get("ob_available")):
        return False
    side_imb = side_ob_imbalance(row)
    return np.isfinite(side_imb) and side_imb <= 0.0


def gate_allows(row: dict, key: str) -> bool:
    bandwalk = finite(row.get("m_bandwalk10"))
    sigma10 = finite(row.get("sigma10_bps"))
    if not np.isfinite(bandwalk):
        return False
    capacity = bandwalk < 6.0
    quality = 3.0 <= bandwalk < 6.0
    early_high_vol = bandwalk < 3.0 and np.isfinite(sigma10) and sigma10 > 18.0
    state_v15 = quality or early_high_vol
    ob_soft = (not bool(row.get("ob_available"))) or orderbook_rejects_breakout(row)

    if key == "capacity_bw_lt6":
        return capacity
    if key == "quality_bw_3_5":
        return quality
    if key == "v14_sigma_gt10":
        return capacity and np.isfinite(sigma10) and sigma10 > 10.0
    if key == "v15_bw35_or_early_sigma18":
        return state_v15
    if key == "v15_bw35_or_early_sigma18_ob_soft":
        return state_v15 and ob_soft
    if key == "v15_capacity_ob_soft":
        return capacity and ob_soft
    if key == "v15_require_ob_reject":
        return capacity and orderbook_rejects_breakout(row)
    raise ValueError(f"unknown gate: {key}")


def selected_rule() -> v6.RuleSpec:
    spec = next((s for s in v6.rule_specs() if s.name == "V6_CONSENSUS_2OF5_UPPER"), None)
    if spec is None:
        raise RuntimeError("V6_CONSENSUS_2OF5_UPPER not found")
    return spec


def build_variant_rows(base_rows: list[dict], bars: pd.DataFrame, variant: Variant) -> tuple[pd.DataFrame, dict]:
    spec = selected_rule()
    candidates = []
    skipped = {"rule": 0, "gate": 0}
    for row in base_rows:
        ok, detail = v6.rule_allows(row, spec)
        if not ok:
            skipped["rule"] += 1
            continue
        if not gate_allows(row, variant.key):
            skipped["gate"] += 1
            continue
        out = dict(row)
        votes_n, votes = v6.consensus_votes(row)
        out["variant"] = variant.key
        out["variant_description"] = variant.description
        out["rule_filter_detail"] = detail
        out["consensus_votes"] = votes_n
        out["consensus_vote_names"] = ",".join(votes)
        out["ob_side_imb20"] = round(side_ob_imbalance(row), 6) if np.isfinite(side_ob_imbalance(row)) else np.nan
        out["ob_rejects_breakout"] = bool(orderbook_rejects_breakout(row))
        candidates.append(out)

    confirmed, confirm_meta = v7.apply_confirmation(candidates, bars, delay_sec=5, max_adverse_bps=5.0)
    df = pd.DataFrame(confirmed)
    if not df.empty:
        df["variant"] = variant.key
        df["variant_description"] = variant.description
        df["ob_side_imb20"] = df.apply(side_ob_imbalance, axis=1)
        df["ob_rejects_breakout"] = df.apply(orderbook_rejects_breakout, axis=1)
    return df, {"candidate_n": len(candidates), "skipped": skipped, "confirm": confirm_meta}


def leave_one_day_out(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    rows = []
    for day in sorted(df["day_cn"].dropna().unique().tolist()):
        kept = df[df["day_cn"] != day]
        s = summarize(kept)
        rows.append(
            {
                "removed_day": str(day),
                "kept_n": s["n"],
                "kept_wr": s["wr"],
                "kept_pnl": s["pnl"],
                "kept_max_dd": s["max_dd"],
                "kept_wilson_low": s["wilson95"][0],
            }
        )
    return rows


def variant_report(variant: Variant, df: pd.DataFrame, meta: dict) -> dict:
    parts = split_summary(df)
    lodo = leave_one_day_out(df)
    s = parts["summary"]
    train = parts["train_to_0630"]
    recent = parts["recent_0701_plus"]
    losing_days = sum(1 for d in s["days"] if float(d.get("pnl", 0.0)) < 0.0)
    flags = []
    if s["n"] < 50:
        flags.append("sample_under_50")
    if recent["n"] < 15:
        flags.append("recent_under_15")
    if s["wilson95"][0] < BREAKEVEN_WR:
        flags.append("wilson_low_below_breakeven")
    if train["n"] < 15 or train["pnl"] <= 0.0 or train["wr"] < 60.0:
        flags.append("train_split_weak")
    if recent["n"] < 5 or recent["pnl"] <= 0.0 or recent["wr"] < 60.0:
        flags.append("recent_split_weak")
    if lodo and min(float(x["kept_pnl"]) for x in lodo) <= 0.0:
        flags.append("leave_one_day_out_can_break_profit")

    ob_available = df[df["ob_available"].fillna(False).astype(bool)] if not df.empty and "ob_available" in df else pd.DataFrame()
    ob_unavailable = df[~df["ob_available"].fillna(False).astype(bool)] if not df.empty and "ob_available" in df else pd.DataFrame()
    return {
        "variant": variant.key,
        "description": variant.description,
        "candidate_n": int(meta["candidate_n"]),
        "confirm_cooldown_skipped": int(meta["confirm"]["cooldown_skipped"]),
        "confirm_rejected_adverse": int(meta["confirm"]["rejected"]["adverse_confirmation"]),
        "n": s["n"],
        "wr": s["wr"],
        "pnl": s["pnl"],
        "ev": s["ev"],
        "max_dd": s["max_dd"],
        "wilson_low": s["wilson95"][0],
        "wilson_high": s["wilson95"][1],
        "train_n": train["n"],
        "train_wr": train["wr"],
        "train_pnl": train["pnl"],
        "recent_n": recent["n"],
        "recent_wr": recent["wr"],
        "recent_pnl": recent["pnl"],
        "recent_ev": recent["ev"],
        "active_days": s["active_days"],
        "avg_per_active_day": s["avg_per_active_day"],
        "losing_days": losing_days,
        "worst_lodo_removed_day": min(lodo, key=lambda x: x["kept_pnl"])["removed_day"] if lodo else "",
        "worst_lodo_kept_pnl": min(float(x["kept_pnl"]) for x in lodo) if lodo else 0.0,
        "ob_available_n": summarize(ob_available)["n"],
        "ob_available_wr": summarize(ob_available)["wr"],
        "ob_available_pnl": summarize(ob_available)["pnl"],
        "ob_unavailable_n": summarize(ob_unavailable)["n"],
        "ob_unavailable_wr": summarize(ob_unavailable)["wr"],
        "ob_unavailable_pnl": summarize(ob_unavailable)["pnl"],
        "risk_flags": ";".join(flags),
        "fit_risk": "medium_high" if flags else "medium",
        "days": s["days"],
        "leave_one_day_out": lodo,
    }


def signature(df: pd.DataFrame) -> set[tuple[int, int, str, float]]:
    if df.empty:
        return set()
    return set(zip(df["idx"].astype(int), df["settle_idx"].astype(int), df["signal"].astype(str), df["entry"].astype(float).round(2)))


def incremental_summary(target: pd.DataFrame, base: pd.DataFrame) -> dict:
    base_sig = signature(base)
    if target.empty:
        return summarize(target)
    mask = [item not in base_sig for item in signature_rows(target)]
    return summarize(target[mask].copy())


def signature_rows(df: pd.DataFrame) -> list[tuple[int, int, str, float]]:
    return list(zip(df["idx"].astype(int), df["settle_idx"].astype(int), df["signal"].astype(str), df["entry"].astype(float).round(2)))


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

    variant_dfs: dict[str, pd.DataFrame] = {}
    reports = []
    all_trades = []
    for variant in VARIANTS:
        df, meta = build_variant_rows(base_rows, bars, variant)
        variant_dfs[variant.key] = df
        reports.append(variant_report(variant, df, meta))
        if not df.empty:
            all_trades.append(df)

    report_table = pd.DataFrame(reports).sort_values(
        ["recent_pnl", "pnl", "n"],
        ascending=[False, False, False],
    )
    trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    OUT_VARIANTS.parent.mkdir(parents=True, exist_ok=True)
    report_table.to_csv(OUT_VARIANTS, index=False, encoding="utf-8-sig")
    trades.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")

    capacity = variant_dfs.get("capacity_bw_lt6", pd.DataFrame())
    quality = variant_dfs.get("quality_bw_3_5", pd.DataFrame())
    incremental = {}
    for key, df in variant_dfs.items():
        incremental[key] = {
            "vs_capacity": incremental_summary(df, capacity),
            "vs_quality": incremental_summary(df, quality),
        }

    recommended = "v15_bw35_or_early_sigma18"
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
            "orderbook_available_at_base_pct": round(float(pd.DataFrame(base_rows)["ob_available"].mean() * 100.0), 4) if base_rows else 0.0,
        },
        "payoff": {"win": WIN_PAY, "loss": LOSS_PAY, "breakeven_wr_pct": round(BREAKEVEN_WR, 2)},
        "method": {
            "entry": "Same as V11/V14: D5/A5, enter 5 seconds after signal only if adverse move <= 5 bps, expire 10 minutes after delayed entry.",
            "state_rule": "Do not fade every normal-band return. Mild bandwalk means accepted reversion; early/non-bandwalk entries need sigma10_bps > 18. Orderbook filters are diagnostic only until more live coverage is collected.",
            "anti_overfit": [
                "The sigma buckets 10/18 and bandwalk buckets 0-2/3-5 are reused from prior diagnostics, not a new wide parameter search.",
                "All variants use the same candidate generation, confirmation, cooldown, and 10-minute settlement.",
                "Train <= 2026-06-30 and recent >= 2026-07-01 are reported separately.",
                "Orderbook veto is marked soft because historical orderbook coverage is sparse before late June.",
            ],
        },
        "variant_table": report_table.to_dict("records"),
        "incremental": incremental,
        "recommended_shadow_candidate": recommended,
        "recommended_summary": next((r for r in reports if r["variant"] == recommended), {}),
        "outputs": {
            "json": str(OUT_JSON),
            "variants_csv": str(OUT_VARIANTS),
            "trades_csv": str(OUT_TRADES),
        },
    }
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    out = run()
    print(
        json.dumps(
            {
                "data": out["data"],
                "variant_table": out["variant_table"],
                "recommended_shadow_candidate": out["recommended_shadow_candidate"],
                "recommended_summary": out["recommended_summary"],
                "outputs": out["outputs"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
