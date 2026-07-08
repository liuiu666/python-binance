from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

import research_normal_state_v1 as v1
import research_normal_state_v3 as v3
import research_normal_state_v6 as v6
import research_normal_state_v7_confirm_reentry as v7


@dataclass(frozen=True)
class NormalStateV9Config:
    strategy_id: str = "BTC_10min_NORMAL_STATE_V9_D5A5"
    rule_name: str = "V6_CONSENSUS_3OF5_UPPER"
    state_gate: str = "avoid_slow_persistent_edge"
    lookback_sec: int = 180 * 60
    horizon_sec: int = 600
    confirm_delay_sec: int = 5
    max_adverse_bps: float = 5.0
    confirmation_veto: str = "none"
    amount: float = 5.0
    label: str = "normal_state_v9_confirmed_false_break"


def load_default_features(second_index: pd.DatetimeIndex) -> tuple[pd.DataFrame, dict]:
    minute = v1.load_minute_features(second_index)
    orderbook, orderbook_sources = v3.load_orderbook_features_v3(second_index)
    features = pd.concat(
        [
            minute.drop(columns=["minute_source"], errors="ignore"),
            orderbook.drop(columns=["orderbook_sources"], errors="ignore"),
        ],
        axis=1,
    )
    return features, {
        "minute_source": minute["minute_source"].iloc[0] if "minute_source" in minute else "",
        "orderbook_sources": orderbook_sources,
    }


def find_rule_spec(rule_name: str) -> v6.RuleSpec:
    for spec in v6.rule_specs():
        if spec.name == rule_name:
            return spec
    raise ValueError(f"unknown V9 rule_name: {rule_name}")


def state_gate_allows(row: dict[str, Any], gate_name: str) -> tuple[bool, str]:
    bandwalk = v6.finite(row.get("m_bandwalk10"))
    half_life = v6.finite(row.get("m_half_life_min"))
    sigma10 = v6.finite(row.get("sigma10_bps"))
    if gate_name == "none":
        return True, "pass"
    if gate_name == "edge_persistence_lt6":
        ok = np.isfinite(bandwalk) and bandwalk < 6.0
        return ok, "bandwalk_lt6" if ok else "persistent_edge"
    if gate_name == "v15_bw35_or_early_sigma18":
        mild_bandwalk = np.isfinite(bandwalk) and 3.0 <= bandwalk < 6.0
        early_high_vol = np.isfinite(bandwalk) and bandwalk < 3.0 and np.isfinite(sigma10) and sigma10 > 18.0
        if mild_bandwalk:
            return True, "mild_bandwalk_3_5"
        if early_high_vol:
            return True, "early_high_vol_sigma_gt18"
        return False, "v15_state_reject"
    if gate_name == "avoid_slow_persistent_edge":
        bad = np.isfinite(bandwalk) and np.isfinite(half_life) and bandwalk >= 6.0 and half_life > 8.0
        return not bad, "pass" if not bad else "slow_persistent_edge"
    if gate_name == "avoid_lowvol_slow_edge":
        bad = (
            np.isfinite(sigma10)
            and np.isfinite(bandwalk)
            and np.isfinite(half_life)
            and sigma10 < 18.0
            and bandwalk >= 5.0
            and half_life > 8.0
        )
        return not bad, "pass" if not bad else "lowvol_slow_edge"
    raise ValueError(f"unknown V9 state_gate: {gate_name}")


def _side_value(row: dict[str, Any], key: str) -> float:
    side = v6.finite(row.get("breakout_side"))
    value = v6.finite(row.get(key))
    if not np.isfinite(side) or not np.isfinite(value):
        return float("nan")
    return float(side * value)


def confirmation_veto_reason(row: dict[str, Any], veto_name: str) -> str | None:
    name = str(veto_name or "none").lower()
    if name in ("", "none", "off", "false"):
        return None

    adverse = v6.finite(row.get("confirm_adverse_bps"))
    confirm_weak = np.isfinite(adverse) and -1.4 < adverse < 1.0
    ob_available = bool(row.get("ob_available"))
    side_imb = _side_value(row, "ob_imb20")
    side_micro = _side_value(row, "ob_micro_bps")
    ob_weak = ob_available and (
        (np.isfinite(side_imb) and side_imb > -0.35)
        or (np.isfinite(side_micro) and side_micro > -0.0035)
    )

    width = v6.finite(row.get("m_width_ratio"))
    sigma10 = v6.finite(row.get("sigma10_bps"))
    bandwalk = v6.finite(row.get("m_bandwalk10"))
    price_weak = (
        (np.isfinite(width) and np.isfinite(sigma10) and width > 2.2 and sigma10 < 18.0)
        or (np.isfinite(bandwalk) and np.isfinite(sigma10) and bandwalk <= 3.0 and sigma10 < 15.0)
    )

    if name == "ob_confirm_weak" and ob_weak and confirm_weak:
        return "ob_confirm_weak"
    if name == "ob_weak" and ob_weak:
        return "ob_weak"
    if name == "price_confirm_weak" and price_weak and confirm_weak:
        return "price_confirm_weak"
    if name == "ob_or_price_weak" and ((ob_weak and confirm_weak) or (price_weak and confirm_weak)):
        return "ob_or_price_weak"
    if name not in ("ob_confirm_weak", "ob_weak", "price_confirm_weak", "ob_or_price_weak"):
        raise ValueError(f"unknown confirmation_veto: {veto_name}")
    return None


def generate_normal_state_v9_signals(
    bars: pd.DataFrame,
    cfg: NormalStateV9Config = NormalStateV9Config(),
    *,
    features: pd.DataFrame | None = None,
) -> list[dict]:
    if features is None:
        features, _ = load_default_features(bars.index)

    ctx = v1.build_second_context(bars, int(cfg.lookback_sec))
    base_rows = v7.prepare_base_rows(bars, features, ctx)
    spec = find_rule_spec(cfg.rule_name)

    candidates: list[dict] = []
    skipped = {"rule": 0, "state_gate": 0}
    for row in base_rows:
        ok, rule_detail = v6.rule_allows(row, spec)
        if not ok:
            skipped["rule"] += 1
            continue
        state_ok, state_reason = state_gate_allows(row, cfg.state_gate)
        if not state_ok:
            skipped["state_gate"] += 1
            continue
        out = dict(row)
        votes_n, votes = v6.consensus_votes(row)
        out["source_rule"] = spec.name
        out["state_gate"] = cfg.state_gate
        out["state_gate_reason"] = state_reason
        out["rule_filter_detail"] = rule_detail
        out["consensus_votes"] = votes_n
        out["consensus_vote_names"] = ",".join(votes)
        candidates.append(out)

    confirmed, confirm_meta = v7.apply_confirmation(
        candidates,
        bars,
        delay_sec=int(cfg.confirm_delay_sec),
        max_adverse_bps=float(cfg.max_adverse_bps),
        cooldown_sec=0,
    )

    rows: list[dict] = []
    last_entry_idx = -10**9
    skipped_veto = 0
    skipped_gap = 0
    signal_gap_sec = int(getattr(cfg, "signal_gap_sec", cfg.horizon_sec))
    for row in confirmed:
        veto_reason = confirmation_veto_reason(row, getattr(cfg, "confirmation_veto", "none"))
        if veto_reason is not None:
            skipped_veto += 1
            continue
        idx = int(row["idx"])
        if idx - last_entry_idx < signal_gap_sec:
            skipped_gap += 1
            continue
        last_entry_idx = idx
        idx = int(row["idx"])
        settle_idx = int(row["settle_idx"])
        signal = str(row["signal"])
        entry = float(row["entry"])
        settle = float(row["settle"])
        rows.append(
            {
                "strategy_id": cfg.strategy_id,
                "model_type": "normal_state_v9",
                "idx": idx,
                "settle_idx": settle_idx,
                "time": pd.Timestamp(row["time"]),
                "signal": signal,
                "entry": entry,
                "settle_time": bars.index[settle_idx],
                "settle": settle,
                "won": bool(settle > entry if signal == "UP" else settle < entry),
                "horizon_sec": int(cfg.horizon_sec),
                "amount": float(cfg.amount),
                "signal_time": pd.Timestamp(row["signal_time"]),
                "signal_entry": float(row["signal_entry"]),
                "confirm_delay_sec": int(row["confirm_delay_sec"]),
                "confirm_adverse_bps": float(row["confirm_adverse_bps"]),
                "confirm_max_adverse_bps": float(row["confirm_max_adverse_bps"]),
                "source_rule": row.get("source_rule"),
                "state_gate": row.get("state_gate"),
                "state_gate_reason": row.get("state_gate_reason"),
                "rule_filter_detail": row.get("rule_filter_detail"),
                "consensus_votes": row.get("consensus_votes"),
                "consensus_vote_names": row.get("consensus_vote_names"),
                "outside_sec": row.get("outside_sec"),
                "peak_abs_z": row.get("peak_abs_z"),
                "z": row.get("z"),
                "sigma10_bps": row.get("sigma10_bps"),
                "flow60": row.get("flow60"),
                "m_cover2_120": row.get("m_cover2_120"),
                "m_width_ratio": row.get("m_width_ratio"),
                "m_slope60_bps": row.get("m_slope60_bps"),
                "m_bandwalk10": row.get("m_bandwalk10"),
                "m_half_life_min": row.get("m_half_life_min"),
                "ob_available": bool(row.get("ob_available")),
                "ob_imb20": row.get("ob_imb20"),
                "ob_micro_bps": row.get("ob_micro_bps"),
                "generation_skipped_rule": skipped["rule"],
                "generation_skipped_state_gate": skipped["state_gate"],
                "generation_skipped_confirmation_veto": skipped_veto,
                "generation_confirm_gap_skipped": skipped_gap,
                "generation_confirm_rejected_adverse": confirm_meta["rejected"]["adverse_confirmation"],
                "generation_confirm_cooldown_skipped": confirm_meta["cooldown_skipped"],
                "confirmation_veto": getattr(cfg, "confirmation_veto", "none"),
            }
        )
    return rows
