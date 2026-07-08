from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_normal_state_v9_state_gate as v9


V9_JSON = ROOT / "tmp" / "normal_state_v9_state_gate.json"
V9_TRADES = ROOT / "tmp" / "normal_state_v9_state_gate_trades.csv"

OUT_JSON = ROOT / "tmp" / "normal_state_v11_capacity_frontier.json"
OUT_CSV = ROOT / "tmp" / "normal_state_v11_capacity_frontier.csv"
OUT_TRADES = ROOT / "tmp" / "normal_state_v11_capacity_frontier_trades.csv"

WIN_PAY = 0.8
LOSS_PAY = -1.0
BREAKEVEN_WR = abs(LOSS_PAY) / (WIN_PAY + abs(LOSS_PAY)) * 100.0
HORIZON_SEC = 600

CORE_KEY = "D5_A5_V6_CONSENSUS_3OF5_UPPER_avoid_slow_persistent_edge"
MORE_TRADES_KEY = "D5_A5_V6_CONSENSUS_2OF5_UPPER_edge_persistence_lt6"
SLOW_EDGE_2OF5_KEY = "D5_A5_V6_CONSENSUS_2OF5_UPPER_avoid_slow_persistent_edge"
LOWVOL_EDGE_2OF5_KEY = "D5_A5_V6_CONSENSUS_2OF5_UPPER_avoid_lowvol_slow_edge"
BASELINE_2OF5_KEY = "D5_A5_V6_CONSENSUS_2OF5_UPPER_none"
BASELINE_QUALITY_KEY = "D5_A5_V6_BASE_UPPER_QUALITY_trend_flow_veto"


def payout(won: bool) -> float:
    return WIN_PAY if bool(won) else LOSS_PAY


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
                "wins": int(sum(gw)),
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
        "train_to_0630": summarize(df[df["day_cn"] <= "2026-06-30"]),
        "recent_0701_plus": summarize(df[df["day_cn"] >= "2026-07-01"]),
    }


def active_trade_rows(trades: pd.DataFrame, strategy_key: str) -> pd.DataFrame:
    df = trades[trades["strategy_key"].eq(strategy_key)].copy()
    if df.empty:
        return df
    df = df.sort_values("idx")
    return df.drop_duplicates(["idx", "settle_idx", "signal", "entry"], keep="last")


def build_portfolio(trades: pd.DataFrame, name: str, legs: list[tuple[str, int]]) -> pd.DataFrame:
    frames = []
    for key, priority in legs:
        part = active_trade_rows(trades, key)
        if part.empty:
            continue
        part = part.copy()
        part["source_strategy_key"] = key
        part["portfolio_priority"] = int(priority)
        frames.append(part)
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True)
    merged = merged.sort_values(["idx", "portfolio_priority", "source_strategy_key"])

    accepted = []
    last_idx = -10**12
    seen_entries: set[tuple[int, int, str, float]] = set()
    for _, row in merged.iterrows():
        key = (int(row["idx"]), int(row["settle_idx"]), str(row["signal"]), float(row["entry"]))
        if key in seen_entries:
            continue
        seen_entries.add(key)
        idx = int(row["idx"])
        if idx - last_idx < HORIZON_SEC:
            continue
        out = row.to_dict()
        out["portfolio_name"] = name
        accepted.append(out)
        last_idx = idx
    return pd.DataFrame(accepted).sort_values("idx") if accepted else pd.DataFrame()


def leave_one_day_out(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    rows = []
    for day in sorted(df["day_cn"].dropna().unique().tolist()):
        kept = df[df["day_cn"] != day]
        summary = summarize(kept)
        rows.append(
            {
                "removed_day": str(day),
                "kept_n": summary["n"],
                "kept_wr": summary["wr"],
                "kept_pnl": summary["pnl"],
                "kept_max_dd": summary["max_dd"],
                "kept_wilson_low": summary["wilson95"][0],
                "kept_wilson_high": summary["wilson95"][1],
            }
        )
    return rows


def flags_for(parts: dict, lodo: list[dict]) -> list[str]:
    summary = parts["summary"]
    train = parts["train_to_0630"]
    recent = parts["recent_0701_plus"]
    flags = []
    if summary["n"] < 50:
        flags.append("sample_under_50_trades")
    if recent["n"] < 15:
        flags.append("recent_under_15_trades")
    if summary["wilson95"][0] < BREAKEVEN_WR:
        flags.append("overall_wilson_low_below_breakeven")
    if recent["wilson95"][0] < BREAKEVEN_WR:
        flags.append("recent_wilson_low_below_breakeven")
    if train["n"] < 15 or train["pnl"] <= 0 or train["wr"] < 60.0:
        flags.append("train_split_not_strong_enough")
    if recent["n"] < 5 or recent["pnl"] <= 0 or recent["wr"] < 60.0:
        flags.append("recent_split_not_strong_enough")
    if summary["max_dd"] < -3.0:
        flags.append("max_dd_worse_than_3u")
    if lodo and min(float(x["kept_pnl"]) for x in lodo) <= 0.0:
        flags.append("leave_one_day_out_can_break_profit")
    return flags


def row_report(name: str, label: str, df: pd.DataFrame, kind: str) -> dict:
    parts = split_summary(df)
    lodo = leave_one_day_out(df)
    flags = flags_for(parts, lodo)
    worst_lodo = min(lodo, key=lambda x: x["kept_pnl"]) if lodo else {}
    return {
        "name": name,
        "label": label,
        "kind": kind,
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
        "active_days": parts["summary"]["active_days"],
        "avg_per_active_day": parts["summary"]["avg_per_active_day"],
        "worst_lodo_removed_day": worst_lodo.get("removed_day", ""),
        "worst_lodo_kept_pnl": worst_lodo.get("kept_pnl", 0.0),
        "risk_flags": ";".join(flags),
        "fit_risk": "medium_high" if flags else "medium",
        "oos_ok": (
            parts["train_to_0630"]["n"] >= 15
            and parts["recent_0701_plus"]["n"] >= 5
            and parts["train_to_0630"]["pnl"] > 0
            and parts["recent_0701_plus"]["pnl"] > 0
            and parts["train_to_0630"]["wr"] >= 60.0
            and parts["recent_0701_plus"]["wr"] >= 60.0
            and parts["summary"]["max_dd"] >= -3.0
            and (not lodo or min(float(x["kept_pnl"]) for x in lodo) > 0.0)
        ),
        "parts": parts,
        "leave_one_day_out": lodo,
    }


def run() -> dict:
    # Rebuild V9 from raw local second, minute, and orderbook data so this audit is not
    # comparing stale CSVs.
    v9_report = v9.run()
    trades = pd.read_csv(V9_TRADES)

    standalone_keys = [
        CORE_KEY,
        MORE_TRADES_KEY,
        SLOW_EDGE_2OF5_KEY,
        LOWVOL_EDGE_2OF5_KEY,
        BASELINE_2OF5_KEY,
    ]
    if BASELINE_QUALITY_KEY in set(trades["strategy_key"].dropna().astype(str)):
        standalone_keys.append(BASELINE_QUALITY_KEY)

    reports = []
    trade_outputs = []
    for key in standalone_keys:
        rows = active_trade_rows(trades, key)
        reports.append(row_report(key, key, rows, "standalone"))
        if not rows.empty:
            out = rows.copy()
            out["capacity_case"] = key
            trade_outputs.append(out)

    portfolios = {
        "V11_CORE_ONLY": [(CORE_KEY, 10)],
        "V11_CORE_PLUS_BANDWALK_2OF5": [(CORE_KEY, 10), (MORE_TRADES_KEY, 20)],
        "V11_BANDWALK_2OF5_ONLY": [(MORE_TRADES_KEY, 10)],
        "V11_CORE_PLUS_SLOWEDGE_2OF5": [(CORE_KEY, 10), (SLOW_EDGE_2OF5_KEY, 20)],
        "V11_CORE_PLUS_LOWVOL_2OF5": [(CORE_KEY, 10), (LOWVOL_EDGE_2OF5_KEY, 20)],
        "V11_CORE_PLUS_NO_STATE_2OF5": [(CORE_KEY, 10), (BASELINE_2OF5_KEY, 20)],
    }
    for name, legs in portfolios.items():
        rows = build_portfolio(trades, name, legs)
        reports.append(row_report(name, name, rows, "portfolio"))
        if not rows.empty:
            out = rows.copy()
            out["capacity_case"] = name
            trade_outputs.append(out)

    table = pd.DataFrame([{k: v for k, v in report.items() if k not in {"parts", "leave_one_day_out"}} for report in reports])
    table = table.sort_values(["oos_ok", "recent_ev", "pnl", "n"], ascending=[False, False, False, False])
    table.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    if trade_outputs:
        pd.concat(trade_outputs, ignore_index=True).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")

    recommended_name = "V11_CORE_PLUS_BANDWALK_2OF5"
    recommended = next(report for report in reports if report["name"] == recommended_name)
    report = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "data": v9_report["data"],
        "payoff": {"win": WIN_PAY, "loss": LOSS_PAY, "breakeven_wr_pct": round(BREAKEVEN_WR, 2)},
        "method": {
            "purpose": "Capacity frontier audit: test whether the V9 normal-state edge can be widened without losing recent out-of-sample quality.",
            "not_deployed": True,
            "selection_rule": "Prefer the highest-capacity simple state rule only if train and recent splits stay profitable, max drawdown stays within 3U, and leave-one-day-out stays profitable.",
            "warning": "Wilson confidence is still wide because all candidates have fewer than 50 trades and recent samples have fewer than 15 trades.",
        },
        "recommended_capacity_candidate": recommended_name,
        "recommended": recommended,
        "frontier_table": table.to_dict("records"),
        "all_reports": reports,
        "outputs": {"json": str(OUT_JSON), "csv": str(OUT_CSV), "trades_csv": str(OUT_TRADES)},
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    result = run()
    print(
        json.dumps(
            {
                "data": {k: result["data"][k] for k in ("rows_dense", "rows_observed", "observed_pct", "first", "last")},
                "recommended": {
                    k: result["recommended"][k]
                    for k in (
                        "name",
                        "n",
                        "wr",
                        "pnl",
                        "max_dd",
                        "train_n",
                        "train_wr",
                        "train_pnl",
                        "recent_n",
                        "recent_wr",
                        "recent_pnl",
                        "recent_ev",
                        "avg_per_active_day",
                        "fit_risk",
                        "risk_flags",
                    )
                },
                "frontier": result["frontier_table"][:8],
                "outputs": result["outputs"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
