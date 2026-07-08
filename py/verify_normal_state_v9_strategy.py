from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_normal_state_v3 as v3
from second_backtest.normal_state_v9 import NormalStateV9Config, generate_normal_state_v9_signals, load_default_features


REFERENCE_CSV = ROOT / "tmp" / "normal_state_v9_state_gate_trades.csv"
OUT_JSON = ROOT / "tmp" / "normal_state_v9_strategy_verify.json"
REFERENCE_KEY = "D5_A5_V6_CONSENSUS_3OF5_UPPER_avoid_slow_persistent_edge"


def _read_reference() -> pd.DataFrame:
    if not REFERENCE_CSV.exists():
        raise FileNotFoundError(f"missing {REFERENCE_CSV}; run research_normal_state_v9_state_gate.py first")
    df = pd.read_csv(REFERENCE_CSV)
    ref = df[df["strategy_key"].eq(REFERENCE_KEY)].copy()
    ref = ref.sort_values("idx").drop_duplicates(["idx", "settle_idx", "signal", "entry"], keep="last")
    return ref


def _wins(rows: list[dict]) -> int:
    return sum(1 for row in rows if bool(row["won"]))


def _pnl(rows: list[dict]) -> float:
    return round(sum(0.8 if bool(row["won"]) else -1.0 for row in rows), 4)


def run() -> dict:
    bars, second_sources = v3.load_merged_bars_v3()
    features, feature_meta = load_default_features(bars.index)
    cfg = NormalStateV9Config()
    rows = generate_normal_state_v9_signals(bars, cfg, features=features)
    ref = _read_reference()

    generated = pd.DataFrame(rows)
    if not generated.empty:
        generated["time_iso"] = pd.to_datetime(generated["time"], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        generated["signal_time_iso"] = pd.to_datetime(generated["signal_time"], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    ref["time_iso"] = pd.to_datetime(ref["time"], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    ref["signal_time_iso"] = pd.to_datetime(ref["signal_time"], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")

    compare_cols = ["idx", "settle_idx", "signal", "time_iso", "signal_time_iso", "won"]
    gen_records = generated[compare_cols].to_dict("records") if not generated.empty else []
    ref_records = ref[compare_cols].to_dict("records") if not ref.empty else []
    exact_match = gen_records == ref_records

    value_mismatches = []
    if len(generated) == len(ref):
        for i, (g, r) in enumerate(zip(generated.to_dict("records"), ref.to_dict("records"))):
            for col in ("entry", "settle", "confirm_adverse_bps"):
                if round(float(g[col]), 4) != round(float(r[col]), 4):
                    value_mismatches.append(
                        {
                            "row": i,
                            "column": col,
                            "generated": round(float(g[col]), 4),
                            "reference": round(float(r[col]), 4),
                        }
                    )
    else:
        value_mismatches.append({"row_count": {"generated": len(generated), "reference": len(ref)}})

    generated_rows = generated.to_dict("records")
    report = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "config": cfg.__dict__,
        "data": {
            "rows_dense": int(len(bars)),
            "rows_observed": int(bars["observed"].sum()),
            "observed_pct": round(float(bars["observed"].mean() * 100.0), 4),
            "first": bars.index.min().isoformat(),
            "last": bars.index.max().isoformat(),
            "second_sources": second_sources,
            **feature_meta,
        },
        "generated": {
            "n": int(len(generated)),
            "wins": int(_wins(generated_rows)),
            "wr": round(_wins(generated_rows) / max(len(generated_rows), 1) * 100.0, 2) if generated_rows else 0.0,
            "pnl": _pnl(generated_rows),
        },
        "reference": {
            "strategy_key": REFERENCE_KEY,
            "n": int(len(ref)),
            "wins": int(ref["won"].astype(bool).sum()) if not ref.empty else 0,
            "wr": round(float(ref["won"].astype(bool).mean() * 100.0), 2) if not ref.empty else 0.0,
            "pnl": round(sum(0.8 if bool(x) else -1.0 for x in ref["won"].tolist()), 4) if not ref.empty else 0.0,
        },
        "checks": {
            "row_count_match": int(len(generated)) == int(len(ref)),
            "sequence_match": exact_match,
            "value_mismatch_count": len(value_mismatches),
            "passed": bool(exact_match and not value_mismatches),
        },
        "value_mismatches": value_mismatches[:20],
        "outputs": {"json": str(OUT_JSON)},
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    result = run()
    print(
        json.dumps(
            {
                "generated": result["generated"],
                "reference": result["reference"],
                "checks": result["checks"],
                "outputs": result["outputs"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
