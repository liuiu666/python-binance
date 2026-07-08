from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_normal_state_v1 as v1
import research_normal_state_v3 as v3


OUT_JSON = ROOT / "tmp" / "normal_state_v4_dynamic_regime.json"
OUT_TRADES = ROOT / "tmp" / "normal_state_v4_dynamic_regime_trades.csv"

HORIZON_SEC = 600
WIN_PAY = 0.8
LOSS_PAY = -1.0
BREAKEVEN_WR = abs(LOSS_PAY) / (WIN_PAY + abs(LOSS_PAY)) * 100.0


def finite(value: object) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return out if np.isfinite(out) else float("nan")


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
        return {"n": 0, "wr": 0.0, "pnl": 0.0, "ev": 0.0, "max_dd": 0.0, "days": [], "modules": {}}
    wins = [bool(r["won"]) for r in rows]
    pnl = sum(WIN_PAY if won else LOSS_PAY for won in wins)
    df = pd.DataFrame(rows)
    days = []
    for day, g in df.groupby("day_cn", sort=True):
        gw = [bool(x) for x in g["won"].tolist()]
        gpnl = sum(WIN_PAY if won else LOSS_PAY for won in gw)
        days.append(
            {
                "day": day,
                "n": int(len(g)),
                "wr": round(sum(gw) / len(gw) * 100.0, 2),
                "pnl": round(gpnl, 4),
                "max_dd": max_drawdown(gw),
            }
        )
    modules = {}
    for module, g in df.groupby("module", sort=True):
        gw = [bool(x) for x in g["won"].tolist()]
        modules[module] = {
            "n": int(len(g)),
            "wr": round(sum(gw) / len(gw) * 100.0, 2),
            "pnl": round(sum(WIN_PAY if won else LOSS_PAY for won in gw), 4),
        }
    return {
        "n": int(len(rows)),
        "wr": round(sum(wins) / len(wins) * 100.0, 2),
        "pnl": round(pnl, 4),
        "ev": round(pnl / len(rows), 5),
        "max_dd": max_drawdown(wins),
        "days": days,
        "modules": modules,
    }


def split_report(rows: list[dict]) -> dict:
    return {
        "summary": summarize(rows),
        "train_to_0630": summarize([r for r in rows if r["day_cn"] <= "2026-06-30"]),
        "recent_0701_plus": summarize([r for r in rows if r["day_cn"] >= "2026-07-01"]),
        "d0701": summarize([r for r in rows if r["day_cn"] == "2026-07-01"]),
        "d0702": summarize([r for r in rows if r["day_cn"] == "2026-07-02"]),
        "d0703": summarize([r for r in rows if r["day_cn"] == "2026-07-03"]),
    }


def base_row(
    *,
    bars: pd.DataFrame,
    close: np.ndarray,
    idx: int,
    signal: str,
    module: str,
    regime: str,
    z: float,
    features: dict,
    reason: str,
) -> dict:
    entry = float(close[idx])
    settle = float(close[idx + HORIZON_SEC])
    won = settle > entry if signal == "UP" else settle < entry
    return {
        "idx": int(idx),
        "settle_idx": int(idx + HORIZON_SEC),
        "time": bars.index[idx].isoformat(),
        "day_cn": bars.index[idx].tz_convert("Asia/Shanghai").strftime("%Y-%m-%d"),
        "module": module,
        "regime": regime,
        "signal": signal,
        "entry": round(entry, 2),
        "settle": round(settle, 2),
        "won": bool(won),
        "move_bps": round((settle / entry - 1.0) * 10000.0, 4),
        "z": round(float(z), 4),
        "reason": reason,
        **features,
    }


def feature_at(
    *,
    idx: int,
    side: float,
    sigma10: np.ndarray,
    flow60: np.ndarray,
    ret60: np.ndarray,
    ret300: np.ndarray,
    range600_bps: np.ndarray,
    minute: dict[str, np.ndarray],
    ob: dict[str, np.ndarray],
) -> tuple[dict, dict]:
    width = finite(minute["m_width_ratio"][idx])
    slope60 = finite(minute["m_slope60_bps"][idx])
    bandwalk = finite(minute["m_bandwalk10"][idx])
    cover = finite(minute["m_cover2_120"][idx])
    half_life = finite(minute["m_half_life_min"][idx])
    sig10 = finite(sigma10[idx])
    fl60 = finite(flow60[idx])
    r60 = finite(ret60[idx])
    r300 = finite(ret300[idx])
    rg600 = finite(range600_bps[idx])
    ob_available = bool(ob["ob_available"][idx])
    ob_imb = finite(ob["ob_imb20"][idx])
    ob_micro = finite(ob["ob_micro_bps"][idx])
    raw = {
        "width": width,
        "slope60": slope60,
        "bandwalk": bandwalk,
        "cover": cover,
        "half_life": half_life,
        "sigma10": sig10,
        "flow60": fl60,
        "ret60": r60,
        "ret300": r300,
        "range600_bps": rg600,
        "ob_available": ob_available,
        "ob_imb20": ob_imb,
        "ob_micro_bps": ob_micro,
        "side_slope60": side * slope60,
        "side_flow60": side * fl60,
        "side_ret300": side * r300,
        "side_ob_imb20": side * ob_imb if np.isfinite(ob_imb) else float("nan"),
        "side_ob_micro_bps": side * ob_micro if np.isfinite(ob_micro) else float("nan"),
    }
    rounded = {
        "sigma10_bps": round(sig10, 4) if np.isfinite(sig10) else None,
        "flow60": round(fl60, 5) if np.isfinite(fl60) else None,
        "ret60_bps": round(r60, 4) if np.isfinite(r60) else None,
        "ret300_bps": round(r300, 4) if np.isfinite(r300) else None,
        "range600_bps": round(rg600, 4) if np.isfinite(rg600) else None,
        "m_cover2_120": round(cover, 5) if np.isfinite(cover) else None,
        "m_width_ratio": round(width, 5) if np.isfinite(width) else None,
        "m_slope60_bps": round(slope60, 4) if np.isfinite(slope60) else None,
        "m_bandwalk10": round(bandwalk, 2) if np.isfinite(bandwalk) else None,
        "m_half_life_min": round(half_life, 4) if np.isfinite(half_life) else None,
        "ob_available": ob_available,
        "ob_imb20": round(ob_imb, 5) if np.isfinite(ob_imb) else None,
        "ob_micro_bps": round(ob_micro, 5) if np.isfinite(ob_micro) else None,
    }
    return raw, rounded


def continuation_evidence(raw: dict, side: float, *, squeeze: bool) -> tuple[bool, str]:
    required = ("width", "slope60", "bandwalk", "sigma10", "flow60", "ret300", "range600_bps")
    if not all(np.isfinite(raw[k]) for k in required):
        return False, "feature_nan"

    # Hard blocks: do not call continuation when pressure is visibly fading.
    if raw["side_slope60"] < 20.0:
        return False, "slope_not_aligned"
    if raw["side_flow60"] < -0.10:
        return False, "trade_flow_against"
    if np.isfinite(raw["side_ob_imb20"]) and raw["side_ob_imb20"] < -0.45:
        return False, "orderbook_imb_against"
    if np.isfinite(raw["side_ob_micro_bps"]) and raw["side_ob_micro_bps"] < -0.004:
        return False, "microprice_against"

    if squeeze:
        if not (0.35 <= raw["width"] <= 0.85):
            return False, "not_squeeze_width"
        if raw["side_ret300"] < 8.0:
            return False, "squeeze_no_impulse"
        votes = [
            raw["side_flow60"] >= 0.03,
            np.isfinite(raw["side_ob_imb20"]) and raw["side_ob_imb20"] >= 0.15,
            np.isfinite(raw["side_ob_micro_bps"]) and raw["side_ob_micro_bps"] >= 0.0015,
            raw["range600_bps"] >= 18.0,
        ]
        return sum(votes) >= 2, f"squeeze_votes={sum(votes)}/4"

    if not (0.70 <= raw["width"] <= 3.50):
        return False, "trend_width_bad"
    if raw["bandwalk"] < 7.0:
        return False, "bandwalk_weak"
    votes = [
        raw["side_slope60"] >= 50.0,
        raw["side_flow60"] >= 0.03,
        raw["side_ret300"] >= 12.0,
        np.isfinite(raw["side_ob_imb20"]) and raw["side_ob_imb20"] >= 0.15,
        np.isfinite(raw["side_ob_micro_bps"]) and raw["side_ob_micro_bps"] >= 0.0015,
        raw["sigma10"] >= 15.0,
    ]
    return sum(votes) >= 3, f"trend_votes={sum(votes)}/6"


def generate_continuation_rows(
    bars: pd.DataFrame,
    features: pd.DataFrame,
    ctx: pd.DataFrame,
    *,
    min_outside_sec: int = 30,
    cooldown_sec: int = 600,
) -> list[dict]:
    close = bars["close"].to_numpy(float)
    z_arr = ctx["z"].to_numpy(float)
    sigma10 = ctx["sigma10_bps"].to_numpy(float)
    flow60 = ctx["flow60"].to_numpy(float)
    obs600 = ctx["obs600"].to_numpy(float)
    obs_future = ctx["obs_future"].to_numpy(float)
    close_s = pd.Series(close, index=bars.index)
    ret60 = (close_s / close_s.shift(60) - 1.0).to_numpy(float) * 10000.0
    ret300 = (close_s / close_s.shift(300) - 1.0).to_numpy(float) * 10000.0
    high600 = close_s.rolling(600, min_periods=300).max()
    low600 = close_s.rolling(600, min_periods=300).min()
    range600_bps = ((high600 - low600) / close_s * 10000.0).to_numpy(float)
    minute = {
        key: features[key].to_numpy()
        for key in ("m_width_ratio", "m_slope60_bps", "m_bandwalk10", "m_cover2_120", "m_half_life_min")
    }
    ob = {
        key: features[key].to_numpy()
        for key in ("ob_available", "ob_imb20", "ob_micro_bps")
    }

    rows: list[dict] = []
    last_signal = -10**9
    streak_side = 0.0
    streak_len = 0
    start = max(180 * 60, 7200)
    end = len(close) - HORIZON_SEC - 1
    for idx in range(start, end):
        z = finite(z_arr[idx])
        side = 1.0 if z >= 1.96 else -1.0 if z <= -1.96 else 0.0
        if side == 0.0:
            streak_side = 0.0
            streak_len = 0
            continue
        if side == streak_side:
            streak_len += 1
        else:
            streak_side = side
            streak_len = 1
        if idx - last_signal < cooldown_sec:
            continue
        if streak_len < min_outside_sec:
            continue
        if obs600[idx] < 0.98 or obs_future[idx] < 0.98:
            continue
        side_z = side * z
        if side_z > 3.20:
            continue
        raw, rounded = feature_at(
            idx=idx,
            side=side,
            sigma10=sigma10,
            flow60=flow60,
            ret60=ret60,
            ret300=ret300,
            range600_bps=range600_bps,
            minute=minute,
            ob=ob,
        )
        is_squeeze = raw["width"] <= 0.85 if np.isfinite(raw["width"]) else False
        ok, reason = continuation_evidence(raw, side, squeeze=is_squeeze)
        if not ok:
            continue
        signal = "UP" if side > 0 else "DOWN"
        module = "squeeze_breakout_continue" if is_squeeze else "bandwalk_trend_continue"
        rows.append(
            base_row(
                bars=bars,
                close=close,
                idx=idx,
                signal=signal,
                module=module,
                regime="continuation",
                z=z,
                features={**rounded, "outside_sec": int(streak_len)},
                reason=reason,
            )
        )
        last_signal = idx
    return rows


def generate_upper_reversion_rows(
    bars: pd.DataFrame,
    features: pd.DataFrame,
    ctx: pd.DataFrame,
) -> list[dict]:
    cfg = v3.V3Config(
        name="V4_upper_false_break_revert",
        lookback_min=180,
        reentry_z=1.96,
        max_outside_sec=60,
        side_mode="upper_only",
        min_score_ratio=0.78,
        min_width_ratio=0.45,
        max_width_ratio=3.0,
        max_slope_side_bps=120,
        max_bandwalk10=6,
        max_half_life_min=40,
        max_flow60_side=0.10,
        max_ob_imb_side=0.10,
        max_ob_micro_side=0.001,
        max_peak_abs_z=3.2,
        cooldown_sec=600,
    )
    candidates = v1.generate_reversion_rows(
        bars,
        features,
        lookback_sec=180 * 60,
        second_context=ctx,
        reentry_z=cfg.reentry_z,
        max_outside_sec=900,
        state_filter="none",
        ob_filter="none",
        cooldown_sec=0,
    )
    rows = v3.apply_v3(candidates, cfg)
    for row in rows:
        row["settle_idx"] = int(row["idx"] + HORIZON_SEC)
        row["module"] = "upper_false_break_revert"
        row["regime"] = "mean_reversion"
        row["reason"] = (
            "upper band false breakout re-entered; bandwalk/slope/orderbook did not confirm continuation"
        )
    return rows


def merge_with_priority(rows: list[dict], cooldown_sec: int = 600) -> list[dict]:
    priority = {
        "squeeze_breakout_continue": 0,
        "bandwalk_trend_continue": 1,
        "upper_false_break_revert": 2,
    }
    merged = sorted(rows, key=lambda r: (int(r["idx"]), priority.get(str(r.get("module")), 99)))
    out: list[dict] = []
    last_idx = -10**9
    for row in merged:
        idx = int(row["idx"])
        if idx - last_idx < cooldown_sec:
            continue
        out.append(row)
        last_idx = idx
    return out


def apply_online_risk_gate(
    rows: list[dict],
    *,
    cooldown_sec: int = 600,
    module_loss_streak_limit: int = 2,
    module_pause_sec: int = 7200,
    module_daily_stop_u: float = -2.0,
    global_daily_stop_u: float = -3.0,
) -> tuple[list[dict], dict]:
    priority = {
        "squeeze_breakout_continue": 0,
        "bandwalk_trend_continue": 1,
        "upper_false_break_revert": 2,
    }
    candidates = sorted(rows, key=lambda r: (int(r["idx"]), priority.get(str(r.get("module")), 99)))
    accepted: list[dict] = []
    pending: list[dict] = []
    last_signal_idx = -10**9
    daily_global_pnl: dict[str, float] = {}
    module_day_pnl: dict[tuple[str, str], float] = {}
    module_loss_streak: dict[str, int] = {}
    module_pause_until: dict[str, int] = {}
    day_halted: set[str] = set()
    module_day_halted: set[tuple[str, str]] = set()
    skipped = {
        "cooldown": 0,
        "global_daily_stop": 0,
        "module_daily_stop": 0,
        "module_pause": 0,
        "same_time_lower_priority": 0,
    }
    settled_ptr = 0
    pending.sort(key=lambda r: int(r["settle_idx"]))

    def settle_until(idx: int) -> None:
        nonlocal settled_ptr
        # pending is tiny because accepted entries have a 10 minute global cooldown.
        pending.sort(key=lambda r: int(r["settle_idx"]))
        while settled_ptr < len(pending) and int(pending[settled_ptr]["settle_idx"]) <= idx:
            row = pending[settled_ptr]
            settled_ptr += 1
            day = str(row["day_cn"])
            module = str(row["module"])
            pnl = WIN_PAY if bool(row["won"]) else LOSS_PAY
            daily_global_pnl[day] = daily_global_pnl.get(day, 0.0) + pnl
            key = (day, module)
            module_day_pnl[key] = module_day_pnl.get(key, 0.0) + pnl
            if bool(row["won"]):
                module_loss_streak[module] = 0
            else:
                module_loss_streak[module] = module_loss_streak.get(module, 0) + 1
                if module_loss_streak[module] >= module_loss_streak_limit:
                    module_pause_until[module] = idx + module_pause_sec
            if daily_global_pnl[day] <= global_daily_stop_u:
                day_halted.add(day)
            if module_day_pnl[key] <= module_daily_stop_u:
                module_day_halted.add(key)

    for row in candidates:
        idx = int(row["idx"])
        settle_until(idx)
        day = str(row["day_cn"])
        module = str(row["module"])
        if idx - last_signal_idx < cooldown_sec:
            skipped["cooldown"] += 1
            continue
        if day in day_halted:
            skipped["global_daily_stop"] += 1
            continue
        if (day, module) in module_day_halted:
            skipped["module_daily_stop"] += 1
            continue
        if module_pause_until.get(module, -1) > idx:
            skipped["module_pause"] += 1
            continue
        out = dict(row)
        out["risk_gate"] = "accepted"
        accepted.append(out)
        pending.append(out)
        last_signal_idx = idx

    return accepted, {
        "params": {
            "cooldown_sec": cooldown_sec,
            "module_loss_streak_limit": module_loss_streak_limit,
            "module_pause_sec": module_pause_sec,
            "module_daily_stop_u": module_daily_stop_u,
            "global_daily_stop_u": global_daily_stop_u,
        },
        "skipped": skipped,
    }


def apply_walkforward_module_gate(
    rows: list[dict],
    *,
    cooldown_sec: int = 600,
    min_closed_trades: int = 20,
    lookback_closed_trades: int = 30,
    min_wr_pct: float = BREAKEVEN_WR,
    min_trailing_pnl_u: float = 0.0,
    allow_bootstrap: bool = True,
    learn_from_shadow: bool = False,
) -> tuple[list[dict], dict]:
    priority = {
        "squeeze_breakout_continue": 0,
        "bandwalk_trend_continue": 1,
        "upper_false_break_revert": 2,
    }
    candidates = sorted(rows, key=lambda r: (int(r["idx"]), priority.get(str(r.get("module")), 99)))
    accepted: list[dict] = []
    history_pending: list[dict] = []
    module_history: dict[str, list[bool]] = {}
    last_signal_idx = -10**9
    skipped = {"cooldown": 0, "module_not_ready": 0, "module_below_breakeven": 0}
    settled_ptr = 0

    def settle_until(idx: int) -> None:
        nonlocal settled_ptr
        history_pending.sort(key=lambda r: int(r["settle_idx"]))
        while settled_ptr < len(history_pending) and int(history_pending[settled_ptr]["settle_idx"]) <= idx:
            row = history_pending[settled_ptr]
            settled_ptr += 1
            module_history.setdefault(str(row["module"]), []).append(bool(row["won"]))

    for row in candidates:
        idx = int(row["idx"])
        settle_until(idx)
        module = str(row["module"])
        if idx - last_signal_idx < cooldown_sec:
            skipped["cooldown"] += 1
            if learn_from_shadow:
                history_pending.append(row)
            continue
        hist = module_history.get(module, [])
        if len(hist) < min_closed_trades and not allow_bootstrap:
            skipped["module_not_ready"] += 1
            if learn_from_shadow:
                history_pending.append(row)
            continue
        if len(hist) >= min_closed_trades:
            trailing = hist[-lookback_closed_trades:]
            wins = sum(trailing)
            wr = wins / len(trailing) * 100.0
            pnl = sum(WIN_PAY if won else LOSS_PAY for won in trailing)
            if wr < min_wr_pct or pnl <= min_trailing_pnl_u:
                skipped["module_below_breakeven"] += 1
                if learn_from_shadow:
                    history_pending.append(row)
                continue
        out = dict(row)
        out["risk_gate"] = "walkforward_module_gate"
        out["module_closed_before"] = len(hist)
        if hist:
            trailing = hist[-lookback_closed_trades:]
            out["module_trailing_wr"] = round(sum(trailing) / len(trailing) * 100.0, 2)
            out["module_trailing_pnl"] = round(sum(WIN_PAY if won else LOSS_PAY for won in trailing), 4)
        else:
            out["module_trailing_wr"] = None
            out["module_trailing_pnl"] = None
        accepted.append(out)
        history_pending.append(row if learn_from_shadow else out)
        last_signal_idx = idx

    return accepted, {
        "params": {
            "cooldown_sec": cooldown_sec,
            "min_closed_trades": min_closed_trades,
            "lookback_closed_trades": lookback_closed_trades,
            "min_wr_pct": round(min_wr_pct, 4),
            "min_trailing_pnl_u": min_trailing_pnl_u,
            "allow_bootstrap": allow_bootstrap,
            "learn_from_shadow": learn_from_shadow,
        },
        "skipped": skipped,
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

    reversion_rows = generate_upper_reversion_rows(bars, features, ctx)
    continuation_rows = generate_continuation_rows(bars, features, ctx)
    raw_candidates = reversion_rows + continuation_rows
    combined_rows = merge_with_priority(raw_candidates)
    gated_rows, gate_meta = apply_online_risk_gate(raw_candidates)
    walkforward_rows, walkforward_meta = apply_walkforward_module_gate(raw_candidates)
    conservative_rows, conservative_meta = apply_walkforward_module_gate(
        raw_candidates,
        min_wr_pct=60.0,
        min_trailing_pnl_u=1.0,
        allow_bootstrap=False,
        learn_from_shadow=True,
    )

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
        "anti_overfit": [
            "No parameter grid is selected on recent days in this script.",
            "Rules are pre-declared from market-state logic: mean reversion only after failed upper breakout; continuation only after persistent outside-band pressure with flow/orderbook confirmation.",
            "Report keeps train <= 2026-06-30 and recent >= 2026-07-01 separated.",
        ],
        "rules": {
            "upper_false_break_revert": "Short only after upper-band z>=1.96 re-enters within 60s and trend/bandwalk/orderbook do not strongly confirm continuation.",
            "bandwalk_trend_continue": "Trade with outside-band direction after at least 30s outside, bandwalk>=7, slope/flow/ret/orderbook votes confirm.",
            "squeeze_breakout_continue": "Trade with outside-band direction from compressed width when impulse plus flow/orderbook votes confirm.",
        },
        "module_results_raw": {
            "upper_false_break_revert": split_report(reversion_rows),
            "continuation_all": split_report(continuation_rows),
        },
        "combined": split_report(combined_rows),
        "combined_online_risk_gated": split_report(gated_rows),
        "online_risk_gate": gate_meta,
        "combined_walkforward_module_gated": split_report(walkforward_rows),
        "walkforward_module_gate": walkforward_meta,
        "combined_conservative_walkforward": split_report(conservative_rows),
        "conservative_walkforward_gate": conservative_meta,
        "sample": combined_rows[-50:],
        "sample_gated": gated_rows[-50:],
        "sample_walkforward": walkforward_rows[-50:],
        "sample_conservative": conservative_rows[-50:],
        "outputs": {"json": str(OUT_JSON), "trades_csv": str(OUT_TRADES)},
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(conservative_rows).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    return report


if __name__ == "__main__":
    result = run()
    print(
        json.dumps(
            {
                "data": {k: result["data"][k] for k in ("rows_dense", "rows_observed", "observed_pct", "first", "last")},
                "module_results_raw": {
                    "upper_false_break_revert": result["module_results_raw"]["upper_false_break_revert"]["summary"],
                    "continuation_all": result["module_results_raw"]["continuation_all"]["summary"],
                },
                "combined": result["combined"],
                "combined_online_risk_gated": result["combined_online_risk_gated"],
                "online_risk_gate": result["online_risk_gate"],
                "combined_walkforward_module_gated": result["combined_walkforward_module_gated"],
                "walkforward_module_gate": result["walkforward_module_gate"],
                "combined_conservative_walkforward": result["combined_conservative_walkforward"],
                "conservative_walkforward_gate": result["conservative_walkforward_gate"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
