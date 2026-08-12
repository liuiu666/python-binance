from __future__ import annotations

import numpy as np
import pandas as pd


def action_values(p_up: np.ndarray, payout_rate: float) -> tuple[np.ndarray, np.ndarray]:
    p = np.asarray(p_up, dtype=float)
    return p * payout_rate - (1.0 - p), (1.0 - p) * payout_rate - p


def plan_actions(
    p_up: np.ndarray,
    *,
    payout_rate: float,
    min_ev: float,
    transition_confidence: np.ndarray | None = None,
    state_support: np.ndarray | None = None,
    uncertainty_penalty: float = 0.0,
    sparse_state_penalty: float = 0.0,
) -> pd.DataFrame:
    """Choose UP, DOWN, or SKIP from conservative action values.

    Transition confidence and state support couple the action decision to the
    learned latent dynamics. They may only reduce an edge, never create one.
    """
    p = np.asarray(p_up, dtype=float)
    up_ev, down_ev = action_values(p, payout_rate)
    confidence = np.ones_like(p) if transition_confidence is None else np.asarray(transition_confidence, dtype=float)
    support = np.ones_like(p) if state_support is None else np.asarray(state_support, dtype=float)
    penalty = uncertainty_penalty * (1.0 - np.clip(confidence, 0.0, 1.0))
    penalty += sparse_state_penalty * (1.0 - np.clip(support, 0.0, 1.0))
    up_score = up_ev - penalty
    down_score = down_ev - penalty
    best = np.maximum(up_score, down_score)
    signal = np.where(best < min_ev, "SKIP", np.where(up_score >= down_score, "UP", "DOWN"))
    return pd.DataFrame({
        "p_up": p,
        "confidence": np.maximum(p, 1.0 - p),
        "ev_up": up_ev,
        "ev_down": down_ev,
        "conservative_ev": best,
        "signal": signal,
    })
