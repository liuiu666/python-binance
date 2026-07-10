"""Shared signal rules for the 10-minute normal/liquidity V2 strategy."""

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class LiquidityV2Rules:
    normal_window_sec: int = 600
    horizon_sec: int = 600
    min_gap_sec: int = 600
    z_entry: float = 1.2
    z_reclaim: float = 0.85
    retest_sec: int = 120
    inside_min: float = 0.55
    observed_min_pct: float = 88.0
    center_slope_sec: int = 300
    center_slope_max_bps: float = 8.0
    sigma_min_bps: float = 5.8
    sigma_max_bps: float = 55.0
    sigma_expand_max: float = 1.9
    orderbook_max_age_sec: int = 3
    ob_imbalance_min: float = 0.08
    micro_min_bps: float = 0.001
    wall_ratio_min: float = 1.0
    flow_guard: float = 0.12
    true_break_flow: float = 0.28
    true_break_imbalance: float = 0.28
    bidwall_trap_enabled: bool = True
    bidwall_trap_ret300_max_bps: float = -5.0
    bidwall_trap_bid20_chg60_min: float = 2.0
    bidwall_trap_ret600_min_bps: float = -20.0
    quality_v2_enabled: bool = True
    quality_v2_down_bid20_chg60_min: float = -0.7
    quality_v2_up_flow60_min: float = -0.063
    trend_space_enabled: bool = False
    trend_space_sigma_expand_max: float = 1.6
    trend_space_center_slope_abs_max_bps: float = 6.0
    trend_space_inside_max: float = 0.75
    trend_space_trend_ret_1800_bps: float = 15.0
    trend_space_up_pos_1800_min: float = 0.72
    trend_space_down_pos_1800_max: float = 0.28
    trend_space_block_countertrend: bool = True
    trend_space_block_upper_fade_pullback: bool = True
    trend_space_short_ret_600_up_bps: float = 12.0
    trend_space_short_pos_600_min: float = 0.65
    mode: str = "reclaim"

    @classmethod
    def from_strategy(cls, strategy):
        return cls(
            **{
                field: getattr(strategy, field)
                for field in cls.__dataclass_fields__
            }
        )

    @classmethod
    def from_config(cls, cfg):
        horizon = int(cfg.get("second_liq_horizon_sec", cfg.get("second_horizon_sec", 600)))
        return cls(
            normal_window_sec=int(cfg.get("second_liq_normal_window_sec", 600)),
            horizon_sec=horizon,
            min_gap_sec=int(cfg.get("second_liq_signal_gap_sec", cfg.get("second_min_gap_sec", horizon))),
            z_entry=float(cfg.get("second_liq_z_entry", 1.2)),
            z_reclaim=float(cfg.get("second_liq_z_reclaim", 0.85)),
            retest_sec=int(cfg.get("second_liq_retest_sec", 120)),
            inside_min=float(cfg.get("second_liq_inside_min", 0.55)),
            observed_min_pct=float(cfg.get("second_liq_observed_min_pct", 88.0)),
            center_slope_sec=int(cfg.get("second_liq_center_slope_sec", 300)),
            center_slope_max_bps=float(cfg.get("second_liq_center_slope_max_bps", 8.0)),
            sigma_min_bps=float(cfg.get("second_liq_sigma_min_bps", 5.8)),
            sigma_max_bps=float(cfg.get("second_liq_sigma_max_bps", 55.0)),
            sigma_expand_max=float(cfg.get("second_liq_sigma_expand_max", 1.9)),
            orderbook_max_age_sec=int(cfg.get("second_liq_orderbook_max_age_sec", 3)),
            ob_imbalance_min=float(cfg.get("second_liq_ob_imbalance_min", 0.08)),
            micro_min_bps=float(cfg.get("second_liq_micro_min_bps", 0.001)),
            wall_ratio_min=float(cfg.get("second_liq_wall_ratio_min", 1.0)),
            flow_guard=float(cfg.get("second_liq_flow_guard", 0.12)),
            true_break_flow=float(cfg.get("second_liq_true_break_flow", 0.28)),
            true_break_imbalance=float(cfg.get("second_liq_true_break_imbalance", 0.28)),
            bidwall_trap_enabled=bool(cfg.get("second_liq_bidwall_trap_enabled", True)),
            bidwall_trap_ret300_max_bps=float(cfg.get("second_liq_bidwall_trap_ret300_max_bps", -5.0)),
            bidwall_trap_bid20_chg60_min=float(cfg.get("second_liq_bidwall_trap_bid20_chg60_min", 2.0)),
            bidwall_trap_ret600_min_bps=float(cfg.get("second_liq_bidwall_trap_ret600_min_bps", -20.0)),
            quality_v2_enabled=bool(cfg.get("second_liq_quality_v2_enabled", True)),
            quality_v2_down_bid20_chg60_min=float(cfg.get("second_liq_quality_v2_down_bid20_chg60_min", -0.7)),
            quality_v2_up_flow60_min=float(cfg.get("second_liq_quality_v2_up_flow60_min", -0.063)),
            trend_space_enabled=bool(cfg.get("second_liq_trend_space_enabled", False)),
            trend_space_sigma_expand_max=float(cfg.get("second_liq_trend_space_sigma_expand_max", 1.6)),
            trend_space_center_slope_abs_max_bps=float(cfg.get("second_liq_trend_space_center_slope_abs_max_bps", 6.0)),
            trend_space_inside_max=float(cfg.get("second_liq_trend_space_inside_max", 0.75)),
            trend_space_trend_ret_1800_bps=float(cfg.get("second_liq_trend_space_trend_ret_1800_bps", 15.0)),
            trend_space_up_pos_1800_min=float(cfg.get("second_liq_trend_space_up_pos_1800_min", 0.72)),
            trend_space_down_pos_1800_max=float(cfg.get("second_liq_trend_space_down_pos_1800_max", 0.28)),
            trend_space_block_countertrend=bool(cfg.get("second_liq_trend_space_block_countertrend", True)),
            trend_space_block_upper_fade_pullback=bool(cfg.get("second_liq_trend_space_block_upper_fade_pullback", True)),
            trend_space_short_ret_600_up_bps=float(cfg.get("second_liq_trend_space_short_ret_600_up_bps", 12.0)),
            trend_space_short_pos_600_min=float(cfg.get("second_liq_trend_space_short_pos_600_min", 0.65)),
            mode=str(cfg.get("second_liq_mode", "reclaim")).lower(),
        )


def safe_float(row, key, default=float("nan")):
    try:
        return float(row.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def rolling_sum(series, window, min_periods=None):
    return series.rolling(int(window), min_periods=min_periods or max(30, int(window) // 3)).sum()


def build_features(data, rules: LiquidityV2Rules):
    close = data["close"].astype(float)
    volume = data["volume"].astype(float).clip(lower=0.0)
    observed = data["observed"].astype(bool).astype(float) if "observed" in data else pd.Series(1.0, index=data.index)
    window = int(rules.normal_window_sec)

    sw = rolling_sum(volume, window)
    sx = rolling_sum(close * volume, window)
    sx2 = rolling_sum(close * close * volume, window)
    mean = close.rolling(window, min_periods=max(120, window // 3)).mean()
    std = close.rolling(window, min_periods=max(120, window // 3)).std(ddof=1)
    vwap = sx / sw.replace(0, np.nan)
    var = sx2 / sw.replace(0, np.nan) - vwap * vwap
    vw_sigma = np.sqrt(var.clip(lower=0.0))
    center = vwap.fillna(mean)
    sigma = vw_sigma.where(vw_sigma > 1e-9, std)
    z = (close - center) / sigma.replace(0, np.nan)

    inside1 = z.abs().le(1.0).astype(float)
    sigma_bps = sigma / close * 10000.0
    sigma_median = sigma_bps.rolling(max(window, 900), min_periods=max(120, window // 3)).median()
    buy_60 = data["buy_qty"].astype(float).rolling(60, min_periods=10).sum()
    sell_60 = data["sell_qty"].astype(float).rolling(60, min_periods=10).sum()
    flow_60 = (buy_60 - sell_60) / (buy_60 + sell_60).replace(0, np.nan)
    bid20 = data["bid_qty_20"].astype(float)
    ask20 = data["ask_qty_20"].astype(float)
    bid_wall = data["bid_wall_qty"].astype(float)
    ask_wall = data["ask_wall_qty"].astype(float)

    out = pd.DataFrame(index=data.index)
    out["close"] = close
    out["center"] = center
    out["sigma"] = sigma
    out["z"] = z
    out["normal_low"] = center - sigma
    out["normal_high"] = center + sigma
    out["inside1_ratio"] = inside1.rolling(window, min_periods=max(120, window // 3)).mean()
    out["observed_pct"] = observed.rolling(min(600, window), min_periods=120).mean() * 100.0
    out["center_slope_bps"] = (center / center.shift(rules.center_slope_sec) - 1.0) * 10000.0
    out["sigma_bps"] = sigma_bps
    out["sigma_expand"] = sigma_bps / sigma_median.replace(0, np.nan)
    out["flow_60"] = flow_60
    out["slope_30_bps"] = (close / close.shift(30) - 1.0) * 10000.0
    out["slope_90_bps"] = (close / close.shift(90) - 1.0) * 10000.0
    out["ret_300s_bps"] = np.log(close / close.shift(300)) * 10000.0
    for sec in (600, 900, 1800, 3600):
        out[f"ret_{sec}s_bps"] = np.log(close / close.shift(sec)) * 10000.0
    for sec in (600, 1800, 3600):
        high = close.rolling(sec, min_periods=max(60, sec // 3)).max()
        low = close.rolling(sec, min_periods=max(60, sec // 3)).min()
        out[f"pos_{sec}s"] = (close - low) / (high - low).replace(0, np.nan)
        out[f"range_{sec}s_bps"] = (high / low - 1.0) * 10000.0
    out["imbalance_5"] = data["imbalance_5"].astype(float)
    out["imbalance_20"] = data["imbalance_20"].astype(float)
    out["micro_bps"] = data["microprice_edge_bps"].astype(float)
    out["spread_bps"] = data["spread_bps"].astype(float)
    out["bid_qty_20"] = bid20
    out["ask_qty_20"] = ask20
    out["bid20_chg_30"] = bid20 / bid20.shift(30).replace(0, np.nan) - 1.0
    out["bid20_chg_60"] = bid20 / bid20.shift(60).replace(0, np.nan) - 1.0
    out["ask20_chg_30"] = ask20 / ask20.shift(30).replace(0, np.nan) - 1.0
    out["wall_balance"] = (bid_wall - ask_wall) / (bid_wall + ask_wall).replace(0, np.nan)
    out["z_max_retest"] = z.rolling(rules.retest_sec, min_periods=10).max()
    out["z_min_retest"] = z.rolling(rules.retest_sec, min_periods=10).min()
    out["ob_available"] = data["ob_available"].astype(bool)
    out["ob_age_sec"] = (
        data["ob_age_sec"].astype(float)
        if "ob_age_sec" in data
        else pd.Series(np.where(out["ob_available"], 0.0, np.nan), index=data.index)
    )
    return out


def normal_ready(row, rules: LiquidityV2Rules):
    checks = [row.get(key) for key in ("z", "inside1_ratio", "observed_pct", "center_slope_bps", "sigma_bps", "sigma_expand")]
    if any(not np.isfinite(float(value)) for value in checks):
        return False
    return (
        float(row["inside1_ratio"]) >= rules.inside_min
        and float(row["observed_pct"]) >= rules.observed_min_pct
        and abs(float(row["center_slope_bps"])) <= rules.center_slope_max_bps
        and rules.sigma_min_bps <= float(row["sigma_bps"]) <= rules.sigma_max_bps
        and float(row["sigma_expand"]) <= rules.sigma_expand_max
    )


def _passive_resistance(row, rules):
    ask = safe_float(row, "ask_qty_20")
    bid = safe_float(row, "bid_qty_20")
    return np.isfinite(ask) and np.isfinite(bid) and float(row["imbalance_20"]) <= -rules.ob_imbalance_min and float(row["micro_bps"]) <= -rules.micro_min_bps and ask >= max(1e-9, bid * rules.wall_ratio_min) and safe_float(row, "ask20_chg_30", 0.0) > -0.55


def _passive_support(row, rules):
    ask = safe_float(row, "ask_qty_20")
    bid = safe_float(row, "bid_qty_20")
    return np.isfinite(ask) and np.isfinite(bid) and float(row["imbalance_20"]) >= rules.ob_imbalance_min and float(row["micro_bps"]) >= rules.micro_min_bps and bid >= max(1e-9, ask * rules.wall_ratio_min) and safe_float(row, "bid20_chg_30", 0.0) > -0.55


def signal_from_row(row, rules: LiquidityV2Rules):
    z = float(row["z"])
    flow = float(row["flow_60"])
    resistance = _passive_resistance(row, rules)
    support = _passive_support(row, rules)
    true_up = flow >= rules.true_break_flow or float(row["imbalance_20"]) >= rules.true_break_imbalance or float(row["micro_bps"]) >= rules.micro_min_bps * 4.0
    true_down = flow <= -rules.true_break_flow or float(row["imbalance_20"]) <= -rules.true_break_imbalance or float(row["micro_bps"]) <= -rules.micro_min_bps * 4.0
    edge_down = z >= rules.z_entry and resistance and flow <= rules.flow_guard and not true_up
    edge_up = z <= -rules.z_entry and support and flow >= -rules.flow_guard and not true_down
    reclaim_down = float(row["z_max_retest"]) >= rules.z_entry and 0.0 <= z <= rules.z_reclaim and resistance and flow <= rules.flow_guard and not true_up
    reclaim_up = float(row["z_min_retest"]) <= -rules.z_entry and -rules.z_reclaim <= z <= 0.0 and support and flow >= -rules.flow_guard and not true_down
    if rules.mode in ("edge", "hybrid") and edge_down:
        return "DOWN", "upper_passive_resistance_fade"
    if rules.mode in ("edge", "hybrid") and edge_up:
        return "UP", "lower_passive_support_fade"
    if rules.mode in ("reclaim", "hybrid") and reclaim_down:
        return "DOWN", "upper_fake_break_reclaim"
    if rules.mode in ("reclaim", "hybrid") and reclaim_up:
        return "UP", "lower_fake_break_reclaim"
    return None, None


def is_bidwall_trap(signal, reason, row, rules):
    if not rules.bidwall_trap_enabled or signal != "UP" or reason != "lower_fake_break_reclaim":
        return False
    ret300 = safe_float(row, "ret_300s_bps")
    bid20_chg60 = safe_float(row, "bid20_chg_60")
    return np.isfinite(ret300) and np.isfinite(bid20_chg60) and ret300 <= rules.bidwall_trap_ret300_max_bps and bid20_chg60 > rules.bidwall_trap_bid20_chg60_min


def quality_v2_veto_code(signal, row, rules):
    if not rules.quality_v2_enabled or not signal:
        return None
    if signal == "DOWN" and np.isfinite(safe_float(row, "bid20_chg_60")) and safe_float(row, "bid20_chg_60") <= rules.quality_v2_down_bid20_chg60_min:
        return "liq_v2_skip_down_bid_fade"
    if signal == "UP" and np.isfinite(safe_float(row, "flow_60")) and safe_float(row, "flow_60") <= rules.quality_v2_up_flow60_min:
        return "liq_v2_skip_up_negative_flow"
    return None


def trend_space_mode(row, rules):
    ret = safe_float(row, "ret_1800s_bps")
    pos = safe_float(row, "pos_1800s")
    if not np.isfinite(ret) or not np.isfinite(pos):
        return "unknown"
    if ret >= rules.trend_space_trend_ret_1800_bps and pos >= rules.trend_space_up_pos_1800_min:
        return "uptrend"
    if ret <= -rules.trend_space_trend_ret_1800_bps and pos <= rules.trend_space_down_pos_1800_max:
        return "downtrend"
    return "range"


def trend_space_veto_code(signal, reason, row, rules):
    if not rules.trend_space_enabled or not signal:
        return None
    sigma_expand = safe_float(row, "sigma_expand")
    center_slope = safe_float(row, "center_slope_bps")
    inside = safe_float(row, "inside1_ratio")
    if np.isfinite(sigma_expand) and sigma_expand > rules.trend_space_sigma_expand_max:
        return "trend_space_sigma_expand_high"
    if np.isfinite(center_slope) and abs(center_slope) > rules.trend_space_center_slope_abs_max_bps:
        return "trend_space_center_slope_high"
    if np.isfinite(inside) and inside > rules.trend_space_inside_max:
        return "trend_space_inside_too_high"
    mode = trend_space_mode(row, rules)
    if rules.trend_space_block_countertrend and signal == "DOWN" and mode == "uptrend":
        return "trend_space_block_down_in_uptrend"
    if rules.trend_space_block_countertrend and signal == "UP" and mode == "downtrend":
        return "trend_space_block_up_in_downtrend"
    if rules.trend_space_block_upper_fade_pullback and signal == "DOWN" and reason == "upper_fake_break_reclaim" and safe_float(row, "ret_600s_bps") > rules.trend_space_short_ret_600_up_bps and safe_float(row, "pos_600s") > rules.trend_space_short_pos_600_min:
        return "trend_space_block_short_pullback_up"
    return None


def evaluate_candidate(row, rules: LiquidityV2Rules):
    signal, reason = signal_from_row(row, rules)
    result = {
        "status": "wait",
        "signal": signal,
        "reason": reason,
        "candidate_signal": signal,
        "candidate_reason": reason,
        "raw_signal": signal,
        "raw_reason": reason,
        "bidwall_trap": False,
        "veto_type": None,
    }
    if not signal:
        return result
    result["bidwall_trap"] = is_bidwall_trap(signal, reason, row, rules)
    if result["bidwall_trap"]:
        ret600 = safe_float(row, "ret_600s_bps")
        if np.isfinite(ret600) and ret600 < rules.bidwall_trap_ret600_min_bps:
            return {**result, "status": "veto", "signal": None, "blocked_signal": "DOWN", "reason": "bidwall_trap_extreme_drop_skip", "veto_type": "quality"}
        signal, reason = "DOWN", "lower_reclaim_bidwall_trap_flip_down"
        result.update(signal=signal, reason=reason, candidate_signal=signal, candidate_reason=reason)
    veto = quality_v2_veto_code(signal, row, rules)
    if veto:
        return {**result, "status": "veto", "signal": None, "blocked_signal": signal, "reason": veto, "veto_type": "quality"}
    veto = trend_space_veto_code(signal, reason, row, rules)
    if veto:
        return {**result, "status": "veto", "signal": None, "blocked_signal": signal, "reason": veto, "veto_type": "trend_space"}
    return {**result, "status": "accepted", "signal": signal, "reason": reason}
