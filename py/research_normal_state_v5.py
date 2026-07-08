from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_normal_state_v1 as v1
import research_normal_state_v3 as v3
import research_normal_state_v4 as v4


OUT_JSON = ROOT / "tmp" / "normal_state_v5_shadow_adaptive.json"
OUT_TRADES = ROOT / "tmp" / "normal_state_v5_shadow_adaptive_trades.csv"
OUT_CANDIDATES = ROOT / "tmp" / "normal_state_v5_shadow_adaptive_candidates.csv"

HORIZON_SEC = 600
WIN_PAY = 0.8
LOSS_PAY = -1.0
BREAKEVEN_WR = abs(LOSS_PAY) / (WIN_PAY + abs(LOSS_PAY)) * 100.0


def payout(won: bool) -> float:
    return WIN_PAY if won else LOSS_PAY


def max_drawdown(wons: list[bool]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for won in wons:
        equity += payout(bool(won))
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return round(worst, 4)


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "wr": 0.0, "pnl": 0.0, "ev": 0.0, "max_dd": 0.0, "days": [], "modules": {}}
    wons = [bool(r["won"]) for r in rows]
    pnl = sum(payout(w) for w in wons)
    df = pd.DataFrame(rows)
    days = []
    for day, g in df.groupby("day_cn", sort=True):
        gw = [bool(x) for x in g["won"].tolist()]
        gpnl = sum(payout(w) for w in gw)
        days.append({"day": day, "n": int(len(g)), "wr": round(sum(gw) / len(gw) * 100.0, 2), "pnl": round(gpnl, 4), "max_dd": max_drawdown(gw)})
    modules = {}
    for module, g in df.groupby("module", sort=True):
        gw = [bool(x) for x in g["won"].tolist()]
        modules[module] = {"n": int(len(g)), "wr": round(sum(gw) / len(gw) * 100.0, 2), "pnl": round(sum(payout(w) for w in gw), 4)}
    return {
        "n": int(len(rows)),
        "wr": round(sum(wons) / len(wons) * 100.0, 2),
        "pnl": round(pnl, 4),
        "ev": round(pnl / len(rows), 5),
        "max_dd": max_drawdown(wons),
        "days": days,
        "modules": modules,
    }


def split_report(rows: list[dict]) -> dict:
    return {
        "summary": summarize(rows),
        "train_to_0630": summarize([r for r in rows if r["day_cn"] <= "2026-06-30"]),
        "recent_0701_plus": summarize([r for r in rows if r["day_cn"] >= "2026-07-01"]),
        "d0701": summarize([r for r in rows if r["day_cn"] == "2026-07-01"]),
        "d0702": summarize([r for r in rows if r["day_cn"] == "2026-07-02"]),
        "d0703": summarize([r for r in rows if r["day_cn"] == "2026-07-03"]),
    }


def invert_signal(signal: str) -> str:
    return "DOWN" if signal == "UP" else "UP"


def build_mature_exhaustion_rows(continuation_rows: list[dict]) -> list[dict]:
    rows = []
    for row in continuation_rows:
        outside = int(row.get("outside_sec") or 0)
        half_life = float(row.get("m_half_life_min") or np.nan)
        if outside < 300 or not np.isfinite(half_life) or half_life < 10.0:
            continue
        out = dict(row)
        out["module"] = "mature_bandwalk_exhaustion_fade"
        out["regime"] = "exhaustion_reversion"
        out["source_module"] = row.get("module")
        out["source_signal"] = row.get("signal")
        out["signal"] = invert_signal(str(row["signal"]))
        entry = float(row["entry"])
        settle = float(row["settle"])
        out["won"] = bool(settle > entry if out["signal"] == "UP" else settle < entry)
        out["reason"] = "mature outside-band move with slow half-life; fade the mature continuation candidate"
        return_move = (settle / entry - 1.0) * 10000.0
        out["move_bps"] = round(return_move, 4)
        rows.append(out)
    return rows


def bucket_for(row: dict) -> str:
    width = float(row.get("m_width_ratio") or np.nan)
    sigma = float(row.get("sigma10_bps") or np.nan)
    bandwalk = float(row.get("m_bandwalk10") or np.nan)
    outside = int(row.get("outside_sec") or 0)
    half_life = float(row.get("m_half_life_min") or np.nan)

    width_b = "squeeze" if np.isfinite(width) and width < 0.85 else "wide" if np.isfinite(width) and width > 2.2 else "normal_width"
    sigma_b = "low_vol" if np.isfinite(sigma) and sigma < 15 else "high_vol" if np.isfinite(sigma) and sigma > 30 else "mid_vol"
    walk_b = "walk_hi" if np.isfinite(bandwalk) and bandwalk >= 9 else "walk_mid" if np.isfinite(bandwalk) and bandwalk >= 6 else "walk_low"
    mature_b = "mature" if outside >= 300 else "fresh" if outside <= 60 else "developing"
    half_b = "slow_hl" if np.isfinite(half_life) and half_life >= 10 else "fast_hl"
    return "|".join([str(row.get("module")), width_b, sigma_b, walk_b, mature_b, half_b])


def hist_stats(wons: list[bool], lookback: int) -> dict:
    if not wons:
        return {"n": 0, "wr": 0.0, "pnl": 0.0}
    tail = wons[-lookback:]
    pnl = sum(payout(w) for w in tail)
    return {"n": len(tail), "wr": sum(tail) / len(tail) * 100.0, "pnl": pnl}


def shadow_adaptive_gate(
    candidates: list[dict],
    *,
    cooldown_sec: int = 600,
    module_min_n: int = 20,
    module_lookback: int = 30,
    module_min_wr: float = 58.0,
    module_min_pnl: float = 1.0,
    bucket_min_n: int = 8,
    bucket_lookback: int = 12,
    bucket_floor_wr: float = BREAKEVEN_WR,
    bucket_floor_pnl: float = -0.2,
) -> tuple[list[dict], dict]:
    priority = {
        "upper_false_break_revert": 0,
        "mature_bandwalk_exhaustion_fade": 1,
        "squeeze_breakout_continue": 2,
        "bandwalk_trend_continue": 3,
    }
    rows = sorted(candidates, key=lambda r: (int(r["idx"]), priority.get(str(r.get("module")), 99)))
    accepted: list[dict] = []
    pending: list[dict] = []
    module_hist: dict[str, list[bool]] = {}
    bucket_hist: dict[str, list[bool]] = {}
    last_signal = -10**9
    skipped = {"cooldown": 0, "module_not_ready": 0, "module_weak": 0, "bucket_weak": 0}
    settled_ptr = 0

    def settle_until(idx: int) -> None:
        nonlocal settled_ptr
        pending.sort(key=lambda r: int(r["settle_idx"]))
        while settled_ptr < len(pending) and int(pending[settled_ptr]["settle_idx"]) <= idx:
            row = pending[settled_ptr]
            settled_ptr += 1
            module = str(row["module"])
            bucket = str(row["state_bucket"])
            won = bool(row["won"])
            module_hist.setdefault(module, []).append(won)
            bucket_hist.setdefault(bucket, []).append(won)

    for row in rows:
        idx = int(row["idx"])
        settle_until(idx)
        module = str(row["module"])
        state_bucket = bucket_for(row)
        shadow_row = dict(row)
        shadow_row["state_bucket"] = state_bucket
        if idx - last_signal < cooldown_sec:
            skipped["cooldown"] += 1
            pending.append(shadow_row)
            continue
        m_hist = module_hist.get(module, [])
        if len(m_hist) < module_min_n:
            skipped["module_not_ready"] += 1
            pending.append(shadow_row)
            continue
        m_stats = hist_stats(m_hist, module_lookback)
        if m_stats["wr"] < module_min_wr or m_stats["pnl"] < module_min_pnl:
            skipped["module_weak"] += 1
            pending.append(shadow_row)
            continue
        b_hist = bucket_hist.get(state_bucket, [])
        b_stats = hist_stats(b_hist, bucket_lookback)
        if b_stats["n"] >= bucket_min_n and (b_stats["wr"] < bucket_floor_wr or b_stats["pnl"] < bucket_floor_pnl):
            skipped["bucket_weak"] += 1
            pending.append(shadow_row)
            continue
        out = dict(shadow_row)
        out["risk_gate"] = "shadow_adaptive_v5"
        out["module_hist_n"] = m_stats["n"]
        out["module_hist_wr"] = round(m_stats["wr"], 2)
        out["module_hist_pnl"] = round(m_stats["pnl"], 4)
        out["bucket_hist_n"] = b_stats["n"]
        out["bucket_hist_wr"] = round(b_stats["wr"], 2)
        out["bucket_hist_pnl"] = round(b_stats["pnl"], 4)
        accepted.append(out)
        pending.append(shadow_row)
        last_signal = idx
    return accepted, {
        "params": {
            "cooldown_sec": cooldown_sec,
            "module_min_n": module_min_n,
            "module_lookback": module_lookback,
            "module_min_wr": module_min_wr,
            "module_min_pnl": module_min_pnl,
            "bucket_min_n": bucket_min_n,
            "bucket_lookback": bucket_lookback,
            "bucket_floor_wr": round(bucket_floor_wr, 4),
            "bucket_floor_pnl": bucket_floor_pnl,
            "learns_from_shadow_candidates": True,
        },
        "skipped": skipped,
    }


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
    reversion_rows = v4.generate_upper_reversion_rows(bars, features, ctx)
    continuation_rows = v4.generate_continuation_rows(bars, features, ctx)
    exhaustion_rows = build_mature_exhaustion_rows(continuation_rows)
    candidates = reversion_rows + continuation_rows + exhaustion_rows
    for row in candidates:
        row["state_bucket"] = bucket_for(row)
    adaptive_rows, gate_meta = shadow_adaptive_gate(candidates)

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
        },
        "payoff": {"win": WIN_PAY, "loss": LOSS_PAY, "breakeven_wr_pct": round(BREAKEVEN_WR, 2)},
        "anti_overfit": [
            "V5 does not select best parameters on recent days.",
            "Every module learns from shadow candidates that have already expired before the current decision.",
            "A trade is allowed only if module trailing shadow performance clears a fixed safety margin, and weak state buckets can veto it.",
        ],
        "candidate_summary": split_report(candidates),
        "adaptive": split_report(adaptive_rows),
        "gate": gate_meta,
        "sample": adaptive_rows[-50:],
        "outputs": {"json": str(OUT_JSON), "trades_csv": str(OUT_TRADES), "candidates_csv": str(OUT_CANDIDATES)},
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(adaptive_rows).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    pd.DataFrame(candidates).to_csv(OUT_CANDIDATES, index=False, encoding="utf-8-sig")
    return report


if __name__ == "__main__":
    result = run()
    print(
        json.dumps(
            {
                "data": {k: result["data"][k] for k in ("rows_dense", "rows_observed", "observed_pct", "first", "last")},
                "candidate_summary": result["candidate_summary"]["summary"],
                "adaptive": result["adaptive"],
                "gate": result["gate"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
