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
import research_normal_state_v6 as v6


OUT_JSON = ROOT / "tmp" / "normal_state_v7_confirm_reentry.json"
OUT_TRADES = ROOT / "tmp" / "normal_state_v7_confirm_reentry_trades.csv"
OUT_RULES = ROOT / "tmp" / "normal_state_v7_confirm_reentry_rules.csv"

HORIZON_SEC = 600
WIN_PAY = 0.8
LOSS_PAY = -1.0
BREAKEVEN_WR = abs(LOSS_PAY) / (WIN_PAY + abs(LOSS_PAY)) * 100.0


def payout(won: bool) -> float:
    return WIN_PAY if won else LOSS_PAY


def split_report(rows: list[dict]) -> dict:
    return {
        "summary": v4.summarize(rows),
        "train_to_0630": v4.summarize([r for r in rows if r["day_cn"] <= "2026-06-30"]),
        "recent_0701_plus": v4.summarize([r for r in rows if r["day_cn"] >= "2026-07-01"]),
        "d0701": v4.summarize([r for r in rows if r["day_cn"] == "2026-07-01"]),
        "d0702": v4.summarize([r for r in rows if r["day_cn"] == "2026-07-02"]),
        "d0703": v4.summarize([r for r in rows if r["day_cn"] == "2026-07-03"]),
    }


def observed_ratio(observed: pd.Series, start_idx: int, end_idx: int) -> float:
    if start_idx < 0 or end_idx <= start_idx or end_idx > len(observed):
        return 0.0
    return float(observed.iloc[start_idx:end_idx].mean())


def apply_confirmation(
    rows: list[dict],
    bars: pd.DataFrame,
    *,
    delay_sec: int,
    max_adverse_bps: float,
    cooldown_sec: int = HORIZON_SEC,
) -> tuple[list[dict], dict]:
    close = bars["close"].to_numpy(float)
    observed = bars["observed"].astype(float)
    confirmed: list[dict] = []
    rejected = {"adverse_confirmation": 0, "missing_history": 0, "missing_future": 0}

    for row in sorted(rows, key=lambda r: int(r["idx"])):
        signal_idx = int(row["idx"])
        entry_idx = signal_idx + delay_sec
        settle_idx = entry_idx + HORIZON_SEC
        if entry_idx >= len(close) or observed_ratio(observed, max(0, entry_idx - 600), entry_idx) < 0.98:
            rejected["missing_history"] += 1
            continue
        if settle_idx >= len(close) or observed_ratio(observed, entry_idx, settle_idx) < 0.98:
            rejected["missing_future"] += 1
            continue

        signal = str(row["signal"])
        signal_entry = float(row["entry"])
        delayed_entry = float(close[entry_idx])
        adverse_bps = (delayed_entry / signal_entry - 1.0) * 10000.0
        if signal == "UP":
            adverse_bps = -adverse_bps
        if adverse_bps > max_adverse_bps:
            rejected["adverse_confirmation"] += 1
            continue

        settle = float(close[settle_idx])
        won = settle > delayed_entry if signal == "UP" else settle < delayed_entry
        out = dict(row)
        out["signal_time"] = row["time"]
        out["time"] = bars.index[entry_idx].isoformat()
        out["day_cn"] = bars.index[entry_idx].tz_convert("Asia/Shanghai").strftime("%Y-%m-%d")
        out["idx"] = int(entry_idx)
        out["settle_idx"] = int(settle_idx)
        out["signal_entry"] = round(signal_entry, 2)
        out["entry"] = round(delayed_entry, 2)
        out["settle"] = round(settle, 2)
        out["won"] = bool(won)
        out["move_bps"] = round((settle / delayed_entry - 1.0) * 10000.0, 4)
        out["confirm_delay_sec"] = int(delay_sec)
        out["confirm_adverse_bps"] = round(float(adverse_bps), 4)
        out["confirm_max_adverse_bps"] = float(max_adverse_bps)
        confirmed.append(out)

    accepted: list[dict] = []
    last_entry_idx = -10**9
    cooldown_skipped = 0
    for row in sorted(confirmed, key=lambda r: int(r["idx"])):
        idx = int(row["idx"])
        if idx - last_entry_idx < cooldown_sec:
            cooldown_skipped += 1
            continue
        accepted.append(row)
        last_entry_idx = idx
    return accepted, {
        "params": {
            "delay_sec": delay_sec,
            "max_adverse_bps": max_adverse_bps,
            "cooldown_sec": cooldown_sec,
            "entry_after_confirmation": True,
            "settlement_after_entry": True,
        },
        "rejected": rejected,
        "cooldown_skipped": cooldown_skipped,
    }


def selected_specs() -> list[v6.RuleSpec]:
    wanted = {
        "V6_FRESH_TAIL_CAP",
        "V6_SLOPE_WIDTH_REJECT",
        "V6_CONSENSUS_3OF5_UPPER",
        "V6_CONSENSUS_2OF5_UPPER",
        "V6_BASE_UPPER_QUALITY",
    }
    return [spec for spec in v6.rule_specs() if spec.name in wanted]


def prepare_base_rows(bars: pd.DataFrame, features: pd.DataFrame, ctx: pd.DataFrame) -> list[dict]:
    raw = v1.generate_reversion_rows(
        bars,
        features,
        lookback_sec=180 * 60,
        second_context=ctx,
        reentry_z=1.96,
        max_outside_sec=900,
        state_filter="none",
        ob_filter="none",
        cooldown_sec=0,
    )
    out: list[dict] = []
    for row in raw:
        annotated = v6.annotate_base_quality(row, "upper_only")
        if annotated:
            out.append(annotated)
    return out


def apply_rule(base_rows: list[dict], spec: v6.RuleSpec) -> list[dict]:
    out: list[dict] = []
    for row in base_rows:
        ok, why = v6.rule_allows(row, spec)
        if not ok:
            continue
        item = dict(row)
        votes_n, votes = v6.consensus_votes(row)
        item["module"] = f"V7_CONFIRM_{spec.name}"
        item["source_rule"] = spec.name
        item["regime"] = "confirmed_false_break_reversion"
        item["reason"] = f"{spec.description}; enter only after delayed confirmation"
        item["consensus_votes"] = votes_n
        item["consensus_vote_names"] = ",".join(votes)
        item["rule_filter_detail"] = why
        out.append(item)
    return out


def fitting_check(report: dict) -> dict:
    train = report["train_to_0630"]
    recent = report["recent_0701_plus"]
    summary = report["summary"]
    survivor = (
        train["n"] >= 15
        and recent["n"] >= 5
        and train["wr"] >= 60.0
        and recent["wr"] >= 60.0
        and train["pnl"] > 0
        and recent["pnl"] > 0
    )
    days = summary.get("days", [])
    losing_days = sum(1 for d in days if float(d.get("pnl", 0.0)) < 0.0)
    return {
        "survivor": survivor,
        "fit_risk": "medium" if survivor and summary["n"] >= 25 else "high",
        "active_days": len(days),
        "losing_days": losing_days,
        "worst_day_pnl": round(min((float(d.get("pnl", 0.0)) for d in days), default=0.0), 4),
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
    base_rows = prepare_base_rows(bars, features, ctx)

    reports: list[dict] = []
    all_trades: list[dict] = []
    detail: dict[str, dict] = {}
    delay_grid = [5, 15]
    tolerance_grid = [0.0, 1.0, 2.0, 5.0]

    for spec in selected_specs():
        rule_rows = apply_rule(base_rows, spec)
        for delay_sec in delay_grid:
            for tolerance_bps in tolerance_grid:
                confirmed, confirm_meta = apply_confirmation(
                    rule_rows,
                    bars,
                    delay_sec=delay_sec,
                    max_adverse_bps=tolerance_bps,
                )
                for row in confirmed:
                    row["module"] = f"V7_D{delay_sec}_A{tolerance_bps:g}_{spec.name}"
                report = split_report(confirmed)
                check = fitting_check(report)
                key = f"D{delay_sec}_A{tolerance_bps:g}_{spec.name}"
                reports.append(
                    {
                        "key": key,
                        "source_rule": spec.name,
                        "delay_sec": delay_sec,
                        "max_adverse_bps": tolerance_bps,
                        "candidate_n_before_confirm": len(rule_rows),
                        "n": report["summary"]["n"],
                        "wr": report["summary"]["wr"],
                        "pnl": report["summary"]["pnl"],
                        "max_dd": report["summary"]["max_dd"],
                        "train_n": report["train_to_0630"]["n"],
                        "train_wr": report["train_to_0630"]["wr"],
                        "train_pnl": report["train_to_0630"]["pnl"],
                        "recent_n": report["recent_0701_plus"]["n"],
                        "recent_wr": report["recent_0701_plus"]["wr"],
                        "recent_pnl": report["recent_0701_plus"]["pnl"],
                        "d0701_n": report["d0701"]["n"],
                        "d0701_wr": report["d0701"]["wr"],
                        "d0701_pnl": report["d0701"]["pnl"],
                        "d0702_n": report["d0702"]["n"],
                        "d0702_wr": report["d0702"]["wr"],
                        "d0702_pnl": report["d0702"]["pnl"],
                        "d0703_n": report["d0703"]["n"],
                        "d0703_wr": report["d0703"]["wr"],
                        "d0703_pnl": report["d0703"]["pnl"],
                        **check,
                    }
                )
                detail[key] = {"report": report, "confirm_meta": confirm_meta, "fit_check": check}
                all_trades.extend(confirmed)

    table = pd.DataFrame(reports).sort_values(
        ["survivor", "recent_pnl", "train_pnl", "n"],
        ascending=[False, False, False, False],
    )
    OUT_RULES.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(OUT_RULES, index=False, encoding="utf-8-sig")
    pd.DataFrame(all_trades).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")

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
        "method": {
            "logic": "After an upper-band false breakout re-enters the normal band, wait a few seconds and enter only if price has not continued adversely beyond the tolerance.",
            "anti_overfit": [
                "Delay/tolerance grid is reported in full; do not pick a single best cell without walk-forward confirmation.",
                "Rejected confirmation candidates do not start cooldown; accepted entries settle 10 minutes after the delayed entry.",
                "Train <= 2026-06-30 and recent >= 2026-07-01 are separated.",
            ],
        },
        "base_counts": {"base_upper_quality": len(base_rows)},
        "top": table.head(20).to_dict("records"),
        "details": detail,
        "outputs": {"json": str(OUT_JSON), "trades_csv": str(OUT_TRADES), "rule_report_csv": str(OUT_RULES)},
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    result = run()
    print(
        json.dumps(
            {
                "data": {k: result["data"][k] for k in ("rows_dense", "rows_observed", "observed_pct", "first", "last")},
                "base_counts": result["base_counts"],
                "top": result["top"][:12],
                "outputs": result["outputs"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
