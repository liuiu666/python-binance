from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_yellow_revert_filters import load_merged_bars, max_drawdown


OUT_JSON = ROOT / "tmp" / "normal_state_v1_research.json"
OUT_CSV = ROOT / "tmp" / "normal_state_v1_scan.csv"
HORIZON_SEC = 600
WIN_PAY = 0.8
LOSS_PAY = -1.0
BREAKEVEN_WR = abs(LOSS_PAY) / (WIN_PAY + abs(LOSS_PAY))


def latest_file(pattern: str) -> Path | None:
    files = sorted(ROOT.glob(pattern), key=lambda p: p.stat().st_mtime if p.exists() else 0)
    return files[-1] if files else None


def parse_time_series(df: pd.DataFrame) -> pd.Series:
    col = next((c for c in ("timestamp", "time", "ts", "open_time") if c in df.columns), None)
    if not col:
        raise ValueError(f"no timestamp column in {list(df.columns)}")
    return pd.to_datetime(df[col], utc=True, errors="coerce")


def load_minute_features(second_index: pd.DatetimeIndex) -> pd.DataFrame:
    path = (
        latest_file("tmp/latest_data_pull_*/btcusdt_1m.csv")
        or latest_file("tmp/latest_market_pull_*/btcusdt_1m.csv")
        or ROOT / "data" / "btcusdt_1m.csv"
    )
    if path is None or not path.exists():
        raise FileNotFoundError("btcusdt_1m.csv not found")
    df = pd.read_csv(path)
    ts = parse_time_series(df)
    close_col = "close" if "close" in df.columns else "price"
    vol_col = "volume" if "volume" in df.columns else None
    minute = pd.DataFrame(
        {
            "m_close": pd.to_numeric(df[close_col], errors="coerce").to_numpy(float),
            "m_volume": pd.to_numeric(df[vol_col], errors="coerce").fillna(0.0).to_numpy(float) if vol_col else np.zeros(len(df), dtype=float),
        },
        index=ts.to_numpy(),
    ).dropna(subset=["m_close"])
    minute = minute[~minute.index.duplicated(keep="last")].sort_index()
    close = minute["m_close"]
    ret = np.log(close / close.shift(1))
    ma60 = close.rolling(60, min_periods=40).mean()
    sd60 = close.rolling(60, min_periods=40).std(ddof=1)
    z60 = (close - ma60) / sd60.replace(0, np.nan)
    width_bps = 4.0 * sd60 / close * 10000.0
    width_median = width_bps.rolling(360, min_periods=120).median()
    vol_median = minute["m_volume"].rolling(180, min_periods=60).median()
    x = z60.shift(1)
    y = z60
    mean_x = x.rolling(120, min_periods=60).mean()
    mean_y = y.rolling(120, min_periods=60).mean()
    cov_xy = (x * y).rolling(120, min_periods=60).mean() - mean_x * mean_y
    var_x = (x * x).rolling(120, min_periods=60).mean() - mean_x * mean_x
    phi = cov_xy / var_x.replace(0, np.nan)
    half_life = pd.Series(np.nan, index=minute.index, dtype=float)
    mask = (phi > 0.0) & (phi < 0.999)
    half_life.loc[mask] = -math.log(2.0) / np.log(phi.loc[mask])
    features = pd.DataFrame(index=minute.index)
    features["m_z60"] = z60
    features["m_width_bps"] = width_bps
    features["m_width_ratio"] = width_bps / width_median.replace(0, np.nan)
    features["m_slope15_bps"] = (close / close.shift(15) - 1.0) * 10000.0
    features["m_slope60_bps"] = (close / close.shift(60) - 1.0) * 10000.0
    features["m_sigma10_bps"] = ret.rolling(60, min_periods=40).std(ddof=1) * math.sqrt(10) * 10000.0
    features["m_cover2_120"] = (z60.abs() <= 2.0).astype(float).rolling(120, min_periods=80).mean()
    features["m_bandwalk10"] = (z60.abs() >= 1.5).astype(float).rolling(10, min_periods=5).sum()
    features["m_vol_ratio"] = minute["m_volume"] / vol_median.replace(0, np.nan)
    features["m_half_life_min"] = half_life
    aligned = features.reindex(second_index, method="ffill")
    aligned["minute_source"] = str(path)
    return aligned


def load_orderbook_features(second_index: pd.DatetimeIndex) -> pd.DataFrame:
    path = latest_file("tmp/latest_data_pull_*/btcusdt_orderbook_1s.csv") or latest_file("tmp/latest_market_pull_*/btcusdt_orderbook_1s.csv")
    out = pd.DataFrame(index=second_index)
    out["ob_available"] = False
    out["ob_imb20"] = np.nan
    out["ob_micro_bps"] = np.nan
    out["ob_spread_bps"] = np.nan
    if path is None or not path.exists():
        out["orderbook_source"] = ""
        return out
    df = pd.read_csv(path)
    ts = parse_time_series(df).dt.floor("s")
    ob = pd.DataFrame(
        {
            "ob_imb20": pd.to_numeric(df.get("imbalance_20", np.nan), errors="coerce").to_numpy(float),
            "ob_micro_bps": pd.to_numeric(df.get("microprice_edge_bps", np.nan), errors="coerce").to_numpy(float),
            "ob_spread_bps": pd.to_numeric(df.get("spread_bps", np.nan), errors="coerce").to_numpy(float),
            "ob_available": np.ones(len(df), dtype=bool),
        },
        index=ts.to_numpy(),
    ).dropna(how="all")
    ob = ob[~ob.index.duplicated(keep="last")].sort_index()
    aligned = ob.reindex(second_index, method="ffill", limit=5)
    for col in ("ob_imb20", "ob_micro_bps", "ob_spread_bps"):
        out[col] = aligned[col]
    out["ob_available"] = aligned["ob_available"].fillna(False).astype(bool)
    out["orderbook_source"] = str(path)
    return out


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "wr": 0.0, "pnl": 0.0, "ev": 0.0, "max_dd": 0.0, "up": {}, "down": {}, "modes": {}, "days": []}
    wins = sum(bool(r["won"]) for r in rows)
    pnl = sum(WIN_PAY if r["won"] else LOSS_PAY for r in rows)
    df = pd.DataFrame(rows)
    days = []
    for day, g in df.groupby("day_cn", sort=True):
        gw = [bool(x) for x in g["won"].tolist()]
        gpnl = sum(WIN_PAY if x else LOSS_PAY for x in gw)
        days.append({"day": day, "n": int(len(g)), "wr": round(sum(gw) / len(gw) * 100, 2), "pnl": round(gpnl, 4), "max_dd": max_drawdown(gw)})
    side = {}
    for signal, g in df.groupby("signal"):
        gw = [bool(x) for x in g["won"].tolist()]
        side[signal] = {"n": int(len(g)), "wr": round(sum(gw) / len(gw) * 100, 2)}
    modes = {}
    for mode, g in df.groupby("mode"):
        gw = [bool(x) for x in g["won"].tolist()]
        modes[mode] = {"n": int(len(g)), "wr": round(sum(gw) / len(gw) * 100, 2)}
    return {
        "n": int(len(rows)),
        "wr": round(wins / len(rows) * 100, 2),
        "pnl": round(pnl, 4),
        "ev": round(pnl / len(rows), 5),
        "max_dd": max_drawdown([bool(r["won"]) for r in rows]),
        "up": side.get("UP", {"n": 0, "wr": 0.0}),
        "down": side.get("DOWN", {"n": 0, "wr": 0.0}),
        "modes": modes,
        "days": days,
    }


def config_allows_state(name: str, f: dict) -> bool:
    if name == "none":
        return True
    if name == "state_mild":
        return (
            0.84 <= f["m_cover2_120"] <= 0.985
            and 0.60 <= f["m_width_ratio"] <= 1.90
            and abs(f["m_slope60_bps"]) <= 140
            and f["m_bandwalk10"] <= 8
            and 8 <= f["sigma10_bps"] <= 45
        )
    if name == "state_normal":
        return (
            0.86 <= f["m_cover2_120"] <= 0.98
            and 0.70 <= f["m_width_ratio"] <= 1.70
            and abs(f["m_slope60_bps"]) <= 100
            and f["m_bandwalk10"] <= 6
            and 10 <= f["sigma10_bps"] <= 38
        )
    if name == "state_strict":
        return (
            0.88 <= f["m_cover2_120"] <= 0.97
            and 0.80 <= f["m_width_ratio"] <= 1.45
            and abs(f["m_slope60_bps"]) <= 70
            and f["m_bandwalk10"] <= 5
            and 12 <= f["sigma10_bps"] <= 30
        )
    if name == "avoid_bandwalk":
        return f["m_bandwalk10"] <= 5 and abs(f["m_slope60_bps"]) <= 120 and 10 <= f["sigma10_bps"] <= 40
    return False


def orderbook_allows(mode: str, breakout_side: float, f: dict) -> bool:
    if mode == "none" or not f["ob_available"]:
        return True
    continuation = breakout_side * f["ob_imb20"] > 0.35 or breakout_side * f["ob_micro_bps"] > 0.002
    if mode == "block_continuation":
        return not continuation
    if mode == "require_reversion":
        return breakout_side * f["ob_imb20"] <= 0.10 and breakout_side * f["ob_micro_bps"] <= 0.001
    return True


def build_second_context(bars: pd.DataFrame, lookback_sec: int) -> pd.DataFrame:
    close = bars["close"].astype(float)
    lr = np.log(close / close.shift(1))
    mean = close.rolling(lookback_sec, min_periods=max(600, lookback_sec // 2)).mean()
    std = close.rolling(lookback_sec, min_periods=max(600, lookback_sec // 2)).std(ddof=1)
    buy = bars["buy_qty"].astype(float)
    sell = bars["sell_qty"].astype(float)
    flow60 = (buy - sell).rolling(60, min_periods=20).sum() / ((buy + sell).rolling(60, min_periods=20).sum() + 1e-12)
    out = pd.DataFrame(index=bars.index)
    out["z"] = (close - mean) / std.replace(0, np.nan)
    out["sigma10_bps"] = lr.rolling(600, min_periods=240).std(ddof=1) * math.sqrt(HORIZON_SEC) * 10000.0
    out["flow60"] = flow60
    out["obs600"] = bars["observed"].astype(float).rolling(600, min_periods=600).mean()
    out["obs_future"] = bars["observed"].astype(float).shift(-HORIZON_SEC).rolling(HORIZON_SEC, min_periods=HORIZON_SEC).mean()
    return out


def generate_reversion_rows(
    bars: pd.DataFrame,
    all_features: pd.DataFrame,
    *,
    lookback_sec: int,
    second_context: pd.DataFrame | None = None,
    reentry_z: float,
    max_outside_sec: int,
    state_filter: str,
    ob_filter: str,
    cooldown_sec: int,
) -> list[dict]:
    sec = second_context if second_context is not None else build_second_context(bars, lookback_sec)
    close = bars["close"].to_numpy(float)
    z_arr = sec["z"].to_numpy(float)
    sigma10_arr = sec["sigma10_bps"].to_numpy(float)
    flow60_arr = sec["flow60"].to_numpy(float)
    obs600_arr = sec["obs600"].to_numpy(float)
    obs_future_arr = sec["obs_future"].to_numpy(float)
    m_cover_arr = all_features["m_cover2_120"].to_numpy(float)
    m_width_arr = all_features["m_width_ratio"].to_numpy(float)
    m_slope_arr = all_features["m_slope60_bps"].to_numpy(float)
    m_bandwalk_arr = all_features["m_bandwalk10"].to_numpy(float)
    m_half_life_arr = all_features["m_half_life_min"].to_numpy(float) if "m_half_life_min" in all_features else np.full(len(all_features), np.nan)
    ob_available_arr = all_features["ob_available"].to_numpy(bool)
    ob_imb_arr = all_features["ob_imb20"].to_numpy(float)
    ob_micro_arr = all_features["ob_micro_bps"].to_numpy(float)
    rows = []
    state = None
    last_signal = -10**9
    start = max(lookback_sec, 3600)
    end = len(bars) - HORIZON_SEC - 1
    for i in range(start, end):
        z = z_arr[i]
        if not np.isfinite(z):
            continue
        if obs600_arr[i] < 0.98 or obs_future_arr[i] < 0.98:
            state = None
            continue
        if state is None:
            if z >= 1.96:
                state = {"side": 1.0, "start": i, "peak": abs(float(z))}
            elif z <= -1.96:
                state = {"side": -1.0, "start": i, "peak": abs(float(z))}
            continue
        breakout_side = float(state["side"])
        state["peak"] = max(float(state["peak"]), abs(float(z)))
        outside_sec = i - int(state["start"])
        if outside_sec > 900 or breakout_side * z < 0:
            state = None
            continue
        reentered = (breakout_side > 0 and z <= reentry_z) or (breakout_side < 0 and z >= -reentry_z)
        if not reentered:
            continue
        if outside_sec > max_outside_sec or i - last_signal < cooldown_sec:
            state = None
            continue
        sigma10 = sigma10_arr[i]
        flow60 = flow60_arr[i]
        m_cover = m_cover_arr[i]
        m_width = m_width_arr[i]
        m_slope = m_slope_arr[i]
        m_bandwalk = m_bandwalk_arr[i]
        m_half_life = m_half_life_arr[i]
        if not all(np.isfinite(x) for x in (sigma10, flow60, m_cover, m_width, m_slope, m_bandwalk)):
            state = None
            continue
        if breakout_side * flow60 > 0.12:
            state = None
            continue
        f = {
            "sigma10_bps": sigma10,
            "flow60": flow60,
            "m_cover2_120": m_cover,
            "m_width_ratio": m_width,
            "m_slope60_bps": m_slope,
            "m_bandwalk10": m_bandwalk,
            "m_half_life_min": m_half_life,
            "ob_available": bool(ob_available_arr[i]),
            "ob_imb20": ob_imb_arr[i],
            "ob_micro_bps": ob_micro_arr[i],
        }
        if not config_allows_state(state_filter, f) or not orderbook_allows(ob_filter, breakout_side, f):
            state = None
            continue
        signal = "DOWN" if breakout_side > 0 else "UP"
        entry = float(close[i])
        settle = float(close[i + HORIZON_SEC])
        won = settle > entry if signal == "UP" else settle < entry
        rows.append(
            {
                "idx": int(i),
                "time": bars.index[i].isoformat(),
                "day_cn": bars.index[i].tz_convert("Asia/Shanghai").strftime("%Y-%m-%d"),
                "mode": "reversion",
                "signal": signal,
                "breakout_side": breakout_side,
                "entry": round(entry, 2),
                "settle": round(settle, 2),
                "won": bool(won),
                "move_bps": round((settle / entry - 1.0) * 10000.0, 4),
                "z": round(float(z), 4),
                "peak_abs_z": round(float(state["peak"]), 4),
                "outside_sec": int(outside_sec),
                "sigma10_bps": round(float(f["sigma10_bps"]), 4),
                "flow60": round(float(f["flow60"]), 5),
                "m_cover2_120": round(float(f["m_cover2_120"]), 5),
                "m_width_ratio": round(float(f["m_width_ratio"]), 5),
                "m_slope60_bps": round(float(f["m_slope60_bps"]), 4),
                "m_bandwalk10": round(float(f["m_bandwalk10"]), 2),
                "m_half_life_min": None if not np.isfinite(float(f["m_half_life_min"])) else round(float(f["m_half_life_min"]), 4),
                "ob_available": bool(f["ob_available"]),
                "ob_imb20": None if not np.isfinite(float(f["ob_imb20"])) else round(float(f["ob_imb20"]), 5),
                "ob_micro_bps": None if not np.isfinite(float(f["ob_micro_bps"])) else round(float(f["ob_micro_bps"]), 5),
            }
        )
        last_signal = i
        state = None
    return rows


def apply_candidate_filters(
    candidates: list[dict],
    *,
    max_outside_sec: int,
    state_filter: str,
    ob_filter: str,
    side_filter: str = "both",
    cooldown_sec: int = 0,
) -> list[dict]:
    rows = []
    last_signal = -10**9
    for row in candidates:
        if side_filter == "down_only" and row["signal"] != "DOWN":
            continue
        if side_filter == "up_only" and row["signal"] != "UP":
            continue
        if int(row["outside_sec"]) > max_outside_sec:
            continue
        if int(row["idx"]) - last_signal < cooldown_sec:
            continue
        f = dict(row)
        for key in ("ob_imb20", "ob_micro_bps"):
            if f.get(key) is None:
                f[key] = np.nan
        if not config_allows_state(state_filter, f):
            continue
        if not orderbook_allows(ob_filter, float(row["breakout_side"]), f):
            continue
        rows.append(row)
        last_signal = int(row["idx"])
    return rows


def run() -> dict:
    bars, sources = load_merged_bars()
    minute = load_minute_features(bars.index)
    orderbook = load_orderbook_features(bars.index)
    all_features = pd.concat([minute.drop(columns=["minute_source"], errors="ignore"), orderbook.drop(columns=["orderbook_source"], errors="ignore")], axis=1)
    lookback_grid = (30, 180)
    reentry_grid = (1.96, 1.95)
    second_contexts = {lookback_min: build_second_context(bars, lookback_min * 60) for lookback_min in lookback_grid}
    scan_rows = []
    interesting: dict[str, dict] = {}
    candidates_by_key = {}
    for lookback_min in lookback_grid:
        for reentry_z in reentry_grid:
            candidates_by_key[(lookback_min, reentry_z)] = generate_reversion_rows(
                bars,
                all_features,
                lookback_sec=lookback_min * 60,
                second_context=second_contexts[lookback_min],
                reentry_z=reentry_z,
                max_outside_sec=900,
                state_filter="none",
                ob_filter="none",
                cooldown_sec=0,
            )
    for lookback_min in lookback_grid:
        for reentry_z in reentry_grid:
            for max_outside_sec in (5, 15, 30, 120):
                for state_filter in ("none", "state_mild", "state_normal", "state_strict", "avoid_bandwalk"):
                    for ob_filter in ("none", "block_continuation", "require_reversion"):
                        for side_filter in ("both", "down_only"):
                            rows = apply_candidate_filters(
                                candidates_by_key[(lookback_min, reentry_z)],
                                max_outside_sec=max_outside_sec,
                                state_filter=state_filter,
                                ob_filter=ob_filter,
                                side_filter=side_filter,
                                cooldown_sec=0,
                            )
                            s = summarize(rows)
                            train = summarize([r for r in rows if r["day_cn"] <= "2026-06-30"])
                            test = summarize([r for r in rows if r["day_cn"] >= "2026-07-02"])
                            d2 = summarize([r for r in rows if r["day_cn"] == "2026-07-02"])
                            d3 = summarize([r for r in rows if r["day_cn"] == "2026-07-03"])
                            key = f"NSV1_W{lookback_min}_R{reentry_z}_O{max_outside_sec}_{state_filter}_{ob_filter}_{side_filter}"
                            row = {
                                "key": key,
                                "lookback_min": lookback_min,
                                "reentry_z": reentry_z,
                                "max_outside_sec": max_outside_sec,
                                "state_filter": state_filter,
                                "ob_filter": ob_filter,
                                "side_filter": side_filter,
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
                                "up_n": s["up"].get("n", 0),
                                "up_wr": s["up"].get("wr", 0.0),
                                "down_n": s["down"].get("n", 0),
                                "down_wr": s["down"].get("wr", 0.0),
                            }
                            scan_rows.append(row)
                            if train["n"] >= 25 and train["wr"] >= BREAKEVEN_WR * 100 and test["n"] >= 5 and test["wr"] >= 50:
                                interesting[key] = {"summary": s, "train": train, "test": test, "d2": d2, "d3": d3, "sample": rows[-30:]}
    scan = pd.DataFrame(scan_rows)
    scan["score"] = np.where(
        (scan["train_n"] >= 150) & (scan["test_n"] >= 20),
        scan["train_pnl"] + scan["test_pnl"] - scan["max_dd"].abs() * 0.2,
        -9999.0,
    )
    scan = scan.sort_values(["score", "test_pnl", "train_pnl"], ascending=[False, False, False])
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
        "payoff": {"win": WIN_PAY, "loss": LOSS_PAY, "breakeven_wr_pct": round(BREAKEVEN_WR * 100, 2)},
        "rule": "1m state filter + second-level normal band reentry + optional orderbook continuation block; 10m binary expiry",
        "top": scan.head(30).to_dict("records"),
        "interesting": interesting,
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    result = run()
    print(json.dumps({"data": result["data"], "top": result["top"][:15]}, ensure_ascii=False, indent=2))
