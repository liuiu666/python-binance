"""Research 2m-regime strategies for BTC 10-minute binary options.

Research-only:
- Aggregate local 1m BTCUSDT data into 2m bars.
- Label next 10 minutes as UP/DOWN.
- Classify each bar as uptrend, downtrend, range, transition, or uncertain.
- Scan simple regime-specific rules to see where edge comes from.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "E:/codex/py")
from research_2m_10min_binary import (
    OUT,
    SYMBOL,
    BAR_MIN,
    OPTION_MIN,
    HORIZON,
    BREAKEVEN_WR,
    aggregate_bars,
    build_features,
    load_1m,
    merge_external,
    metric,
)

REPORT_FILE = os.path.join(OUT, "regime_strategy_2m_10min_report.json")
STAKE = 5


def pct_rank(s):
    return pd.Series(s).rank(pct=True).fillna(0.5).to_numpy()


def enrich_features(fdf, bars2):
    bars = bars2[["time", "open", "high", "low", "close", "volume"]].copy()
    bars["time"] = pd.to_datetime(bars["time"], utc=True)
    out = fdf.copy()
    out["time"] = pd.to_datetime(out["time"], utc=True)
    out = out.merge(bars, on="time", how="left")
    out["future_close_10m"] = out["close"].shift(-HORIZON)
    out["future_ret_10m"] = out["future_close_10m"] / out["close"] - 1
    out["target"] = out["target"].astype(int)
    return out.dropna(subset=["close", "future_ret_10m"]).reset_index(drop=True)


def classify_regime(df):
    out = df.copy()
    trend = out["trend_score"].astype(float)
    htf = out["htf_score"].astype(float)
    bbp = out["bbp"].astype(float)
    bbw_rank = pct_rank(out["bbw"].astype(float))
    atr_rank = pct_rank(out["atrp"].astype(float))
    vr_rank = pct_rank(out["vr"].astype(float))
    taker = out["taker_ratio"].astype(float)
    t12 = out["trend_12m"].astype(float)
    t24 = out["trend_24m"].astype(float)
    t60 = out["trend_60m"].astype(float)

    squeeze = (bbw_rank <= 0.25) & (atr_rank <= 0.45)
    recent_squeeze = pd.Series(squeeze.astype(int)).rolling(10, min_periods=1).max().shift(1).fillna(0).to_numpy() > 0
    expansion = (out["atr_exp"].astype(float).to_numpy() >= 1.08) | (vr_rank >= 0.72)
    break_up = (bbp >= 0.88) & (t12 > 0) & (t24 > 0)
    break_down = (bbp <= 0.12) & (t12 < 0) & (t24 < 0)
    transition = recent_squeeze & expansion & (break_up | break_down | (trend.abs().to_numpy() >= 3))

    uptrend = (trend >= 2) & (htf >= 0) & (t24 > 0) & (t60 > 0) & ~transition
    downtrend = (trend <= -2) & (htf <= 0) & (t24 < 0) & (t60 < 0) & ~transition
    range_state = (trend.abs() <= 1) & (htf.abs() <= 1) & (bbw_rank <= 0.70) & ~transition

    state = np.full(len(out), "uncertain", dtype=object)
    state[range_state.to_numpy()] = "range"
    state[uptrend.to_numpy()] = "uptrend"
    state[downtrend.to_numpy()] = "downtrend"
    trans_dir = np.where(break_up.to_numpy(), "up", np.where(break_down.to_numpy(), "down", "unknown"))
    state[transition] = np.where(trans_dir[transition] == "up", "transition_up", np.where(trans_dir[transition] == "down", "transition_down", "transition"))

    out["regime"] = state
    out["bbw_rank"] = bbw_rank
    out["atr_rank"] = atr_rank
    out["vr_rank"] = vr_rank
    out["is_squeeze"] = squeeze
    out["recent_squeeze"] = recent_squeeze
    out["is_expansion"] = expansion
    out["break_up"] = break_up.to_numpy()
    out["break_down"] = break_down.to_numpy()
    out["taker_bull"] = taker >= 1.05
    out["taker_bear"] = taker <= 0.95
    return out


def eval_signal(df, name, family, direction, mask, params=None):
    direction = np.asarray(direction, dtype=int)
    mask = np.asarray(mask, dtype=bool) & (direction >= 0)
    target = df["target"].to_numpy(dtype=int)
    wins = direction == target
    times = df["time"].astype(str).to_numpy()
    raw_selected = times[mask]
    raw_first = raw_selected[0] if len(raw_selected) else times[0]
    raw_last = raw_selected[-1] if len(raw_selected) else times[-1]
    raw_overlap = metric(wins[mask], raw_first, raw_last)
    live_mask = non_overlap_mask(mask, HORIZON)
    selected = times[live_mask]
    first = selected[0] if len(selected) else times[0]
    last = selected[-1] if len(selected) else times[-1]
    overall = metric(wins[live_mask], first, last)
    by_regime = {}
    for reg, part_idx in df.groupby("regime").groups.items():
        idx = np.asarray(list(part_idx), dtype=int)
        m = live_mask[idx]
        st = times[idx][m]
        by_regime[reg] = metric(wins[idx][m], st[0] if len(st) else times[idx][0], st[-1] if len(st) else times[idx][-1])
    score = (
        overall["pnl_5u"]
        + overall["wr"] * 3
        + min(overall["trades"], 2500) * 0.16
        - overall["max_loss"] * 10
    )
    return {
        "name": name,
        "family": family,
        "params": params or {},
        "overall": overall,
        "raw_overlap": raw_overlap,
        "by_regime": by_regime,
        "score": round(float(score), 2),
    }


def non_overlap_mask(mask, cooldown_bars):
    mask = np.asarray(mask, dtype=bool)
    out = np.zeros(len(mask), dtype=bool)
    next_allowed = 0
    for i, ok in enumerate(mask):
        if ok and i >= next_allowed:
            out[i] = True
            next_allowed = i + cooldown_bars
    return out


def scan_rules(df):
    rows = []
    n = len(df)
    neg = np.full(n, -1, dtype=int)
    regime = df["regime"].astype(str).to_numpy()
    trend = df["trend_score"].astype(float).to_numpy()
    htf = df["htf_score"].astype(float).to_numpy()
    bbp = df["bbp"].astype(float).to_numpy()
    rsi14 = df["rsi14"].astype(float).to_numpy()
    rsi35 = df["rsi35"].astype(float).to_numpy()
    taker = df["taker_ratio"].astype(float).to_numpy()
    vr_rank = df["vr_rank"].astype(float).to_numpy()

    for min_score in [1, 2, 3, 4]:
        for htf_align in [False, True]:
            up = trend >= min_score
            down = trend <= -min_score
            if htf_align:
                up &= htf >= 0
                down &= htf <= 0
            direction = np.where(up, 1, np.where(down, 0, -1))
            mask = direction >= 0
            rows.append(eval_signal(
                df,
                f"trend_follow_s{min_score}_{'htf' if htf_align else 'nohtf'}",
                "trend_follow",
                direction,
                mask,
                {"min_score": min_score, "htf_align": htf_align},
            ))

            pullback = np.where(direction == 1, bbp <= 0.70, np.where(direction == 0, bbp >= 0.30, False))
            rows.append(eval_signal(
                df,
                f"trend_pullback_s{min_score}_{'htf' if htf_align else 'nohtf'}",
                "trend_pullback",
                direction,
                mask & pullback,
                {"min_score": min_score, "htf_align": htf_align, "pullback_bbp": [0.30, 0.70]},
            ))

    range_mask = regime == "range"
    for lo, hi in [(0.08, 0.92), (0.12, 0.88), (0.18, 0.82), (0.25, 0.75)]:
        direction = np.where(bbp <= lo, 1, np.where(bbp >= hi, 0, -1))
        rows.append(eval_signal(
            df,
            f"range_bbp_reversal_{int(lo*100)}_{int(hi*100)}",
            "range_reversal_no_rsi",
            direction,
            range_mask & (direction >= 0),
            {"bbp": [lo, hi]},
        ))

    for col_name, rsi in [("rsi14", rsi14), ("rsi35", rsi35)]:
        for lo, hi in [(25, 75), (30, 70), (35, 65), (40, 60)]:
            direction = np.where(rsi <= lo, 1, np.where(rsi >= hi, 0, -1))
            rows.append(eval_signal(
                df,
                f"range_{col_name}_reversal_{lo}_{hi}",
                "range_reversal_rsi",
                direction,
                range_mask & (direction >= 0),
                {"rsi_col": col_name, "rsi": [lo, hi]},
            ))

            rows.append(eval_signal(
                df,
                f"all_{col_name}_reversal_{lo}_{hi}",
                "all_regime_rsi",
                direction,
                direction >= 0,
                {"rsi_col": col_name, "rsi": [lo, hi]},
            ))

    trans_up = regime == "transition_up"
    trans_down = regime == "transition_down"
    for require_taker in [False, True]:
        for min_vr in [0.0, 0.65, 0.80]:
            up = trans_up.copy()
            down = trans_down.copy()
            if require_taker:
                up &= taker >= 1.05
                down &= taker <= 0.95
            if min_vr:
                up &= vr_rank >= min_vr
                down &= vr_rank >= min_vr
            direction = np.where(up, 1, np.where(down, 0, -1))
            rows.append(eval_signal(
                df,
                f"transition_breakout_vr{int(min_vr*100)}_{'taker' if require_taker else 'notaker'}",
                "transition_breakout",
                direction,
                direction >= 0,
                {"min_vr_rank": min_vr, "require_taker": require_taker},
            ))

    up_state = (regime == "uptrend")
    down_state = (regime == "downtrend")
    direction = np.where(up_state, 1, np.where(down_state, 0, -1))
    rows.append(eval_signal(df, "state_direction_only", "state_baseline", direction, direction >= 0))

    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def summarize_regimes(df):
    rows = {}
    for reg, part in df.groupby("regime"):
        wins_up = part["target"].astype(int).to_numpy() == 1
        wins_down = part["target"].astype(int).to_numpy() == 0
        rows[reg] = {
            "rows": int(len(part)),
            "up_rate": round(float(wins_up.mean() * 100), 2),
            "down_rate": round(float(wins_down.mean() * 100), 2),
            "avg_future_ret_10m_bps": round(float(part["future_ret_10m"].mean() * 10000), 4),
            "abs_future_ret_10m_bps_p50": round(float(part["future_ret_10m"].abs().quantile(0.5) * 10000), 4),
            "abs_future_ret_10m_bps_p90": round(float(part["future_ret_10m"].abs().quantile(0.9) * 10000), 4),
        }
    return rows


def transition_feature_lift(df):
    reg = df["regime"].astype(str)
    range_mask = reg == "range"
    future_regs = pd.concat([reg.shift(-i) for i in range(1, 6)], axis=1)
    soon_transition = future_regs.isin(["transition_up", "transition_down", "uptrend", "downtrend"]).any(axis=1)
    base = df[range_mask].copy()
    event = df[range_mask & soon_transition].copy()
    non = df[range_mask & ~soon_transition].copy()
    features = {
        "recent_squeeze": df["recent_squeeze"].astype(bool),
        "low_bbw_rank_lt25": df["bbw_rank"] <= 0.25,
        "atr_expansion_gt108": df["atr_exp"] >= 1.08,
        "volume_rank_gt72": df["vr_rank"] >= 0.72,
        "bb_edge_lt12_or_gt88": (df["bbp"] <= 0.12) | (df["bbp"] >= 0.88),
        "taker_imbalance": (df["taker_ratio"] >= 1.08) | (df["taker_ratio"] <= 0.92),
        "trend_score_abs_ge2": df["trend_score"].abs() >= 2,
    }
    out = {
        "range_rows": int(len(base)),
        "range_rows_before_transition_10m": int(len(event)),
        "note": "Event means a range row whose next five 2m bars include transition/uptrend/downtrend.",
        "features": {},
    }
    for name, mask in features.items():
        em = mask[range_mask & soon_transition]
        nm = mask[range_mask & ~soon_transition]
        e_rate = float(em.mean() * 100) if len(em) else 0.0
        n_rate = float(nm.mean() * 100) if len(nm) else 0.0
        out["features"][name] = {
            "before_transition_rate": round(e_rate, 2),
            "normal_range_rate": round(n_rate, 2),
            "lift": round(e_rate / n_rate, 3) if n_rate else None,
        }
    return out


def top(rows, family=None, min_trades=50, limit=10, sort_key=None):
    use = [r for r in rows if r["overall"]["trades"] >= min_trades]
    if family:
        use = [r for r in use if r["family"] == family]
    key = sort_key or (lambda r: r["score"])
    return sorted(use, key=key, reverse=True)[:limit]


def main():
    one = load_1m(SYMBOL)
    two = merge_external(aggregate_bars(one))
    fdf = enrich_features(build_features(two), two)
    df = classify_regime(fdf)
    rows = scan_rules(df)
    report = {
        "method": {
            "type": "regime_first_strategy_research_2m_10min",
            "symbol": SYMBOL.upper(),
            "bar_min": BAR_MIN,
            "option_min": OPTION_MIN,
            "horizon_bars": HORIZON,
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "note": "This does not use future data for signals. Regime labels use current/past features; target is next 10m direction.",
        },
        "data": {
            "one_min_rows": int(len(one)),
            "two_min_rows": int(len(two)),
            "feature_rows": int(len(df)),
            "start": str(df["time"].iloc[0]),
            "end": str(df["time"].iloc[-1]),
        },
        "regime_base_rates": summarize_regimes(df),
        "transition_feature_lift": transition_feature_lift(df),
        "results": {
            "top_balanced": top(rows, min_trades=80, limit=15),
            "top_high_wr": top(rows, min_trades=50, limit=15, sort_key=lambda r: (r["overall"]["wr"], r["overall"]["trades"])),
            "top_trade_count_profitable": top([r for r in rows if r["overall"]["pnl_5u"] > 0], min_trades=80, limit=15, sort_key=lambda r: (r["overall"]["trades"], r["overall"]["wr"])),
            "best_trend_follow": top(rows, "trend_follow", min_trades=80, limit=8),
            "best_trend_pullback": top(rows, "trend_pullback", min_trades=80, limit=8),
            "best_range_no_rsi": top(rows, "range_reversal_no_rsi", min_trades=50, limit=8),
            "best_range_rsi": top(rows, "range_reversal_rsi", min_trades=50, limit=8),
            "best_transition": top(rows, "transition_breakout", min_trades=20, limit=8),
            "best_all_regime_rsi": top(rows, "all_regime_rsi", min_trades=80, limit=8),
        },
        "candidate_count": len(rows),
        "interpretation": {
            "rsi_role": "RSI is useful mainly for range mean-reversion tests; it should not be the only entry rule in trend states.",
            "proposed_architecture": [
                "Regime gate first: uptrend/downtrend/range/transition/uncertain.",
                "Trend states: use trend-follow or trend-pullback model; avoid blind RSI reversal.",
                "Range state: use range-specific reversal model; RSI/BBP can be features here.",
                "Transition state: use breakout-confirm model or skip until confirmation if stats are weak.",
                "Uncertain state: no trade or shadow-only.",
            ],
        },
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps({
        "saved": REPORT_FILE,
        "rows": report["data"],
        "regimes": report["regime_base_rates"],
        "top_balanced": [
            {
                "name": r["name"],
                "family": r["family"],
                "wr": r["overall"]["wr"],
                "trades": r["overall"]["trades"],
                "trades_per_day": r["overall"]["trades_per_day"],
                "raw_trades_before_non_overlap": r["raw_overlap"]["trades"],
                "pnl_5u": r["overall"]["pnl_5u"],
                "max_loss": r["overall"]["max_loss"],
            }
            for r in report["results"]["top_balanced"][:8]
        ],
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
