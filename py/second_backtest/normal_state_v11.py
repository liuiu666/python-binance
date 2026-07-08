from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .normal_state_v9 import generate_normal_state_v9_signals


@dataclass(frozen=True)
class NormalStateV11Config:
    strategy_id: str = "BTC_10min_NORMAL_STATE_V11_BANDWALK_2OF5_D5A5"
    rule_name: str = "V6_CONSENSUS_2OF5_UPPER"
    state_gate: str = "edge_persistence_lt6"
    lookback_sec: int = 180 * 60
    horizon_sec: int = 600
    signal_gap_sec: int = 600
    confirm_delay_sec: int = 5
    max_adverse_bps: float = 5.0
    confirmation_veto: str = "none"
    amount: float = 5.0
    label: str = "normal_state_v11_bandwalk_2of5"


def generate_normal_state_v11_signals(
    bars: pd.DataFrame,
    cfg: NormalStateV11Config = NormalStateV11Config(),
    *,
    features: pd.DataFrame | None = None,
) -> list[dict]:
    return generate_normal_state_v9_signals(bars, cfg, features=features)
