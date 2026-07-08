from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from second_backtest.data import load_second_bars


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT / "tmp" / "latest_pull_20260626_003039" / "btcusdt_1s_trades.csv"
DEFAULT_ORDERBOOK = ROOT / "tmp" / "latest_pull_20260626_003039" / "btcusdt_orderbook_1s.csv"
DEFAULT_OUT = ROOT / "tmp" / "volume_value_area_strategy.json"


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(float(x) / math.sqrt(2.0)))


@dataclass(frozen=True)
class Config:
    name: str
    lookback_sec: int = 4200
    horizon_sec: int = 600
    tail_pct: float = 0.25
    sigma_min_bps: float = 8.0
    sigma_max_bps: float = 80.0
    value_area_sec: int = 4200
    bin_size: float = 10.0
    value_pct: float = 0.70
    normal_window_sec: int = 600
    normal_coverage: float = 0.70
    mode: str = "inside_reversion"
    min_edge_bps: float = 0.0
    min_flow: float = 0.0
    min_trend_bps: float = 1.0
    min_volume_ratio: float = 1.15
    min_ob_imbalance: float = 0.05
    min_micro_bps: float = 0.001
    max_against_ob_imbalance: float | None = None
    max_against_flow: float | None = None
    retest_sec: int = 180
    retest_bps: float = 4.0
    break_hold_sec: int = 45
    reclaim_bps: float = 1.0
    absorption_max_progress_bps: float = 1.5
    gap_sec: int = 600
    eval_step_sec: int = 5
    loss_pause_after: int = 0
    loss_pause_sec: int = 0


def load_orderbook_features(path: str | Path, index: pd.DatetimeIndex) -> pd.DataFrame | None:
    path = Path(path)
    if not path.exists():
        return None
    cols = ["timestamp", "imbalance_5", "imbalance_20", "microprice_edge_bps", "spread_bps"]
    raw = pd.read_csv(path, usecols=lambda c: c in cols)
    if "timestamp" not in raw.columns:
        return None
    raw["time"] = pd.to_datetime(raw["timestamp"], utc=True, errors="coerce").dt.floor("s")
    raw = raw.dropna(subset=["time"]).drop_duplicates("time", keep="last").set_index("time").sort_index()
    for col in ("imbalance_5", "imbalance_20", "microprice_edge_bps", "spread_bps"):
        if col in raw.columns:
            raw[col] = pd.to_numeric(raw[col], errors="coerce")
    out = raw.reindex(index).ffill(limit=3)
    return out


def value_area(close: np.ndarray, volume: np.ndarray, idx: int, seconds: int, bin_size: float, value_pct: float):
    start = idx - seconds + 1
    if start < 0:
        return None
    p = close[start : idx + 1]
    v = volume[start : idx + 1]
    mask = np.isfinite(p) & (p > 0) & np.isfinite(v) & (v >= 0)
    p = p[mask]
    v = v[mask]
    if len(p) < max(300, seconds // 4):
        return None
    if float(np.sum(v)) <= 1e-12:
        v = np.ones(len(p), dtype=float)
    bins = np.round(p / bin_size) * bin_size
    hist: dict[float, float] = {}
    for price_bin, qty in zip(bins, v):
        hist[float(price_bin)] = hist.get(float(price_bin), 0.0) + float(qty)
    items = sorted(hist.items())
    prices = np.array([x[0] for x in items], dtype=float)
    vols = np.array([x[1] for x in items], dtype=float)
    total = float(np.sum(vols))
    poc_i = int(np.argmax(vols))
    chosen = {poc_i}
    covered = float(vols[poc_i])
    lo = hi = poc_i
    while covered / total < value_pct and (lo > 0 or hi < len(vols) - 1):
        left_vol = vols[lo - 1] if lo > 0 else -1.0
        right_vol = vols[hi + 1] if hi < len(vols) - 1 else -1.0
        if right_vol >= left_vol:
            hi += 1
            chosen.add(hi)
            covered += float(vols[hi])
        else:
            lo -= 1
            chosen.add(lo)
            covered += float(vols[lo])
    val = float(prices[min(chosen)])
    vah = float(prices[max(chosen)])
    poc = float(prices[poc_i])
    now = float(close[idx])
    width_bps = (vah - val) / now * 10000.0 if now > 0 else float("nan")
    pos = (now - val) / max(vah - val, 1e-12)
    return {
        "val": val,
        "vah": vah,
        "poc": poc,
        "pos": float(pos),
        "width_bps": float(width_bps),
        "outside_up_bps": max(0.0, (now / vah - 1.0) * 10000.0),
        "outside_down_bps": max(0.0, (val / now - 1.0) * 10000.0),
        "inside": bool(val <= now <= vah),
    }


def flow_imbalance(buy_qty: np.ndarray, sell_qty: np.ndarray, idx: int, seconds: int) -> float:
    start = max(0, idx - seconds + 1)
    buy = float(np.nansum(buy_qty[start : idx + 1]))
    sell = float(np.nansum(sell_qty[start : idx + 1]))
    total = buy + sell
    return 0.0 if total <= 1e-12 else (buy - sell) / total


def normal_price_zone(close: np.ndarray, idx: int, seconds: int, coverage: float) -> dict | None:
    start = idx - seconds + 1
    if start < 0:
        return None
    p = close[start : idx + 1]
    p = p[np.isfinite(p) & (p > 0)]
    if len(p) < max(120, seconds // 3):
        return None
    mean = float(np.mean(p))
    sigma = float(np.std(p, ddof=1)) if len(p) > 1 else float("nan")
    if not np.isfinite(sigma) or sigma <= 1e-12:
        return None
    # Two-sided 70% normal interval: mean +/- z*sigma, z ~= Phi^-1(0.85).
    z = 1.036433389 if abs(float(coverage) - 0.70) < 1e-9 else 1.036433389
    low = mean - z * sigma
    high = mean + z * sigma
    now = float(close[idx])
    width = max(high - low, 1e-12)
    return {
        "normal_mean": mean,
        "normal_sigma": sigma,
        "normal_low": float(low),
        "normal_high": float(high),
        "normal_pos": float((now - low) / width),
        "normal_width_bps": float(width / now * 10000.0) if now > 0 else float("nan"),
        "normal_inside": bool(low <= now <= high),
        "normal_outside_up_bps": max(0.0, (now / high - 1.0) * 10000.0),
        "normal_outside_down_bps": max(0.0, (low / now - 1.0) * 10000.0),
    }


def ret_bps(close: np.ndarray, idx: int, seconds: int) -> float:
    if idx - seconds < 0:
        return float("nan")
    base = float(close[idx - seconds])
    now = float(close[idx])
    return (now / max(base, 1e-12) - 1.0) * 10000.0


def volume_ratio(volume: np.ndarray, idx: int, seconds: int, lookback_sec: int) -> float:
    if idx - seconds + 1 < 0:
        return float("nan")
    cur = float(np.nansum(volume[idx - seconds + 1 : idx + 1]))
    start = max(0, idx - lookback_sec + 1)
    hist = pd.Series(volume[start : idx + 1]).rolling(seconds, min_periods=max(10, seconds // 3)).sum()
    hist = hist[np.isfinite(hist)]
    if len(hist) < 5:
        return float("nan")
    baseline = float(np.nanmedian(hist))
    return cur / max(baseline, 1e-12)


def rolling_volume_ratio(volume: np.ndarray, seconds: int, lookback_sec: int) -> np.ndarray:
    vol = pd.Series(volume)
    current = vol.rolling(seconds, min_periods=max(10, seconds // 3)).sum()
    baseline = current.rolling(lookback_sec, min_periods=max(60, lookback_sec // 4)).median()
    out = (current / baseline.replace(0, np.nan)).to_numpy(float)
    return out


def run_config(bars: pd.DataFrame, cfg: Config, orderbook: pd.DataFrame | None = None) -> list[dict]:
    close = bars["close"].to_numpy(float)
    volume = bars["volume"].to_numpy(float)
    buy_qty = bars["buy_qty"].to_numpy(float)
    sell_qty = bars["sell_qty"].to_numpy(float)
    logp = np.log(close)
    lr = np.diff(logp, prepend=np.nan)
    s = pd.Series(lr, index=bars.index)
    mu = s.rolling(cfg.lookback_sec, min_periods=max(60, cfg.lookback_sec // 4)).mean().to_numpy(float)
    sig = s.rolling(cfg.lookback_sec, min_periods=max(60, cfg.lookback_sec // 4)).std(ddof=1).to_numpy(float)
    vol_ratio_60 = rolling_volume_ratio(volume, 60, max(cfg.normal_window_sec, 600))
    rows = []
    last_i = -10**12
    hi = 1.0 - cfg.tail_pct
    start = max(cfg.lookback_sec, cfg.value_area_sec)
    end = len(close) - cfg.horizon_sec
    for i in range(start, end, max(1, int(cfg.eval_step_sec))):
        if i - last_i < cfg.gap_sec:
            continue
        if not np.isfinite(mu[i]) or not np.isfinite(sig[i]) or sig[i] < 1e-12:
            continue
        sigma_bps = math.sqrt(cfg.horizon_sec) * float(sig[i]) * 10000.0
        if not (cfg.sigma_min_bps <= sigma_bps <= cfg.sigma_max_bps):
            continue
        area = value_area(close, volume, i, cfg.value_area_sec, cfg.bin_size, cfg.value_pct)
        if area is None:
            continue
        normal = normal_price_zone(close, i, cfg.normal_window_sec, cfg.normal_coverage)
        if normal is None:
            continue
        z = cfg.horizon_sec * float(mu[i]) / (math.sqrt(cfg.horizon_sec) * float(sig[i]))
        p_up = normal_cdf(z)
        signal = None
        reason = None
        flow = flow_imbalance(buy_qty, sell_qty, i, 300)
        vol_ratio = float(vol_ratio_60[i]) if i < len(vol_ratio_60) else float("nan")
        trend_30s = ret_bps(close, i, 30)
        trend_60s = ret_bps(close, i, 60)
        trend_180s = ret_bps(close, i, 180)
        ob_imb = float(orderbook["imbalance_20"].iloc[i]) if orderbook is not None and "imbalance_20" in orderbook else float("nan")
        ob_micro = float(orderbook["microprice_edge_bps"].iloc[i]) if orderbook is not None and "microprice_edge_bps" in orderbook else float("nan")
        ob_available = bool(np.isfinite(ob_imb) and np.isfinite(ob_micro))
        ob_up = ob_imb >= cfg.min_ob_imbalance and ob_micro >= cfg.min_micro_bps
        ob_down = ob_imb <= -cfg.min_ob_imbalance and ob_micro <= -cfg.min_micro_bps
        flow_up = flow >= cfg.min_flow
        flow_down = flow <= -cfg.min_flow
        confirm_up = flow_up and (orderbook is None or (ob_available and ob_up))
        confirm_down = flow_down and (orderbook is None or (ob_available and ob_down))
        prev_start = max(0, i - cfg.retest_sec)
        prev_high = float(np.nanmax(close[prev_start:i + 1]))
        prev_low = float(np.nanmin(close[prev_start:i + 1]))
        now = float(close[i])
        broke_up_recent = prev_high >= area["vah"] * (1.0 + cfg.min_edge_bps / 10000.0)
        broke_down_recent = prev_low <= area["val"] * (1.0 - cfg.min_edge_bps / 10000.0)
        normal_broke_up_recent = prev_high >= normal["normal_high"] * (1.0 + cfg.min_edge_bps / 10000.0)
        normal_broke_down_recent = prev_low <= normal["normal_low"] * (1.0 - cfg.min_edge_bps / 10000.0)
        hold_start = max(0, i - cfg.break_hold_sec + 1)
        hold_window = close[hold_start : i + 1]
        hold_up = len(hold_window) >= max(5, cfg.break_hold_sec // 2) and bool(
            np.all(hold_window > normal["normal_high"] * (1.0 + cfg.reclaim_bps / 10000.0))
        )
        hold_down = len(hold_window) >= max(5, cfg.break_hold_sec // 2) and bool(
            np.all(hold_window < normal["normal_low"] * (1.0 - cfg.reclaim_bps / 10000.0))
        )
        strong_volume = bool(np.isfinite(vol_ratio) and vol_ratio >= cfg.min_volume_ratio)
        true_break_up = (
            now > normal["normal_high"] * (1.0 + cfg.min_edge_bps / 10000.0)
            and (hold_up or (trend_60s >= cfg.min_trend_bps and strong_volume and confirm_up))
        )
        true_break_down = (
            now < normal["normal_low"] * (1.0 - cfg.min_edge_bps / 10000.0)
            and (hold_down or (trend_60s <= -cfg.min_trend_bps and strong_volume and confirm_down))
        )
        reclaimed_from_up = normal_broke_up_recent and now <= normal["normal_high"] * (1.0 - cfg.reclaim_bps / 10000.0)
        reclaimed_from_down = normal_broke_down_recent and now >= normal["normal_low"] * (1.0 + cfg.reclaim_bps / 10000.0)
        absorption_up = (flow_up or ob_up) and trend_30s <= cfg.absorption_max_progress_bps
        absorption_down = (flow_down or ob_down) and trend_30s >= -cfg.absorption_max_progress_bps
        near_vah = abs(now / area["vah"] - 1.0) * 10000.0 <= cfg.retest_bps
        near_val = abs(now / area["val"] - 1.0) * 10000.0 <= cfg.retest_bps
        if cfg.mode == "inside_reversion":
            if not area["inside"]:
                continue
            if p_up >= hi and area["pos"] >= 0.5 and not confirm_up:
                signal, reason = "DOWN", "inside_va_upper_reversion"
            elif p_up <= cfg.tail_pct and area["pos"] <= 0.5 and not confirm_down:
                signal, reason = "UP", "inside_va_lower_reversion"
        elif cfg.mode == "outside_continue":
            if orderbook is not None and not ob_available:
                continue
            if area["outside_up_bps"] >= cfg.min_edge_bps and p_up >= hi and confirm_up:
                signal, reason = "UP", "outside_vah_continue"
            elif area["outside_down_bps"] >= cfg.min_edge_bps and p_up <= cfg.tail_pct and confirm_down:
                signal, reason = "DOWN", "outside_val_continue"
        elif cfg.mode == "outside_fade_no_flow":
            if orderbook is not None and not ob_available:
                continue
            if area["outside_up_bps"] >= cfg.min_edge_bps and p_up >= hi and not confirm_up:
                signal, reason = "DOWN", "outside_vah_fake_break"
            elif area["outside_down_bps"] >= cfg.min_edge_bps and p_up <= cfg.tail_pct and not confirm_down:
                signal, reason = "UP", "outside_val_fake_break"
        elif cfg.mode == "pullback_continue":
            if orderbook is not None and not ob_available:
                continue
            if broke_up_recent and near_vah and now >= area["vah"] and p_up >= 0.5 and confirm_up:
                signal, reason = "UP", "vah_pullback_continue"
            elif broke_down_recent and near_val and now <= area["val"] and p_up <= 0.5 and confirm_down:
                signal, reason = "DOWN", "val_pullback_continue"
        elif cfg.mode == "failed_break_fade":
            if orderbook is not None and not ob_available:
                continue
            if broke_up_recent and area["inside"] and now < area["vah"] and not confirm_up:
                signal, reason = "DOWN", "vah_failed_break_fade"
            elif broke_down_recent and area["inside"] and now > area["val"] and not confirm_down:
                signal, reason = "UP", "val_failed_break_fade"
        elif cfg.mode == "normal70_liquidity_v2":
            if orderbook is not None and not ob_available:
                continue
            if true_break_up or true_break_down:
                continue
            if reclaimed_from_up and not confirm_up:
                signal, reason = "DOWN", "normal70_up_fake_break_revert"
            elif reclaimed_from_down and not confirm_down:
                signal, reason = "UP", "normal70_down_fake_break_revert"
            elif normal["normal_inside"] and normal["normal_pos"] >= 0.90 and not confirm_up and trend_30s <= cfg.min_trend_bps:
                signal, reason = "DOWN", "normal70_upper_reversion"
            elif normal["normal_inside"] and normal["normal_pos"] <= 0.10 and not confirm_down and trend_30s >= -cfg.min_trend_bps:
                signal, reason = "UP", "normal70_lower_reversion"
            if signal == "DOWN" and confirm_up and not absorption_up:
                signal, reason = None, None
            elif signal == "UP" and confirm_down and not absorption_down:
                signal, reason = None, None
        if not signal:
            continue
        entry = float(close[i])
        settle = float(close[i + cfg.horizon_sec])
        won = settle > entry if signal == "UP" else settle < entry
        rows.append({
            "time": bars.index[i].isoformat(),
            "signal": signal,
            "won": bool(won),
            "pnl": 4 if won else -5,
            "entry": entry,
            "settle": settle,
            "p_up": round(float(p_up), 6),
            "sigma_10m_bps": round(float(sigma_bps), 6),
            "reason": reason,
            "val": area["val"],
            "vah": area["vah"],
            "poc": area["poc"],
            "va_pos": round(float(area["pos"]), 6),
            "va_width_bps": round(float(area["width_bps"]), 6),
            "outside_up_bps": round(float(area["outside_up_bps"]), 6),
            "outside_down_bps": round(float(area["outside_down_bps"]), 6),
            "normal_low": round(float(normal["normal_low"]), 4),
            "normal_high": round(float(normal["normal_high"]), 4),
            "normal_mean": round(float(normal["normal_mean"]), 4),
            "normal_pos": round(float(normal["normal_pos"]), 6),
            "normal_width_bps": round(float(normal["normal_width_bps"]), 6),
            "normal_inside": bool(normal["normal_inside"]),
            "normal_broke_up_recent": bool(normal_broke_up_recent),
            "normal_broke_down_recent": bool(normal_broke_down_recent),
            "true_break_up": bool(true_break_up),
            "true_break_down": bool(true_break_down),
            "reclaimed_from_up": bool(reclaimed_from_up),
            "reclaimed_from_down": bool(reclaimed_from_down),
            "flow_5m": round(float(flow), 6),
            "trend_30s_bps": None if not np.isfinite(trend_30s) else round(float(trend_30s), 6),
            "trend_60s_bps": None if not np.isfinite(trend_60s) else round(float(trend_60s), 6),
            "trend_180s_bps": None if not np.isfinite(trend_180s) else round(float(trend_180s), 6),
            "volume_ratio_60s": None if not np.isfinite(vol_ratio) else round(float(vol_ratio), 6),
            "absorption_up": bool(absorption_up),
            "absorption_down": bool(absorption_down),
            "ob_available": ob_available,
            "ob_imbalance_20": None if not ob_available else round(float(ob_imb), 6),
            "ob_micro_bps": None if not ob_available else round(float(ob_micro), 6),
            "prev_high": round(float(prev_high), 4),
            "prev_low": round(float(prev_low), 4),
        })
        last_i = i
    return rows


def apply_adaptive_execution(rows: list[dict], cfg: Config) -> list[dict]:
    has_loss_pause = cfg.loss_pause_after > 0 and cfg.loss_pause_sec > 0
    has_against_filter = cfg.max_against_flow is not None or cfg.max_against_ob_imbalance is not None
    if not has_loss_pause and not has_against_filter:
        return rows
    selected: list[dict] = []
    pause_until = pd.Timestamp.min.tz_localize("UTC")
    loss_streak = 0
    next_unsettled = 0
    for row in rows:
        signal_time = pd.Timestamp(row["time"])
        while next_unsettled < len(selected):
            prev = selected[next_unsettled]
            settle_time = pd.Timestamp(prev["time"]) + pd.Timedelta(seconds=int(prev.get("horizon_sec", cfg.horizon_sec)))
            if settle_time > signal_time:
                break
            if prev["won"]:
                loss_streak = 0
            else:
                loss_streak += 1
                if has_loss_pause and loss_streak >= int(cfg.loss_pause_after):
                    pause_until = max(pause_until, settle_time + pd.Timedelta(seconds=int(cfg.loss_pause_sec)))
            next_unsettled += 1
        if signal_time < pause_until:
            continue
        signal = str(row.get("signal") or "")
        if cfg.max_against_flow is not None:
            flow = row.get("flow_5m")
            if flow is None:
                continue
            max_against = float(cfg.max_against_flow)
            if signal == "UP" and float(flow) < -max_against:
                continue
            if signal == "DOWN" and float(flow) > max_against:
                continue
        if cfg.max_against_ob_imbalance is not None:
            ob_imb = row.get("ob_imbalance_20")
            if ob_imb is None:
                continue
            max_against = float(cfg.max_against_ob_imbalance)
            if signal == "UP" and float(ob_imb) < -max_against:
                continue
            if signal == "DOWN" and float(ob_imb) > max_against:
                continue
        selected.append(row)
    return selected


def metrics(rows: list[dict]) -> dict:
    n = len(rows)
    wins = sum(1 for r in rows if r["won"])
    pnl = sum(float(r["pnl"]) for r in rows)
    if not rows:
        return {"trades": 0, "wins": 0, "winRate": None, "pnl": 0, "maxDrawdown": 0, "maxLossStreak": 0}
    equity = np.cumsum([float(r["pnl"]) for r in rows])
    peak = np.maximum.accumulate(np.maximum(equity, 0.0))
    max_drawdown = float(np.max(peak - equity)) if len(equity) else 0.0
    max_loss_streak = 0
    cur_loss_streak = 0
    for row in rows:
        if row["won"]:
            cur_loss_streak = 0
        else:
            cur_loss_streak += 1
            max_loss_streak = max(max_loss_streak, cur_loss_streak)
    by_day = {}
    for r in rows:
        day = str(pd.Timestamp(r["time"]).tz_convert("Asia/Shanghai").date())
        by_day.setdefault(day, []).append(r)
    return {
        "trades": n,
        "wins": wins,
        "winRate": round(wins / n * 100.0, 2),
        "pnl": round(pnl, 2),
        "maxDrawdown": round(max_drawdown, 2),
        "maxLossStreak": max_loss_streak,
        "tradesPerDay": round(n / max(len(by_day), 1), 2),
        "days": len(by_day),
        "byDay": {
            day: {
                "trades": len(items),
                "winRate": round(sum(1 for x in items if x["won"]) / len(items) * 100.0, 2),
                "pnl": round(sum(float(x["pnl"]) for x in items), 2),
            }
            for day, items in sorted(by_day.items())
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--orderbook", default=str(DEFAULT_ORDERBOOK))
    parser.add_argument("--tail-rows", type=int, default=120_000)
    parser.add_argument("--eval-step-sec", type=int, default=10)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--day-cn", default="")
    parser.add_argument("--include-shards", action="store_true")
    args = parser.parse_args()

    bars = load_second_bars(args.csv, include_shards=args.include_shards)
    if args.day_cn:
        day = pd.Timestamp(args.day_cn, tz="Asia/Shanghai")
        start = day.tz_convert("UTC")
        end = (day + pd.Timedelta(days=1)).tz_convert("UTC")
        warmup = pd.Timedelta(seconds=7200)
        bars = bars[(bars.index >= start - warmup) & (bars.index < end)]
    if args.tail_rows and len(bars) > args.tail_rows:
        bars = bars.tail(args.tail_rows)
    orderbook = load_orderbook_features(args.orderbook, bars.index)
    configs = []
    modes = ("inside_reversion", "outside_continue", "outside_fade_no_flow", "pullback_continue", "failed_break_fade")
    value_secs = (1800, 3600, 4200)
    tails = (0.20, 0.25, 0.30)
    if args.quick:
        modes = ("inside_reversion", "outside_continue", "pullback_continue", "failed_break_fade")
        value_secs = (1800, 3600)
        tails = (0.20, 0.25)
    for mode in modes:
        for value_sec in value_secs:
            for tail in tails:
                configs.append(Config(
                    name=f"{mode}_VA{value_sec}_T{int(tail*100)}",
                    value_area_sec=value_sec,
                    lookback_sec=4200,
                    tail_pct=tail,
                    sigma_min_bps=8,
                    sigma_max_bps=80,
                    min_edge_bps=2,
                    min_flow=0.05,
                    min_ob_imbalance=0.05,
                    min_micro_bps=0.001,
                    retest_sec=180,
                    retest_bps=4.0,
                    mode=mode,
                    eval_step_sec=args.eval_step_sec,
                ))
    configs.append(Config(
        name="failed_break_fade_VA3600_E1_R180_CD600",
        value_area_sec=3600,
        lookback_sec=4200,
        tail_pct=0.20,
        sigma_min_bps=8,
        sigma_max_bps=80,
        min_edge_bps=1,
        min_flow=0.05,
        min_ob_imbalance=0.05,
        min_micro_bps=0.001,
        retest_sec=180,
        retest_bps=4.0,
        gap_sec=600,
        mode="failed_break_fade",
        eval_step_sec=args.eval_step_sec,
    ))
    configs.append(Config(
        name="SMART_OBSAFE_LOSS2_VA3600_E1_R180_CD600",
        value_area_sec=3600,
        lookback_sec=4200,
        tail_pct=0.20,
        sigma_min_bps=8,
        sigma_max_bps=80,
        min_edge_bps=1,
        min_flow=0.05,
        min_ob_imbalance=0.05,
        min_micro_bps=0.001,
        max_against_ob_imbalance=0.25,
        retest_sec=180,
        retest_bps=4.0,
        gap_sec=600,
        mode="failed_break_fade",
        eval_step_sec=args.eval_step_sec,
        loss_pause_after=2,
        loss_pause_sec=1800,
    ))
    for normal_window in (600, 900, 1200):
        for hold_sec in (30, 45, 60):
            configs.append(Config(
                name=f"NORMAL70_LIQ_V2_W{normal_window}_H{hold_sec}",
                value_area_sec=3600,
                lookback_sec=4200,
                normal_window_sec=normal_window,
                normal_coverage=0.70,
                tail_pct=0.20,
                sigma_min_bps=8,
                sigma_max_bps=80,
                min_edge_bps=1.5,
                min_flow=0.05,
                min_trend_bps=1.0,
                min_volume_ratio=1.15,
                min_ob_imbalance=0.05,
                min_micro_bps=0.001,
                max_against_ob_imbalance=0.25,
                max_against_flow=0.35,
                retest_sec=180,
                retest_bps=4.0,
                break_hold_sec=hold_sec,
                reclaim_bps=0.8,
                absorption_max_progress_bps=1.5,
                gap_sec=600,
                mode="normal70_liquidity_v2",
                eval_step_sec=args.eval_step_sec,
                loss_pause_after=2,
                loss_pause_sec=1800,
            ))
    results = []
    best_rows = {}
    report_start = report_end = None
    if args.day_cn:
        report_day = pd.Timestamp(args.day_cn, tz="Asia/Shanghai")
        report_start = report_day.tz_convert("UTC")
        report_end = (report_day + pd.Timedelta(days=1)).tz_convert("UTC")
    for cfg in configs:
        rows = run_config(bars, cfg, orderbook=orderbook)
        if report_start is not None and report_end is not None:
            rows = [
                row for row in rows
                if report_start <= pd.Timestamp(row["time"]) < report_end
            ]
        rows = apply_adaptive_execution(rows, cfg)
        m = metrics(rows)
        results.append({"name": cfg.name, "config": cfg.__dict__, **m})
        best_rows[cfg.name] = rows[-20:]
    results.sort(key=lambda x: (x.get("pnl", -999999), x.get("winRate") or 0, x.get("trades", 0)), reverse=True)
    out = {
        "csv": str(args.csv),
        "start": bars.index.min().isoformat(),
        "end": bars.index.max().isoformat(),
        "rows": len(bars),
        "dayCn": args.day_cn,
        "orderbook": str(args.orderbook) if orderbook is not None else None,
        "results": results,
        "sampleRows": {r["name"]: best_rows[r["name"]] for r in results[:5]},
    }
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out": args.out, "top": results[:10]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
