from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
V9_JSON = ROOT / "tmp" / "normal_state_v9_state_gate.json"
V9_RULES = ROOT / "tmp" / "normal_state_v9_state_gate_rules.csv"
V9_TRADES = ROOT / "tmp" / "normal_state_v9_state_gate_trades.csv"

OUT_JSON = ROOT / "tmp" / "normal_state_v10_robustness_audit.json"
OUT_CSV = ROOT / "tmp" / "normal_state_v10_robustness_audit.csv"

WIN_PAY = 0.8
LOSS_PAY = -1.0
BREAKEVEN_WR = abs(LOSS_PAY) / (WIN_PAY + abs(LOSS_PAY)) * 100.0

RECOMMENDED = "D5_A5_V6_CONSENSUS_3OF5_UPPER_avoid_slow_persistent_edge"
BASELINE = "D5_A5_V6_CONSENSUS_3OF5_UPPER_none"


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


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 0.0
    p = wins / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    half = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n)) / n) / denom
    return round((center - half) * 100.0, 2), round((center + half) * 100.0, 2)


def summarize(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"n": 0, "wins": 0, "wr": 0.0, "pnl": 0.0, "ev": 0.0, "max_dd": 0.0, "wilson95": [0.0, 0.0], "days": []}
    wons = [bool(x) for x in df["won"].tolist()]
    wins = sum(wons)
    pnl = sum(payout(w) for w in wons)
    days = []
    for day, g in df.groupby("day_cn", sort=True):
        gw = [bool(x) for x in g["won"].tolist()]
        gpnl = sum(payout(w) for w in gw)
        days.append(
            {
                "day": str(day),
                "n": int(len(g)),
                "wins": int(sum(gw)),
                "wr": round(sum(gw) / len(gw) * 100.0, 2),
                "pnl": round(gpnl, 4),
                "max_dd": max_drawdown(gw),
            }
        )
    return {
        "n": int(len(df)),
        "wins": int(wins),
        "wr": round(wins / len(df) * 100.0, 2),
        "pnl": round(pnl, 4),
        "ev": round(pnl / len(df), 5),
        "max_dd": max_drawdown(wons),
        "wilson95": list(wilson_interval(wins, len(df))),
        "days": days,
    }


def active_trade_rows(trades: pd.DataFrame, strategy_key: str) -> pd.DataFrame:
    df = trades[trades["strategy_key"].eq(strategy_key)].copy()
    df = df.sort_values("idx")
    # Defensive de-duplication by actual entry index. V9 also writes daily-stop rows with
    # different keys, but this protects the audit from future appended outputs.
    df = df.drop_duplicates(["idx", "settle_idx", "signal", "entry"], keep="last")
    return df


def leave_one_day_out(df: pd.DataFrame) -> list[dict]:
    out = []
    for day in sorted(df["day_cn"].dropna().unique().tolist()):
        kept = df[df["day_cn"] != day]
        s = summarize(kept)
        out.append(
            {
                "removed_day": str(day),
                "kept_n": s["n"],
                "kept_wr": s["wr"],
                "kept_pnl": s["pnl"],
                "kept_max_dd": s["max_dd"],
                "kept_wilson_low": s["wilson95"][0],
                "kept_wilson_high": s["wilson95"][1],
            }
        )
    return out


def removed_by_gate(baseline: pd.DataFrame, recommended: pd.DataFrame) -> pd.DataFrame:
    rec_keys = set(zip(recommended["signal_time"].astype(str), recommended["time"].astype(str)))
    mask = [
        (str(row["signal_time"]), str(row["time"])) not in rec_keys
        for _, row in baseline.iterrows()
    ]
    return baseline.loc[mask].copy()


def split_summary(df: pd.DataFrame) -> dict:
    return {
        "summary": summarize(df),
        "train_to_0630": summarize(df[df["day_cn"] <= "2026-06-30"]),
        "recent_0701_plus": summarize(df[df["day_cn"] >= "2026-07-01"]),
    }


def train_only_rank(rules: pd.DataFrame) -> pd.DataFrame:
    df = rules.copy()
    df["train_score"] = df.apply(
        lambda r: (
            float(r["train_pnl"])
            - abs(float(r["max_dd"])) * 0.45
            + min(int(r["train_n"]), 40) * 0.04
            - int(r["losing_days"]) * 0.25
        )
        if int(r["train_n"]) >= 15 and float(r["train_wr"]) >= BREAKEVEN_WR and float(r["train_pnl"]) > 0
        else -9999.0,
        axis=1,
    )
    return df.sort_values(["train_score", "train_pnl", "train_n"], ascending=[False, False, False])


def orderbook_sensitivity(df: pd.DataFrame) -> dict:
    if "ob_available" not in df.columns:
        return {}
    out = {}
    for flag, g in df.groupby(df["ob_available"].astype(bool), sort=True):
        out["ob_available" if flag else "ob_missing"] = summarize(g)
    out["available_pct"] = round(float(df["ob_available"].astype(bool).mean() * 100.0), 4) if len(df) else 0.0
    return out


def run() -> dict:
    if not V9_JSON.exists() or not V9_RULES.exists() or not V9_TRADES.exists():
        raise FileNotFoundError("Run research_normal_state_v9_state_gate.py before V10 audit.")

    v9 = json.loads(V9_JSON.read_text(encoding="utf-8"))
    rules = pd.read_csv(V9_RULES)
    trades = pd.read_csv(V9_TRADES)

    rec = active_trade_rows(trades, RECOMMENDED)
    base = active_trade_rows(trades, BASELINE)
    removed = removed_by_gate(base, rec)
    rank = train_only_rank(rules)

    rec_summary = split_summary(rec)
    base_summary = split_summary(base)
    removed_summary = split_summary(removed)
    lodo = leave_one_day_out(rec)
    lodo_df = pd.DataFrame(lodo)

    train_rank_rows = rank.head(16).copy()
    train_rank_rows["is_recommended"] = train_rank_rows["strategy_key"].eq(RECOMMENDED)
    recommended_rank = int(rank.index[rank["strategy_key"].eq(RECOMMENDED)][0]) if rank["strategy_key"].eq(RECOMMENDED).any() else None
    # Convert positional rank for user-facing report.
    rec_pos = rank["strategy_key"].tolist().index(RECOMMENDED) + 1 if RECOMMENDED in rank["strategy_key"].tolist() else None

    audit_rows = []
    for label, df in [("recommended", rec), ("baseline", base), ("removed_by_gate", removed)]:
        parts = split_summary(df)
        for split, summary in parts.items():
            audit_rows.append(
                {
                    "label": label,
                    "split": split,
                    "n": summary["n"],
                    "wr": summary["wr"],
                    "pnl": summary["pnl"],
                    "max_dd": summary["max_dd"],
                    "wilson_low": summary["wilson95"][0],
                    "wilson_high": summary["wilson95"][1],
                }
            )
    pd.DataFrame(audit_rows).to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    red_flags = []
    if rec_summary["summary"]["n"] < 50:
        red_flags.append("sample_under_50_trades")
    if rec_summary["recent_0701_plus"]["n"] < 15:
        red_flags.append("recent_under_15_trades")
    if rec_summary["recent_0701_plus"]["wilson95"][0] < BREAKEVEN_WR:
        red_flags.append("recent_wilson_low_below_breakeven")
    if not lodo_df.empty and float(lodo_df["kept_pnl"].min()) <= 0:
        red_flags.append("leave_one_day_out_can_break_profit")

    report = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "data": v9["data"],
        "payoff": v9["payoff"],
        "recommended_key": RECOMMENDED,
        "baseline_key": BASELINE,
        "audit_method": {
            "leave_one_day_out": "Remove each active trading day from the recommended strategy and recompute metrics.",
            "train_only_rank": "Rank all V9 variants using train<=2026-06-30 only, then inspect recent results separately.",
            "gate_removed_set": "Compare baseline 3/5 D5/A5 with state-gated 3/5 D5/A5 using actual confirmed entry rows.",
            "orderbook_sensitivity": "Split recommended trades by whether orderbook features were available at signal time.",
        },
        "recommended": rec_summary,
        "baseline": base_summary,
        "gate_removed_trades": removed_summary,
        "orderbook_sensitivity": orderbook_sensitivity(rec),
        "leave_one_day_out": lodo,
        "train_only_rank_top": train_rank_rows.to_dict("records"),
        "recommended_train_rank_position": rec_pos,
        "recommended_train_rank_index": recommended_rank,
        "risk_flags": red_flags,
        "fit_risk_conclusion": (
            "medium_high"
            if red_flags
            else "medium"
        ),
        "outputs": {"json": str(OUT_JSON), "csv": str(OUT_CSV)},
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    result = run()
    print(
        json.dumps(
            {
                "recommended": result["recommended"]["summary"],
                "recent": result["recommended"]["recent_0701_plus"],
                "baseline": result["baseline"]["summary"],
                "removed_by_gate": result["gate_removed_trades"]["summary"],
                "orderbook_sensitivity": result["orderbook_sensitivity"],
                "leave_one_day_out_worst": min(result["leave_one_day_out"], key=lambda x: x["kept_pnl"]) if result["leave_one_day_out"] else {},
                "recommended_train_rank_position": result["recommended_train_rank_position"],
                "risk_flags": result["risk_flags"],
                "fit_risk_conclusion": result["fit_risk_conclusion"],
                "outputs": result["outputs"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
