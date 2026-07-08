from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
TRADES_CSV = ROOT / "tmp" / "normal_state_v11_capacity_frontier_trades.csv"
OUT_JSON = ROOT / "tmp" / "normal_state_v14_incremental_capacity.json"

WIN_PAY = 0.8
LOSS_PAY = -1.0
BREAKEVEN_WR = abs(LOSS_PAY) / (WIN_PAY + abs(LOSS_PAY)) * 100.0

CORE_KEY = "D5_A5_V6_CONSENSUS_3OF5_UPPER_avoid_slow_persistent_edge"
CAPACITY_KEY = "D5_A5_V6_CONSENSUS_2OF5_UPPER_edge_persistence_lt6"
SLOWEDGE_KEY = "D5_A5_V6_CONSENSUS_2OF5_UPPER_avoid_slow_persistent_edge"
LOWVOL_KEY = "D5_A5_V6_CONSENSUS_2OF5_UPPER_avoid_lowvol_slow_edge"
NO_STATE_KEY = "D5_A5_V6_CONSENSUS_2OF5_UPPER_none"


def payout(won: bool) -> float:
    return WIN_PAY if bool(won) else LOSS_PAY


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
            "wr": 0.0,
            "pnl": 0.0,
            "ev": 0.0,
            "max_dd": 0.0,
            "train_n": 0,
            "train_wr": 0.0,
            "train_pnl": 0.0,
            "recent_n": 0,
            "recent_wr": 0.0,
            "recent_pnl": 0.0,
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

    train = ordered[ordered["day_cn"] <= "2026-06-30"]
    recent = ordered[ordered["day_cn"] >= "2026-07-01"]
    train_s = summarize_split(train)
    recent_s = summarize_split(recent)
    return {
        "n": int(len(ordered)),
        "wr": round(wins / len(ordered) * 100.0, 2),
        "pnl": pnl,
        "ev": round(pnl / len(ordered), 5),
        "max_dd": max_drawdown(wons),
        "train_n": train_s["n"],
        "train_wr": train_s["wr"],
        "train_pnl": train_s["pnl"],
        "recent_n": recent_s["n"],
        "recent_wr": recent_s["wr"],
        "recent_pnl": recent_s["pnl"],
        "days": days,
    }


def summarize_split(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"n": 0, "wr": 0.0, "pnl": 0.0}
    wons = [bool(x) for x in df["won"].tolist()]
    return {
        "n": int(len(df)),
        "wr": round(sum(wons) / len(wons) * 100.0, 2),
        "pnl": round(sum(payout(w) for w in wons), 4),
    }


def signature_df(df: pd.DataFrame) -> pd.Series:
    return list(zip(df["idx"].astype(int), df["settle_idx"].astype(int), df["signal"].astype(str), df["entry"].astype(float).round(2)))


def strategy_rows(trades: pd.DataFrame, key: str) -> pd.DataFrame:
    source = trades["source_strategy_key"].fillna(trades["strategy_key"]).astype(str)
    df = trades[source.eq(key)].copy()
    if df.empty:
        df = trades[trades["strategy_key"].astype(str).eq(key)].copy()
    if df.empty:
        return df
    return df.sort_values("idx").drop_duplicates(["idx", "settle_idx", "signal", "entry"], keep="last")


def incremental_report(trades: pd.DataFrame) -> list[dict]:
    core = strategy_rows(trades, CORE_KEY)
    core_set = set(signature_df(core))
    rows = []
    for key in [CORE_KEY, CAPACITY_KEY, SLOWEDGE_KEY, LOWVOL_KEY, NO_STATE_KEY]:
        part = strategy_rows(trades, key)
        sig = set(signature_df(part))
        incremental = part[[item not in core_set for item in signature_df(part)]].copy()
        rows.append(
            {
                "key": key,
                "summary": summarize(part),
                "overlap_with_core": int(len(sig & core_set)),
                "incremental_vs_core": summarize(incremental),
            }
        )
    return rows


def bucketize(series: pd.Series, bins: list[float], labels: list[str]) -> pd.Series:
    return pd.cut(pd.to_numeric(series, errors="coerce"), bins=bins, labels=labels, include_lowest=True).astype(str)


def loss_bucket_report(capacity: pd.DataFrame) -> dict:
    df = capacity.copy()
    df["bw_bucket"] = bucketize(df["m_bandwalk10"], [-1, 2, 5, 99], ["0-2", "3-5", "6+"])
    df["sigma_bucket"] = bucketize(df["sigma10_bps"], [-999, 10, 18, 999], ["<=10", "10-18", ">18"])
    df["half_life_bucket"] = bucketize(df["m_half_life_min"], [-999, 8, 20, 999], ["<=8", "8-20", ">20"])
    df["slope_bucket"] = bucketize(df["m_slope60_bps"], [-999, 40, 80, 999], ["<=40", "40-80", ">80"])
    df["flow_bucket"] = bucketize(df["flow60"], [-999, -0.2, 0, 0.2, 999], ["<=-0.2", "-0.2-0", "0-0.2", ">0.2"])
    df["votes_bucket"] = pd.to_numeric(df["consensus_votes"], errors="coerce").fillna(0).astype(int).astype(str)

    out: dict[str, list[dict]] = {}
    for col in ["bw_bucket", "sigma_bucket", "half_life_bucket", "slope_bucket", "flow_bucket", "votes_bucket"]:
        rows = []
        for value, group in df.groupby(col, sort=True, dropna=False):
            s = summarize_split(group)
            rows.append(
                {
                    "bucket": str(value),
                    "n": s["n"],
                    "wr": s["wr"],
                    "pnl": s["pnl"],
                    "losses": int(s["n"] - group["won"].astype(bool).sum()) if s["n"] else 0,
                }
            )
        out[col] = rows
    return out


def simple_veto_tests(capacity: pd.DataFrame) -> dict:
    tests = {
        "capacity_all_bw_lt6": capacity,
        "veto_bandwalk_0_2__bw_3_5": capacity[(capacity["m_bandwalk10"] >= 3) & (capacity["m_bandwalk10"] < 6)],
        "veto_sigma_le10__sigma_gt10": capacity[capacity["sigma10_bps"] > 10],
        "bw_3_5_and_sigma_gt10": capacity[(capacity["m_bandwalk10"] >= 3) & (capacity["m_bandwalk10"] < 6) & (capacity["sigma10_bps"] > 10)],
        "votes_3p_only": capacity[pd.to_numeric(capacity["consensus_votes"], errors="coerce") >= 3],
        "votes_3p_and_bw_3_5": capacity[(pd.to_numeric(capacity["consensus_votes"], errors="coerce") >= 3) & (capacity["m_bandwalk10"] >= 3) & (capacity["m_bandwalk10"] < 6)],
    }
    return {name: summarize(df) for name, df in tests.items()}


def run() -> dict:
    trades = pd.read_csv(TRADES_CSV)
    capacity = strategy_rows(trades, CAPACITY_KEY)
    report = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "payoff": {"win": WIN_PAY, "loss": LOSS_PAY, "breakeven_wr_pct": round(BREAKEVEN_WR, 2)},
        "base_files": {"trades_csv": str(TRADES_CSV)},
        "incremental_report": incremental_report(trades),
        "loss_bucket_report": loss_bucket_report(capacity),
        "simple_veto_tests": simple_veto_tests(capacity),
        "decision": {
            "capacity_baseline": "Keep 2OF5_bw_lt6 as the capacity baseline when trade count matters.",
            "best_quality_veto": "sigma10_bps > 10 is the best simple non-fitted veto in this audit: it removes the low-volatility noise bucket while preserving recent trades.",
            "conservative_veto": "bandwalk 3-5 is cleaner but cuts more trades.",
            "do_not_add": "Do not add lower-band, raw high-frequency, or bandwalk continuation signals from the current research set.",
            "next_shadow_candidate": "BTC_10min_NORMAL_STATE_V14_SIGMA10_GT10 can be shadow-tested before live use.",
        },
        "outputs": {"json": str(OUT_JSON)},
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    result = run()
    print(
        json.dumps(
            {
                "incremental_report": result["incremental_report"],
                "simple_veto_tests": result["simple_veto_tests"],
                "decision": result["decision"],
                "output": str(OUT_JSON),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
