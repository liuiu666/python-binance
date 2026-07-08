from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

import research_normal_state_v1 as v1


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "tmp" / "normal_state_v2_research.json"
OUT_CSV = ROOT / "tmp" / "normal_state_v2_scan.csv"
WIN_PAY = 0.8
LOSS_PAY = -1.0
BREAKEVEN_WR = abs(LOSS_PAY) / (WIN_PAY + abs(LOSS_PAY)) * 100.0


def summarize(rows: list[dict]) -> dict:
    return v1.summarize(rows)


def apply_v2_filter(
    rows: list[dict],
    *,
    max_outside_sec: int,
    min_width_ratio: float,
    max_width_ratio: float,
    max_slope60_bps: float,
    max_bandwalk10: float,
    min_cover2: float,
    max_cover2: float,
    max_half_life_min: float,
    ob_mode: str,
) -> list[dict]:
    out = []
    for row in rows:
        if row["signal"] != "DOWN":
            continue
        if int(row["outside_sec"]) > max_outside_sec:
            continue
        width = float(row.get("m_width_ratio", np.nan))
        slope = float(row.get("m_slope60_bps", np.nan))
        bandwalk = float(row.get("m_bandwalk10", np.nan))
        cover = float(row.get("m_cover2_120", np.nan))
        half_life = float(row.get("m_half_life_min", np.nan))
        if not all(np.isfinite(x) for x in (width, slope, bandwalk, cover, half_life)):
            continue
        # Avoid squeeze continuation, persistent bandwalk, non-normal tail regimes,
        # and slow mean reversion that does not fit a 10 minute binary window.
        if not (min_width_ratio <= width <= max_width_ratio):
            continue
        if slope > max_slope60_bps:
            continue
        if bandwalk > max_bandwalk10:
            continue
        if not (min_cover2 <= cover <= max_cover2):
            continue
        if half_life > max_half_life_min:
            continue
        ob_imb = row.get("ob_imb20")
        ob_micro = row.get("ob_micro_bps")
        ob_ok = row.get("ob_available") and ob_imb is not None and ob_micro is not None
        if ob_mode == "block_strong_up" and ob_ok:
            if float(ob_imb) > 0.35 or float(ob_micro) > 0.002:
                continue
        elif ob_mode == "require_not_up" and ob_ok:
            if float(ob_imb) > 0.10 or float(ob_micro) > 0.001:
                continue
        out.append(row)
    return out


def split_summary(rows: list[dict]) -> dict:
    return {
        "summary": summarize(rows),
        "train": summarize([r for r in rows if r["day_cn"] <= "2026-06-30"]),
        "test": summarize([r for r in rows if r["day_cn"] >= "2026-07-02"]),
        "d2": summarize([r for r in rows if r["day_cn"] == "2026-07-02"]),
        "d3": summarize([r for r in rows if r["day_cn"] == "2026-07-03"]),
    }


def run() -> dict:
    bars, sources = v1.load_merged_bars()
    minute = v1.load_minute_features(bars.index)
    orderbook = v1.load_orderbook_features(bars.index)
    features = pd.concat(
        [
            minute.drop(columns=["minute_source"], errors="ignore"),
            orderbook.drop(columns=["orderbook_source"], errors="ignore"),
        ],
        axis=1,
    )

    candidate_sets: dict[tuple[int, float], list[dict]] = {}
    for lookback_min in (180,):
        ctx = v1.build_second_context(bars, lookback_min * 60)
        for reentry_z in (1.95, 1.96):
            candidate_sets[(lookback_min, reentry_z)] = v1.generate_reversion_rows(
                bars,
                features,
                lookback_sec=lookback_min * 60,
                second_context=ctx,
                reentry_z=reentry_z,
                max_outside_sec=900,
                state_filter="none",
                ob_filter="none",
                cooldown_sec=0,
            )

    scan_rows = []
    interesting: dict[str, dict] = {}
    for (lookback_min, reentry_z), candidates in candidate_sets.items():
        for max_outside_sec in (5, 15, 30):
            for min_width_ratio in (0.45, 0.60):
                for max_width_ratio in (2.2, 3.0):
                    for max_slope60_bps in (30, 70, 120):
                        for max_bandwalk10 in (6, 8):
                            for max_half_life_min in (10, 20, 40, 999):
                                for ob_mode in ("none", "require_not_up"):
                                    rows = apply_v2_filter(
                                        candidates,
                                        max_outside_sec=max_outside_sec,
                                        min_width_ratio=min_width_ratio,
                                        max_width_ratio=max_width_ratio,
                                        max_slope60_bps=max_slope60_bps,
                                        max_bandwalk10=max_bandwalk10,
                                        min_cover2=0.82,
                                        max_cover2=0.99,
                                        max_half_life_min=max_half_life_min,
                                        ob_mode=ob_mode,
                                    )
                                    parts = split_summary(rows)
                                    s, train, test, d2, d3 = (
                                        parts["summary"],
                                        parts["train"],
                                        parts["test"],
                                        parts["d2"],
                                        parts["d3"],
                                    )
                                    key = (
                                        f"NSV2_W{lookback_min}_R{reentry_z}_O{max_outside_sec}"
                                        f"_BW{min_width_ratio}-{max_width_ratio}_SL{max_slope60_bps}"
                                        f"_BWALK{max_bandwalk10}_HL{max_half_life_min}_{ob_mode}"
                                    )
                                    row = {
                                        "key": key,
                                        "lookback_min": lookback_min,
                                        "reentry_z": reentry_z,
                                        "max_outside_sec": max_outside_sec,
                                        "min_width_ratio": min_width_ratio,
                                        "max_width_ratio": max_width_ratio,
                                        "max_slope60_bps": max_slope60_bps,
                                        "max_bandwalk10": max_bandwalk10,
                                        "max_half_life_min": max_half_life_min,
                                        "ob_mode": ob_mode,
                                        "n": s["n"],
                                        "wr": s["wr"],
                                        "pnl": s["pnl"],
                                        "max_dd": s["max_dd"],
                                        "train_n": train["n"],
                                        "train_wr": train["wr"],
                                        "train_pnl": train["pnl"],
                                        "test_n": test["n"],
                                        "test_wr": test["wr"],
                                        "test_pnl": test["pnl"],
                                        "d2_n": d2["n"],
                                        "d2_wr": d2["wr"],
                                        "d2_pnl": d2["pnl"],
                                        "d3_n": d3["n"],
                                        "d3_wr": d3["wr"],
                                        "d3_pnl": d3["pnl"],
                                    }
                                    row["score"] = (
                                        row["train_pnl"]
                                        + row["test_pnl"] * 2.0
                                        - abs(row["max_dd"]) * 0.35
                                        if row["train_n"] >= 25 and row["test_n"] >= 5
                                        else -9999.0
                                    )
                                    scan_rows.append(row)
                                    if (
                                        train["n"] >= 25
                                        and train["wr"] >= BREAKEVEN_WR
                                        and test["n"] >= 5
                                        and test["wr"] >= BREAKEVEN_WR
                                    ):
                                        interesting[key] = {
                                            **parts,
                                            "config": row,
                                            "sample": rows[-30:],
                                        }

    scan = pd.DataFrame(scan_rows).sort_values(["score", "test_pnl", "train_pnl"], ascending=[False, False, False])
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    scan.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    report = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "data": {
            "second_sources": sources,
            "rows_dense": int(len(bars)),
            "rows_observed": int(bars["observed"].sum()),
            "first": bars.index.min().isoformat(),
            "last": bars.index.max().isoformat(),
            "minute_source": minute["minute_source"].iloc[0] if "minute_source" in minute else "",
            "orderbook_source": orderbook["orderbook_source"].iloc[0] if "orderbook_source" in orderbook else "",
        },
        "rule": "Bollinger/normal upper-band false-break reversion only; filters for squeeze, bandwalk, slope, empirical coverage, half-life, and orderbook continuation.",
        "payoff": {"win": WIN_PAY, "loss": LOSS_PAY, "breakeven_wr_pct": round(BREAKEVEN_WR, 2)},
        "top": scan.head(50).to_dict("records"),
        "interesting": interesting,
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    result = run()
    print(json.dumps({"data": result["data"], "top": result["top"][:20]}, ensure_ascii=False, indent=2))
