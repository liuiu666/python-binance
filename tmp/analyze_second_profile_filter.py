import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from analyze_second_volume_time_profile import build_profile_features, load_bars, max_loss_streak


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tmp" / "second_profile_filter_analysis.json"


def normal_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def summarize(rows, sample_hours):
    wins = [r["won"] for r in rows]
    trades = len(wins)
    return {
        "trades": trades,
        "winRate": round(100.0 * sum(wins) / trades, 2) if trades else None,
        "tradesPerDay": round(trades / max(sample_hours / 24.0, 1e-9), 2),
        "maxLoss": max_loss_streak(wins),
        "firstSignal": rows[0]["time"] if rows else None,
        "lastSignal": rows[-1]["time"] if rows else None,
        "sampleSignals": rows[-8:],
    }


def build_second_candidates(bars, lookback=3600, horizon=600, tail_pct=0.23):
    close = bars["close"].to_numpy(dtype=float)
    times = bars.index
    lr = np.diff(np.log(close), prepend=np.nan)
    lr_series = pd.Series(lr)
    mu = lr_series.rolling(lookback, min_periods=60).mean().to_numpy()
    sigma = lr_series.rolling(lookback, min_periods=60).std(ddof=1).to_numpy()
    poc = 1.0 - tail_pct
    rows = []
    for i in range(lookback + 1, len(close) - horizon):
        if not np.isfinite(mu[i]) or not np.isfinite(sigma[i]) or sigma[i] < 1e-12:
            continue
        z = horizon * mu[i] / (math.sqrt(horizon) * sigma[i])
        p_up = normal_cdf(float(z))
        signal = None
        if p_up >= poc:
            signal = "DOWN"
        elif p_up <= tail_pct:
            signal = "UP"
        if not signal:
            continue
        entry = close[i]
        settle = close[i + horizon]
        won = settle > entry if signal == "UP" else settle < entry
        rows.append(
            {
                "idx": i,
                "time": times[i].isoformat(),
                "signal": signal,
                "entry": float(entry),
                "settle": float(settle),
                "p_up": round(float(p_up), 6),
                "won": bool(won),
            }
        )
    return rows


def select_with_gap(candidates, gap_sec, predicate):
    rows = []
    last_idx = -10**12
    for row in candidates:
        if row["idx"] - last_idx < gap_sec:
            continue
        if not predicate(row):
            continue
        rows.append(row)
        last_idx = row["idx"]
    return rows


def attach_profile(candidates, close, features, bin_size):
    out = []
    for row in candidates:
        i = row["idx"]
        price = close[i]
        vah = features["vah"][i]
        val = features["val"][i]
        zone = "unknown"
        dist = 0.0
        if np.isfinite(vah) and np.isfinite(val):
            if price > vah:
                zone = "above"
                dist = (price - vah) / bin_size
            elif price < val:
                zone = "below"
                dist = (val - price) / bin_size
            else:
                zone = "inside"
        breakout_dir = "UP" if zone == "above" else "DOWN" if zone == "below" else None
        profile_conflict = breakout_dir is not None and breakout_dir != row["signal"]
        profile_align = breakout_dir is not None and breakout_dir == row["signal"]
        next_row = {
            **row,
            "entry": round(row["entry"], 2),
            "settle": round(row["settle"], 2),
            "profileZone": zone,
            "profileDistBins": round(float(dist), 3),
            "profileBreakoutDir": breakout_dir,
            "profileConflict": profile_conflict,
            "profileAlign": profile_align,
            "vah": round(float(vah), 2) if np.isfinite(vah) else None,
            "val": round(float(val), 2) if np.isfinite(val) else None,
            "poc": round(float(features["poc"][i]), 2) if np.isfinite(features["poc"][i]) else None,
            "tpoc": round(float(features["tpoc"][i]), 2) if np.isfinite(features["tpoc"][i]) else None,
            "volShare": round(float(features["vol_share"][i]), 4) if np.isfinite(features["vol_share"][i]) else None,
            "timeShare": round(float(features["time_share"][i]), 4) if np.isfinite(features["time_share"][i]) else None,
            "flowRatio": round(float(features["flow_ratio"][i]), 4) if np.isfinite(features["flow_ratio"][i]) else None,
        }
        out.append(next_row)
    return out


def group_stats(rows):
    groups = {}
    for r in rows:
        keys = [
            ("zone", r["profileZone"]),
            ("conflict", str(r["profileConflict"])),
            ("align", str(r["profileAlign"])),
        ]
        for name, value in keys:
            groups.setdefault(f"{name}:{value}", []).append(r)
    return {k: {"count": len(v), "winRate": round(100 * sum(x["won"] for x in v) / len(v), 2)} for k, v in groups.items()}


def main():
    bars = load_bars()
    close = bars["close"].to_numpy(dtype=float)
    volume = bars["volume"].to_numpy(dtype=float)
    buy_qty = bars["buy_qty"].to_numpy(dtype=float)
    sell_qty = bars["sell_qty"].to_numpy(dtype=float)
    times = bars.index
    sample_hours = (times[-1] - times[0]).total_seconds() / 3600.0

    candidates = build_second_candidates(bars)
    results = []
    diagnostics = {}
    for bin_size in [10, 20, 50]:
        features = build_profile_features(close, volume, buy_qty, sell_qty, 3600, bin_size)
        profiled = attach_profile(candidates, close, features, bin_size)
        diagnostics[f"bin{bin_size}"] = group_stats(profiled)
        for gap in [600, 900, 1800]:
            rules = {
                "base": lambda r: True,
                "skip_profile_conflict_dist1": lambda r: not (r["profileConflict"] and r["profileDistBins"] >= 1),
                "skip_profile_conflict_dist2": lambda r: not (r["profileConflict"] and r["profileDistBins"] >= 2),
                "only_profile_align": lambda r: bool(r["profileAlign"]),
                "only_inside_or_align": lambda r: r["profileZone"] == "inside" or bool(r["profileAlign"]),
                "skip_low_share_conflict": lambda r: not (
                    r["profileConflict"]
                    and r["profileDistBins"] >= 1
                    and max(r["volShare"] or 0, r["timeShare"] or 0) < 0.05
                ),
            }
            for rule_name, pred in rules.items():
                rows = select_with_gap(profiled, gap, pred)
                results.append(
                    {
                        "binSize": bin_size,
                        "gapSec": gap,
                        "rule": rule_name,
                        **summarize(rows, sample_hours),
                    }
                )

    ranked = sorted(
        [r for r in results if r["trades"] >= 3],
        key=lambda r: ((r["winRate"] or 0), min(r["tradesPerDay"], 12), -r["maxLoss"]),
        reverse=True,
    )
    payload = {
        "sampleHours": round(sample_hours, 2),
        "method": "second_3600_23_with_volume_time_profile_filter",
        "diagnostics": diagnostics,
        "topRules": ranked[:30],
        "allRules": results,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
