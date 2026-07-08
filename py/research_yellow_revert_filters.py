from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "tmp" / "yellow_revert_filter_scan.json"
OUT_CSV = ROOT / "tmp" / "yellow_revert_filter_scan.csv"

HORIZON_SEC = 600
WIN_PAY = 0.8
LOSS_PAY = -1.0
BREAKEVEN_WR = abs(LOSS_PAY) / (WIN_PAY + abs(LOSS_PAY))

BASE_SOURCES = [
    ROOT / "tmp" / "research_20260626" / "merged_1s_clean_valid_price.csv",
    ROOT / "tmp" / "latest_pull_20260626_003039" / "btcusdt_1s_trades.csv",
    ROOT / "tmp" / "latest_pull_20260628_231346" / "data" / "btcusdt_1s_trades.csv",
    ROOT / "tmp" / "llm_latest_pull_20260629_223601" / "btcusdt_1s_trades.csv",
    ROOT / "tmp" / "latest_data_pull_20260701_000900" / "latest_data_pull_20260701_000900" / "btcusdt_1s_trades.csv",
]


def discover_sources() -> list[Path]:
    sources = list(BASE_SOURCES)
    sources.extend(sorted((ROOT / "tmp").glob("daily_second_pull_*/second/BTCUSDT/futures/*.csv")))
    sources.extend(sorted((ROOT / "tmp").glob("latest_market_pull_*/second/BTCUSDT/futures/*.csv")))
    sources.extend(sorted((ROOT / "tmp").glob("latest_data_pull_*/second/BTCUSDT/futures/*.csv")))
    sources.extend(sorted((ROOT / "tmp").glob("latest_data_pull_*/*/second/BTCUSDT/futures/*.csv")))
    seen = set()
    out = []
    for path in sources:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def first_existing(columns: list[str], names: tuple[str, ...]) -> str | None:
    present = set(columns)
    return next((name for name in names if name in present), None)


def read_source(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    ts_col = first_existing(list(df.columns), ("timestamp", "ts", "time", "open_time"))
    px_col = first_existing(list(df.columns), ("close", "price"))
    if not ts_col or not px_col:
        raise ValueError(f"{path} missing timestamp/price columns")
    out = pd.DataFrame()
    raw_ts = df[ts_col]
    if pd.api.types.is_numeric_dtype(raw_ts):
        numeric_ts = pd.to_numeric(raw_ts, errors="coerce")
        unit = "ms" if numeric_ts.dropna().median() > 10_000_000_000 else "s"
        out["time"] = pd.to_datetime(numeric_ts, unit=unit, utc=True, errors="coerce")
    else:
        out["time"] = pd.to_datetime(raw_ts, utc=True, errors="coerce")
    out["price"] = pd.to_numeric(df[px_col], errors="coerce")
    if "volume" in df.columns:
        out["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    elif "qty" in df.columns:
        out["volume"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0.0)
    else:
        out["volume"] = 0.0
    if "taker_buy_volume" in df.columns or "taker_sell_volume" in df.columns:
        out["buy_qty"] = pd.to_numeric(df.get("taker_buy_volume", 0.0), errors="coerce").fillna(0.0)
        out["sell_qty"] = pd.to_numeric(df.get("taker_sell_volume", 0.0), errors="coerce").fillna(0.0)
    else:
        out["buy_qty"] = out["volume"] * 0.5
        out["sell_qty"] = out["volume"] * 0.5
    out = out.dropna(subset=["time", "price"])
    out = out[np.isfinite(out["price"]) & (out["price"] > 0)]
    out["second"] = out["time"].dt.floor("s")
    return out[["second", "price", "volume", "buy_qty", "sell_qty"]]


def load_merged_bars() -> tuple[pd.DataFrame, list[dict]]:
    frames = []
    source_info = []
    for path in discover_sources():
        if not path.exists():
            continue
        part = read_source(path)
        frames.append(part)
        source_info.append(
            {
                "file": str(path),
                "rows": int(len(part)),
                "first": part["second"].min().isoformat() if len(part) else None,
                "last": part["second"].max().isoformat() if len(part) else None,
            }
        )
    if not frames:
        raise RuntimeError("no source data found")
    raw = pd.concat(frames, ignore_index=True).sort_values("second")
    agg = raw.groupby("second").agg(
        close=("price", "last"),
        volume=("volume", "sum"),
        buy_qty=("buy_qty", "sum"),
        sell_qty=("sell_qty", "sum"),
    )
    agg["observed"] = True
    idx = pd.date_range(agg.index.min(), agg.index.max(), freq="s", tz="UTC")
    bars = agg.reindex(idx)
    bars["observed"] = bars["observed"].fillna(False).astype(bool)
    bars["close"] = bars["close"].ffill()
    for col in ("volume", "buy_qty", "sell_qty"):
        bars[col] = bars[col].fillna(0.0)
    bars = bars.dropna(subset=["close"])
    bars.index.name = "time"
    return bars, source_info


def max_drawdown(wons: list[bool]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for won in wons:
        equity += WIN_PAY if won else LOSS_PAY
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return round(worst, 4)


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "wr": 0.0, "ev": 0.0, "pnl": 0.0, "max_dd": 0.0, "up": {}, "down": {}, "days": []}
    wins = sum(bool(r["won"]) for r in rows)
    pnl = sum(WIN_PAY if r["won"] else LOSS_PAY for r in rows)
    df = pd.DataFrame(rows)
    days = []
    for day, g in df.groupby("day_cn", sort=True):
        gw = [bool(x) for x in g["won"].tolist()]
        gpnl = sum(WIN_PAY if x else LOSS_PAY for x in gw)
        days.append(
            {
                "day": day,
                "n": int(len(g)),
                "wr": round(sum(gw) / len(gw) * 100, 2),
                "pnl": round(gpnl, 4),
                "max_dd": max_drawdown(gw),
            }
        )
    side = {}
    for signal, g in df.groupby("signal"):
        gw = [bool(x) for x in g["won"].tolist()]
        side[signal] = {"n": int(len(g)), "wr": round(sum(gw) / len(gw) * 100, 2)}
    return {
        "n": int(len(rows)),
        "wr": round(wins / len(rows) * 100, 2),
        "ev": round(pnl / len(rows), 5),
        "pnl": round(pnl, 4),
        "max_dd": max_drawdown([bool(r["won"]) for r in rows]),
        "up": side.get("UP", {"n": 0, "wr": 0.0}),
        "down": side.get("DOWN", {"n": 0, "wr": 0.0}),
        "days": days,
    }


def apply_filter_and_cooldown(events: list[dict], filter_name: str, cooldown: int) -> list[dict]:
    rows = []
    last_signal = -10**9
    for event in events:
        if event["idx"] - last_signal < cooldown:
            continue
        if not feature_filter(filter_name, event):
            continue
        rows.append(event)
        last_signal = int(event["idx"])
    return rows


def build_context(bars: pd.DataFrame, lookback: int) -> dict:
    close = bars["close"].to_numpy(float)
    close_s = pd.Series(close, index=bars.index)
    lr = np.diff(np.log(np.maximum(close, 1e-12)), prepend=np.nan)
    lr_s = pd.Series(lr, index=bars.index)
    observed = bars["observed"].to_numpy(bool)
    buy = bars["buy_qty"].to_numpy(float)
    sell = bars["sell_qty"].to_numpy(float)
    vol = bars["volume"].to_numpy(float)
    abs_lr = pd.Series(np.abs(lr), index=bars.index)
    return {
        "close": close,
        "mean": close_s.rolling(lookback, min_periods=max(300, lookback // 2)).mean().to_numpy(float),
        "std": close_s.rolling(lookback, min_periods=max(300, lookback // 2)).std(ddof=1).to_numpy(float),
        "obs600": pd.Series(observed.astype(float), index=bars.index).rolling(600, min_periods=600).mean().to_numpy(float),
        "obs_future": pd.Series(observed.astype(float), index=bars.index).shift(-HORIZON_SEC).rolling(HORIZON_SEC, min_periods=HORIZON_SEC).mean().to_numpy(float),
        "sigma10": lr_s.rolling(600, min_periods=240).std(ddof=1).to_numpy(float) * math.sqrt(HORIZON_SEC) * 10000.0,
        "atr60": abs_lr.rolling(60, min_periods=20).mean().to_numpy(float),
        "atr3600": abs_lr.rolling(3600, min_periods=1200).mean().to_numpy(float),
        "slope300": (close_s / close_s.shift(300) - 1.0).to_numpy(float) * 10000.0,
        "slope1800": (close_s / close_s.shift(1800) - 1.0).to_numpy(float) * 10000.0,
        "flow60": ((pd.Series(buy - sell, index=bars.index).rolling(60, min_periods=20).sum())
                   / (pd.Series(buy + sell, index=bars.index).rolling(60, min_periods=20).sum() + 1e-12)).to_numpy(float),
        "flow300": ((pd.Series(buy - sell, index=bars.index).rolling(300, min_periods=60).sum())
                    / (pd.Series(buy + sell, index=bars.index).rolling(300, min_periods=60).sum() + 1e-12)).to_numpy(float),
        "vol60": pd.Series(vol, index=bars.index).rolling(60, min_periods=20).sum().to_numpy(float),
        "vol1800": pd.Series(vol, index=bars.index).rolling(1800, min_periods=600).sum().to_numpy(float),
    }


def feature_filter(name: str, r: dict) -> bool:
    if name == "none":
        return True
    if name == "flat_trend":
        return abs(r["slope1800"]) <= 30
    if name == "not_strong_breakout":
        return abs(r["slope1800"]) <= 45 and r["vol_ratio"] <= 1.8
    if name == "sigma_mid":
        return 8 <= r["sigma10"] <= 30
    if name == "vol_normal":
        return 0.55 <= r["vol_ratio"] <= 1.45
    if name == "flow_not_follow":
        return r["side"] * r["flow60"] <= 0.08
    if name == "combo_light":
        return abs(r["slope1800"]) <= 45 and 6 <= r["sigma10"] <= 35 and r["vol_ratio"] <= 1.8
    if name == "combo_strict":
        return abs(r["slope1800"]) <= 30 and 8 <= r["sigma10"] <= 30 and 0.55 <= r["vol_ratio"] <= 1.45 and r["side"] * r["flow60"] <= 0.08
    if name == "anti_chase":
        return r["side"] * r["slope300"] <= 6 and r["side"] * r["flow60"] <= 0.12
    if name == "sigma12_25":
        return 12 <= r["sigma10"] < 25
    if name == "sigma12_30_no_mid_out":
        return 12 <= r["sigma10"] < 30 and not (30 <= r["outside_sec"] < 60)
    if name == "sigma12_25_avoid_bad_slope":
        return 12 <= r["sigma10"] < 25 and not (-45 <= r["slope1800"] < -30 or -15 <= r["slope1800"] < 15)
    if name == "core_all":
        return (
            12 <= r["sigma10"] < 25
            and not (30 <= r["outside_sec"] < 60)
            and not (-45 <= r["slope1800"] < -30 or -15 <= r["slope1800"] < 15)
        )
    return False


def generate_reentry_rows(
    bars: pd.DataFrame,
    *,
    lookback: int,
    ctx: dict | None = None,
    outer_z: float,
    reentry_z: float,
    cooldown: int,
    filter_name: str,
) -> list[dict]:
    if ctx is None:
        ctx = build_context(bars, lookback)
    close = ctx["close"]
    mean = ctx["mean"]
    std = ctx["std"]
    state: dict[str, float | int | str] | None = None
    last_signal = -10**9
    rows = []
    start = max(lookback, 3600)
    end = len(close) - HORIZON_SEC - 1
    for i in range(start, end):
        if ctx["obs600"][i] < 0.98 or ctx["obs_future"][i] < 0.98:
            state = None
            continue
        if not np.isfinite(mean[i]) or not np.isfinite(std[i]) or std[i] <= 0:
            continue
        z = (close[i] - mean[i]) / std[i]
        if not np.isfinite(z):
            continue
        if state is None:
            if z >= outer_z:
                state = {"side_name": "upper", "start": i, "peak_abs_z": abs(float(z))}
            elif z <= -outer_z:
                state = {"side_name": "lower", "start": i, "peak_abs_z": abs(float(z))}
            continue
        state["peak_abs_z"] = max(float(state["peak_abs_z"]), abs(float(z)))
        side_name = str(state["side_name"])
        too_old = i - int(state["start"]) > 900
        invalidated = (side_name == "upper" and z < 0) or (side_name == "lower" and z > 0)
        reentered = (side_name == "upper" and z <= reentry_z) or (side_name == "lower" and z >= -reentry_z)
        if too_old or invalidated:
            state = None
            continue
        if not reentered:
            continue
        if cooldown > 0 and i - last_signal < cooldown:
            state = None
            continue
        signal = "DOWN" if side_name == "upper" else "UP"
        side = -1.0 if signal == "DOWN" else 1.0
        vol_ratio = ctx["atr60"][i] / max(ctx["atr3600"][i], 1e-12)
        vol_spike = ctx["vol60"][i] / max(ctx["vol1800"][i] / 30.0, 1e-12)
        row = {
            "idx": int(i),
            "time": bars.index[i].isoformat(),
            "day_cn": bars.index[i].tz_convert("Asia/Shanghai").strftime("%Y-%m-%d"),
            "signal": signal,
            "side": side,
            "entry": float(close[i]),
            "settle": float(close[i + HORIZON_SEC]),
            "z": float(z),
            "peak_abs_z": float(state["peak_abs_z"]),
            "outside_sec": int(i - int(state["start"])),
            "sigma10": float(ctx["sigma10"][i]),
            "vol_ratio": float(vol_ratio),
            "vol_spike": float(vol_spike),
            "slope300": float(ctx["slope300"][i]),
            "slope1800": float(ctx["slope1800"][i]),
            "flow60": float(ctx["flow60"][i]),
            "flow300": float(ctx["flow300"][i]),
        }
        if not all(np.isfinite(row[k]) for k in ("sigma10", "vol_ratio", "vol_spike", "slope300", "slope1800", "flow60", "flow300")):
            state = None
            continue
        if not feature_filter(filter_name, row):
            state = None
            continue
        row["won"] = bool(row["settle"] > row["entry"] if signal == "UP" else row["settle"] < row["entry"])
        row["move_bps"] = round((row["settle"] / row["entry"] - 1.0) * 10000.0, 4)
        rows.append(row)
        last_signal = i
        state = None
    return rows


def run() -> dict:
    bars, source_info = load_merged_bars()
    context_by_lookback = {lookback_min: build_context(bars, lookback_min * 60) for lookback_min in (30, 60, 120, 180)}
    event_cache = {}
    for lookback_min in (30, 60, 120, 180):
        for reentry_z in (1.96, 1.95, 1.8, 1.6, 1.3, 1.04, 0.7):
            event_cache[(lookback_min, reentry_z)] = generate_reentry_rows(
                bars,
                lookback=lookback_min * 60,
                ctx=context_by_lookback[lookback_min],
                outer_z=1.96,
                reentry_z=reentry_z,
                cooldown=0,
                filter_name="none",
            )
    configs = []
    for lookback_min in (30, 60, 120, 180):
        for reentry_z in (1.96, 1.95, 1.8, 1.6, 1.3, 1.04, 0.7):
            for cooldown in (0, 300, 600):
                for filter_name in (
                    "none",
                    "flat_trend",
                    "not_strong_breakout",
                    "sigma_mid",
                    "vol_normal",
                    "flow_not_follow",
                    "combo_light",
                    "combo_strict",
                    "anti_chase",
                    "sigma12_25",
                    "sigma12_30_no_mid_out",
                    "sigma12_25_avoid_bad_slope",
                    "core_all",
                ):
                    configs.append((lookback_min, reentry_z, cooldown, filter_name))

    rows_out = []
    best_rows_by_key = {}
    for lookback_min, reentry_z, cooldown, filter_name in configs:
        trades = apply_filter_and_cooldown(event_cache[(lookback_min, reentry_z)], filter_name, cooldown)
        s = summarize(trades)
        score = s["ev"] if s["n"] >= 250 else -999
        key = f"W{lookback_min}_R{reentry_z}_CD{cooldown}_{filter_name}"
        rows_out.append(
            {
                "key": key,
                "lookback_min": lookback_min,
                "outer_z": 1.96,
                "reentry_z": reentry_z,
                "cooldown_sec": cooldown,
                "filter": filter_name,
                "n": s["n"],
                "wr": s["wr"],
                "ev": s["ev"],
                "pnl": s["pnl"],
                "max_dd": s["max_dd"],
                "up_n": s["up"].get("n", 0),
                "up_wr": s["up"].get("wr", 0.0),
                "down_n": s["down"].get("n", 0),
                "down_wr": s["down"].get("wr", 0.0),
                "score": score,
            }
        )
        if s["n"] >= 150 and s["wr"] >= 55:
            best_rows_by_key[key] = {"summary": s, "sample": trades[-20:]}

    scan_df = pd.DataFrame(rows_out).sort_values(["score", "n"], ascending=[False, False])
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    scan_df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    top = scan_df.head(30).to_dict("records")
    robust = scan_df[(scan_df["n"] >= 300)].sort_values(["ev", "n"], ascending=[False, False]).head(20).to_dict("records")
    report = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "data": {
            "sources": source_info,
            "rows_dense": int(len(bars)),
            "rows_observed": int(bars["observed"].sum()),
            "observed_pct": round(float(bars["observed"].mean() * 100), 4),
            "first": bars.index.min().isoformat(),
            "last": bars.index.max().isoformat(),
            "hours_observed": round(float(bars["observed"].sum() / 3600), 2),
        },
        "rule": "price rolling mean/std; first exceed +/-1.96 yellow band, enter reverse after z re-enters threshold; 10m binary expiry",
        "payoff": {"win": WIN_PAY, "loss": LOSS_PAY, "breakeven_wr_pct": round(BREAKEVEN_WR * 100, 2)},
        "top_all_min_250_trades": top,
        "top_robust_min_300_trades": robust,
        "interesting_configs": best_rows_by_key,
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    result = run()
    print(json.dumps({"data": result["data"], "top_robust_min_300_trades": result["top_robust_min_300_trades"][:10]}, ensure_ascii=False, indent=2))
