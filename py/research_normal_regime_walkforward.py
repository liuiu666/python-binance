from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from second_backtest.data import load_second_bars


DATA_DIR = ROOT / "tmp" / "normal_multiday_20260630" / "second"
LATEST_CSV = ROOT / "tmp" / "normal_research_latest_20260630" / "btcusdt_1s_trades.csv"
OUT = ROOT / "tmp" / "normal_regime_walkforward_20260630.json"

HORIZON = 600
PAYOUT_WIN = 0.8
PAYOUT_LOSS = -1.0


@dataclass(frozen=True)
class Rule:
    name: str
    zone_w: int
    zthr: float
    mode: str
    filter_name: str


RULES = [
    Rule("R600_z1645_revert_slope1800_down_mild", 600, 1.645, "revert", "slope1800[-50,-15)"),
    Rule("R3600_z1282_revert_flow60_sell_mild", 3600, 1.282, "revert", "flow60[-0.2,-0.05)"),
    Rule("R3600_z1282_revert_sig600_15_25", 3600, 1.282, "revert", "sig600[15,25)"),
    Rule("R1800_z1282_revert_volratio_08_12", 1800, 1.282, "revert", "vol_ratio[0.8,1.2)"),
    Rule("B3600_z1645_breakout_recent_core", 3600, 1.645, "breakout", "recent_breakout_core"),
    Rule("BASE3600_z1282_revert", 3600, 1.282, "revert", "none"),
    Rule("BASE1800_z1282_revert", 1800, 1.282, "revert", "none"),
]


def bps_ret(close: np.ndarray, i: int, sec: int) -> float:
    j = i - sec
    if j < 0 or close[j] <= 0:
        return float("nan")
    return (close[i] / close[j] - 1.0) * 10000.0


def flow_imbalance(buy: np.ndarray, sell: np.ndarray, i: int, sec: int) -> float:
    start = max(0, i - sec + 1)
    b = float(np.nansum(buy[start : i + 1]))
    s = float(np.nansum(sell[start : i + 1]))
    return (b - s) / max(b + s, 1e-12)


def rolling_range(close: np.ndarray, i: int, sec: int) -> tuple[float, float]:
    start = max(0, i - sec + 1)
    w = close[start : i + 1]
    w = w[np.isfinite(w) & (w > 0)]
    if len(w) < min(sec, 300):
        return float("nan"), float("nan")
    lo = float(np.min(w))
    hi = float(np.max(w))
    width = (hi - lo) / max(float(close[i]), 1e-12) * 10000.0
    if hi <= lo:
        return 0.5, width
    return (float(close[i]) - lo) / (hi - lo), width


def max_drawdown_u(wons: list[bool]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for won in wons:
        equity += PAYOUT_WIN if won else PAYOUT_LOSS
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return round(worst, 4)


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "wr": 0.0, "ev": 0.0, "pnl_u": 0.0, "max_dd_u": 0.0, "days": []}
    wons = [bool(r["won"]) for r in rows]
    wins = sum(wons)
    pnl = sum(PAYOUT_WIN if x else PAYOUT_LOSS for x in wons)
    df = pd.DataFrame(rows)
    by_day = []
    for day, g in df.groupby("day_cn", sort=True):
        gw = [bool(x) for x in g["won"].tolist()]
        gpnl = sum(PAYOUT_WIN if x else PAYOUT_LOSS for x in gw)
        by_day.append({
            "day": day,
            "n": int(len(g)),
            "wr": round(sum(gw) / len(gw) * 100.0, 2),
            "pnl_u": round(gpnl, 4),
            "max_dd_u": max_drawdown_u(gw),
        })
    return {
        "n": int(len(rows)),
        "wr": round(wins / len(rows) * 100.0, 2),
        "ev": round(pnl / len(rows), 4),
        "pnl_u": round(pnl, 4),
        "max_dd_u": max_drawdown_u(wons),
        "days": by_day,
    }


def filter_ok(name: str, f: dict) -> bool:
    if name == "none":
        return True
    if name == "slope1800[-50,-15)":
        return -50 <= f["slope1800"] < -15
    if name == "flow60[-0.2,-0.05)":
        return -0.2 <= f["flow60"] < -0.05
    if name == "sig600[15,25)":
        return 15 <= f["sig600"] < 25
    if name == "vol_ratio[0.8,1.2)":
        return 0.8 <= f["vol_ratio"] < 1.2
    if name == "recent_breakout_core":
        return (
            f["sig600"] >= 18
            and f["range600"] >= 18
            and abs(f["slope1800"]) >= 20
            and abs(f["flow60"]) >= 0.03
        )
    return False


def build_context(bars: pd.DataFrame) -> dict:
    close = bars["close"].to_numpy(float)
    buy = bars["buy_qty"].to_numpy(float)
    sell = bars["sell_qty"].to_numpy(float)
    observed = bars["observed"].to_numpy(bool) if "observed" in bars else np.ones(len(bars), dtype=bool)
    close_s = pd.Series(close, index=bars.index)
    lr = np.diff(np.log(np.maximum(close, 1e-12)), prepend=np.nan)
    lr_s = pd.Series(lr, index=bars.index)
    return {
        "close": close,
        "buy": buy,
        "sell": sell,
        "observed": observed,
        "obs600": pd.Series(observed.astype(float), index=bars.index).rolling(600, min_periods=600).mean().to_numpy(float),
        "ma": {w: close_s.rolling(w, min_periods=max(240, w // 2)).mean().to_numpy(float) for w in {r.zone_w for r in RULES}},
        "std": {w: close_s.rolling(w, min_periods=max(240, w // 2)).std(ddof=1).to_numpy(float) for w in {r.zone_w for r in RULES}},
        "sig600": lr_s.rolling(600, min_periods=240).std(ddof=1).to_numpy(float) * math.sqrt(HORIZON) * 10000.0,
        "sig3600": lr_s.rolling(3600, min_periods=1200).std(ddof=1).to_numpy(float) * math.sqrt(HORIZON) * 10000.0,
    }


def features(ctx: dict, i: int) -> dict:
    close = ctx["close"]
    pos600, range600 = rolling_range(close, i, 600)
    return {
        "slope600": bps_ret(close, i, 600),
        "slope1800": bps_ret(close, i, 1800),
        "slope3600": bps_ret(close, i, 3600),
        "flow60": flow_imbalance(ctx["buy"], ctx["sell"], i, 60),
        "flow300": flow_imbalance(ctx["buy"], ctx["sell"], i, 300),
        "sig600": float(ctx["sig600"][i]),
        "sig3600": float(ctx["sig3600"][i]),
        "vol_ratio": float(ctx["sig600"][i] / max(ctx["sig3600"][i], 1e-12)),
        "pos600": pos600,
        "range600": range600,
    }


def signal_for(rule: Rule, ctx: dict, i: int) -> tuple[str | None, float]:
    price = float(ctx["close"][i])
    mu = float(ctx["ma"][rule.zone_w][i])
    sd = float(ctx["std"][rule.zone_w][i])
    if not np.isfinite(mu) or not np.isfinite(sd) or sd <= 0:
        return None, float("nan")
    z = (price - mu) / sd
    if abs(z) < rule.zthr:
        return None, z
    if rule.mode == "revert":
        return ("DOWN" if z > 0 else "UP"), z
    if rule.mode == "breakout":
        return ("UP" if z > 0 else "DOWN"), z
    return None, z


def generate_rule_rows(bars: pd.DataFrame, rule: Rule, gap_sec: int = 600) -> list[dict]:
    ctx = build_context(bars)
    close = ctx["close"]
    rows = []
    last_i = -10**9
    start = max(7200, rule.zone_w)
    end = len(close) - HORIZON
    for i in range(start, end):
        if i - last_i < gap_sec:
            continue
        if ctx["obs600"][i] < 0.98:
            continue
        f = features(ctx, i)
        if not all(np.isfinite(v) for v in f.values()):
            continue
        if not filter_ok(rule.filter_name, f):
            continue
        signal, z = signal_for(rule, ctx, i)
        if not signal:
            continue
        entry = float(close[i])
        settle = float(close[i + HORIZON])
        won = settle > entry if signal == "UP" else settle < entry
        last_i = i
        rows.append({
            "time": bars.index[i].isoformat(),
            "day_cn": bars.index[i].tz_convert("Asia/Shanghai").strftime("%Y-%m-%d"),
            "rule": rule.name,
            "signal": signal,
            "z": round(float(z), 4),
            "entry": round(entry, 2),
            "settle": round(settle, 2),
            "won": bool(won),
            **{k: round(float(v), 6) for k, v in f.items()},
        })
    return rows


def generate_dynamic_rows(bars: pd.DataFrame) -> list[dict]:
    priority = [
        "R1800_z1282_revert_volratio_08_12",
        "R600_z1645_revert_slope1800_down_mild",
        "R3600_z1282_revert_sig600_15_25",
        "R3600_z1282_revert_flow60_sell_mild",
        "B3600_z1645_breakout_recent_core",
    ]
    rule_map = {r.name: r for r in RULES}
    ctx = build_context(bars)
    close = ctx["close"]
    rows = []
    last_i = -10**9
    for i in range(7200, len(close) - HORIZON):
        if i - last_i < 600:
            continue
        if ctx["obs600"][i] < 0.98:
            continue
        f = features(ctx, i)
        if not all(np.isfinite(v) for v in f.values()):
            continue
        chosen = None
        chosen_z = float("nan")
        chosen_signal = None
        for name in priority:
            rule = rule_map[name]
            if not filter_ok(rule.filter_name, f):
                continue
            signal, z = signal_for(rule, ctx, i)
            if signal:
                chosen, chosen_signal, chosen_z = rule, signal, z
                break
        if not chosen:
            continue
        entry = float(close[i])
        settle = float(close[i + HORIZON])
        won = settle > entry if chosen_signal == "UP" else settle < entry
        last_i = i
        rows.append({
            "time": bars.index[i].isoformat(),
            "day_cn": bars.index[i].tz_convert("Asia/Shanghai").strftime("%Y-%m-%d"),
            "rule": "DYNAMIC_NORMAL_REGIME",
            "child_rule": chosen.name,
            "signal": chosen_signal,
            "z": round(float(chosen_z), 4),
            "entry": round(entry, 2),
            "settle": round(settle, 2),
            "won": bool(won),
            **{k: round(float(v), 6) for k, v in f.items()},
        })
    return rows


def merge_latest_overlay(files: list[Path]) -> dict[str, pd.DataFrame]:
    by_day: dict[str, pd.DataFrame] = {}
    for p in files:
        part = load_second_bars(p, include_shards=False)
        day = p.stem
        by_day[day] = part

    if LATEST_CSV.exists():
        latest = load_second_bars(LATEST_CSV, include_shards=False)
        latest_days = pd.Series(latest.index.tz_convert("Asia/Shanghai").strftime("%Y-%m-%d"), index=latest.index)
        for day, part in latest.groupby(latest_days):
            existing = by_day.get(day)
            if existing is None:
                by_day[day] = part
            else:
                merged = pd.concat([existing, part]).sort_index()
                by_day[day] = merged[~merged.index.duplicated(keep="last")]
    return dict(sorted(by_day.items()))


def merge_rows_by_day(per_day_rows: list[list[dict]]) -> list[dict]:
    rows = [row for part in per_day_rows for row in part]
    rows.sort(key=lambda r: r["time"])
    return rows


def run() -> dict:
    shard_files = sorted(p for p in DATA_DIR.glob("2026-06-*.csv") if p.stat().st_size > 3_000_000)
    bars_by_day = merge_latest_overlay(shard_files)
    results = {}
    for rule in RULES:
        rows = merge_rows_by_day([generate_rule_rows(bars, rule) for bars in bars_by_day.values()])
        results[rule.name] = summarize(rows)
    dynamic_rows = merge_rows_by_day([generate_dynamic_rows(bars) for bars in bars_by_day.values()])
    results["DYNAMIC_NORMAL_REGIME"] = summarize(dynamic_rows)
    dyn_df = pd.DataFrame(dynamic_rows)
    child = []
    if not dyn_df.empty:
        for name, g in dyn_df.groupby("child_rule"):
            child.append({"child_rule": name, **summarize(g.to_dict("records"))})
    report = {
        "period": {
            "start": min(b.index.min() for b in bars_by_day.values()).isoformat(),
            "end": max(b.index.max() for b in bars_by_day.values()).isoformat(),
            "rows": int(sum(len(b) for b in bars_by_day.values())),
            "observed_pct": round(float(np.mean([b["observed"].mean() for b in bars_by_day.values()]) * 100.0), 4),
            "files": [p.name for p in shard_files],
            "latest_overlay": str(LATEST_CSV) if LATEST_CSV.exists() else None,
            "per_day": {
                day: {
                    "start": bars.index.min().isoformat(),
                    "end": bars.index.max().isoformat(),
                    "rows": int(len(bars)),
                    "observed_pct": round(float(bars["observed"].mean() * 100.0), 4),
                }
                for day, bars in bars_by_day.items()
            },
        },
        "payoff": {"win": PAYOUT_WIN, "loss": PAYOUT_LOSS, "breakeven_wr_pct": 55.56},
        "results": results,
        "dynamic_child_breakdown": child,
        "sample_dynamic": dynamic_rows[-30:],
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
