import json
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "tmp" / "server_second_latest" / "btcusdt_1s_trades.csv"
OUT = ROOT / "tmp" / "second_volume_time_profile_analysis.json"


def load_bars():
    df = pd.read_csv(CSV)
    ts_col = "timestamp" if "timestamp" in df.columns else "ts"
    price_col = "close" if "close" in df.columns else "price"
    df[ts_col] = pd.to_datetime(df[ts_col], utc=True, errors="coerce")
    for col in [price_col, "volume", "taker_buy_volume", "taker_sell_volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    df = df.dropna(subset=[ts_col]).sort_values(ts_col)
    bars = pd.DataFrame(
        {
            "time": df[ts_col].dt.floor("s"),
            "close": df[price_col],
            "volume": df.get("volume", 0.0),
            "buy_qty": df.get("taker_buy_volume", df.get("volume", 0.0) * 0.5),
            "sell_qty": df.get("taker_sell_volume", df.get("volume", 0.0) * 0.5),
        }
    )
    bars = bars.groupby("time", as_index=True).agg(
        close=("close", "last"),
        volume=("volume", "sum"),
        buy_qty=("buy_qty", "sum"),
        sell_qty=("sell_qty", "sum"),
    )
    idx = pd.date_range(bars.index.min(), bars.index.max(), freq="s", tz="UTC")
    bars = bars.reindex(idx)
    bars["close"] = bars["close"].ffill()
    for col in ["volume", "buy_qty", "sell_qty"]:
        bars[col] = bars[col].fillna(0.0)
    return bars.dropna(subset=["close"])


def max_loss_streak(wins):
    cur = 0
    best = 0
    for won in wins:
        if won:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return best


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
        "sampleSignals": rows[-5:],
    }


def build_profile_features(close, volume, buy_qty, sell_qty, lookback, bin_size, value_area=0.7):
    bin_id = np.rint(close / bin_size).astype(int)
    uniq = np.arange(bin_id.min(), bin_id.max() + 1)
    offset = int(uniq[0])
    m = len(uniq)
    n = len(close)
    counts = np.zeros(m, dtype=float)
    vols = np.zeros(m, dtype=float)
    buys = np.zeros(m, dtype=float)
    sells = np.zeros(m, dtype=float)

    poc = np.full(n, np.nan)
    tpoc = np.full(n, np.nan)
    hybrid = np.full(n, np.nan)
    vah = np.full(n, np.nan)
    val = np.full(n, np.nan)
    vol_share = np.full(n, np.nan)
    time_share = np.full(n, np.nan)
    flow_ratio = np.full(n, np.nan)

    for i in range(n):
        b = bin_id[i] - offset
        counts[b] += 1.0
        vols[b] += volume[i]
        buys[b] += buy_qty[i]
        sells[b] += sell_qty[i]
        if i >= lookback:
            old = bin_id[i - lookback] - offset
            counts[old] -= 1.0
            vols[old] -= volume[i - lookback]
            buys[old] -= buy_qty[i - lookback]
            sells[old] -= sell_qty[i - lookback]
        if i < lookback:
            continue
        total_vol = float(vols.sum())
        total_count = float(counts.sum())
        if total_count <= 0:
            continue
        active = counts > 0
        if not active.any():
            continue
        vol_score = vols / max(total_vol, 1e-12)
        time_score = counts / max(total_count, 1e-12)
        hybrid_score = 0.65 * vol_score + 0.35 * time_score
        poc_idx = int(np.argmax(vol_score))
        tpoc_idx = int(np.argmax(time_score))
        hybrid_idx = int(np.argmax(hybrid_score))

        order = np.argsort(vol_score)[::-1]
        acc = 0.0
        selected = []
        for j in order:
            if counts[j] <= 0:
                continue
            selected.append(j)
            acc += vol_score[j]
            if acc >= value_area:
                break
        if not selected:
            continue
        cur_idx = bin_id[i] - offset
        poc[i] = (poc_idx + offset) * bin_size
        tpoc[i] = (tpoc_idx + offset) * bin_size
        hybrid[i] = (hybrid_idx + offset) * bin_size
        vah[i] = (max(selected) + offset) * bin_size
        val[i] = (min(selected) + offset) * bin_size
        if 0 <= cur_idx < m:
            vol_share[i] = vol_score[cur_idx]
            time_share[i] = time_score[cur_idx]
            flow_ratio[i] = buys[cur_idx] / max(sells[cur_idx], 1e-12)

    return {
        "bin_id": bin_id,
        "poc": poc,
        "tpoc": tpoc,
        "hybrid": hybrid,
        "vah": vah,
        "val": val,
        "vol_share": vol_share,
        "time_share": time_share,
        "flow_ratio": flow_ratio,
    }


def run_rule(close, times, features, horizon, gap, rule_name, dist_bins, bin_size, min_share=0.0):
    rows = []
    last_idx = -10**12
    prev_zone = "inside"
    for i in range(len(close) - horizon):
        vah = features["vah"][i]
        val = features["val"][i]
        if not np.isfinite(vah) or not np.isfinite(val):
            continue
        price = close[i]
        zone = "above" if price > vah else "below" if price < val else "inside"
        signal = None
        distance = 0.0
        if zone == "above":
            distance = (price - vah) / bin_size
        elif zone == "below":
            distance = (val - price) / bin_size

        share = max(
            0.0 if not np.isfinite(features["vol_share"][i]) else float(features["vol_share"][i]),
            0.0 if not np.isfinite(features["time_share"][i]) else float(features["time_share"][i]),
        )
        if share < min_share:
            prev_zone = zone
            continue

        if rule_name == "outside_revert":
            if zone == "above" and distance >= dist_bins:
                signal = "DOWN"
            elif zone == "below" and distance >= dist_bins:
                signal = "UP"
        elif rule_name == "outside_continue":
            if zone == "above" and distance >= dist_bins:
                signal = "UP"
            elif zone == "below" and distance >= dist_bins:
                signal = "DOWN"
        elif rule_name == "reenter_revert":
            if zone == "inside" and prev_zone == "above":
                signal = "DOWN"
            elif zone == "inside" and prev_zone == "below":
                signal = "UP"
        elif rule_name == "reenter_continue":
            if zone == "inside" and prev_zone == "above":
                signal = "UP"
            elif zone == "inside" and prev_zone == "below":
                signal = "DOWN"

        prev_zone = zone
        if signal is None or i - last_idx < gap:
            continue
        entry = close[i]
        settle = close[i + horizon]
        won = settle > entry if signal == "UP" else settle < entry
        rows.append(
            {
                "time": times[i].isoformat(),
                "signal": signal,
                "entry": round(float(entry), 2),
                "settle": round(float(settle), 2),
                "zone": zone,
                "vah": round(float(vah), 2),
                "val": round(float(val), 2),
                "poc": round(float(features["poc"][i]), 2) if np.isfinite(features["poc"][i]) else None,
                "tpoc": round(float(features["tpoc"][i]), 2) if np.isfinite(features["tpoc"][i]) else None,
                "hybrid": round(float(features["hybrid"][i]), 2) if np.isfinite(features["hybrid"][i]) else None,
                "distBins": round(float(distance), 3),
                "volShare": round(float(features["vol_share"][i]), 4) if np.isfinite(features["vol_share"][i]) else None,
                "timeShare": round(float(features["time_share"][i]), 4) if np.isfinite(features["time_share"][i]) else None,
                "flowRatio": round(float(features["flow_ratio"][i]), 4) if np.isfinite(features["flow_ratio"][i]) else None,
                "won": bool(won),
            }
        )
        last_idx = i
    return rows


def main():
    bars = load_bars()
    close = bars["close"].to_numpy(dtype=float)
    volume = bars["volume"].to_numpy(dtype=float)
    buy_qty = bars["buy_qty"].to_numpy(dtype=float)
    sell_qty = bars["sell_qty"].to_numpy(dtype=float)
    times = bars.index
    sample_hours = (times[-1] - times[0]).total_seconds() / 3600.0
    lookback = 3600
    horizon = 600

    results = []
    for bin_size in [10, 20, 50]:
        features = build_profile_features(close, volume, buy_qty, sell_qty, lookback, bin_size)
        for rule in ["outside_revert", "outside_continue", "reenter_revert", "reenter_continue"]:
            for gap in [600, 900, 1800]:
                for dist_bins in [0, 1, 2, 3]:
                    if rule.startswith("reenter") and dist_bins:
                        continue
                    rows = run_rule(close, times, features, horizon, gap, rule, dist_bins, bin_size)
                    results.append(
                        {
                            "lookbackSec": lookback,
                            "horizonSec": horizon,
                            "binSize": bin_size,
                            "rule": rule,
                            "gapSec": gap,
                            "distBins": dist_bins,
                            **summarize(rows, sample_hours),
                        }
                    )

    ranked = sorted(
        [r for r in results if r["trades"] >= 3],
        key=lambda r: ((r["winRate"] or 0), min(r["tradesPerDay"], 12), -r["maxLoss"]),
        reverse=True,
    )
    balanced = sorted(
        [r for r in results if r["trades"] >= 5],
        key=lambda r: (r["winRate"] or 0) + min(r["tradesPerDay"], 12) * 0.3 - max(0, r["maxLoss"] - 1) * 4,
        reverse=True,
    )
    payload = {
        "source": str(CSV),
        "sampleHours": round(sample_hours, 2),
        "start": times[0].isoformat(),
        "end": times[-1].isoformat(),
        "method": "volume_time_profile_value_area",
        "definition": {
            "volumePOC": "price bin with max volume in the 60m rolling window",
            "timePOC": "price bin with max seconds stayed in the 60m rolling window",
            "valueArea": "price bins covering 70% of rolling volume",
            "outside_revert": "above value area => DOWN, below value area => UP",
            "outside_continue": "above value area => UP, below value area => DOWN",
            "reenter": "signal when price comes back into value area",
        },
        "topByWinRate": ranked[:25],
        "topBalanced": balanced[:25],
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
