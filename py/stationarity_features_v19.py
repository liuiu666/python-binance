"""Causal rolling stationarity and trend features for V19 research."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


ESTIMATION_WINDOW_MIN = 240


def _rolling_adf_t_stat(log_close: pd.Series, window: int) -> pd.Series:
    """ADF-style t-stat for beta in Δy = a + beta*y[-1] + gamma*Δy[-1]."""
    delta = log_close.diff()
    # Constant shifts and positive scaling do not change beta's t-stat.  These
    # scales materially improve conditioning of the batched 3x3 solves.
    level = (log_close - float(log_close.iloc[0])) * 100.0
    x1 = level.shift(1)
    x2 = delta.shift(1) * 10_000.0
    y = delta * 10_000.0
    valid_row = x1.notna() & x2.notna() & y.notna()
    one = valid_row.astype(float)
    x1v = x1.where(valid_row, 0.0)
    x2v = x2.where(valid_row, 0.0)
    yv = y.where(valid_row, 0.0)

    def rolling_sum(series: pd.Series) -> np.ndarray:
        return series.rolling(window, min_periods=window).sum().to_numpy(float)

    count = rolling_sum(one)
    s1 = rolling_sum(x1v)
    s2 = rolling_sum(x2v)
    sy = rolling_sum(yv)
    s11 = rolling_sum(x1v * x1v)
    s22 = rolling_sum(x2v * x2v)
    s12 = rolling_sum(x1v * x2v)
    s1y = rolling_sum(x1v * yv)
    s2y = rolling_sum(x2v * yv)
    syy = rolling_sum(yv * yv)

    n = len(log_close)
    xtx = np.full((n, 3, 3), np.nan, dtype=float)
    xty = np.full((n, 3), np.nan, dtype=float)
    xtx[:, 0, 0] = count
    xtx[:, 0, 1] = xtx[:, 1, 0] = s1
    xtx[:, 0, 2] = xtx[:, 2, 0] = s2
    xtx[:, 1, 1] = s11
    xtx[:, 1, 2] = xtx[:, 2, 1] = s12
    xtx[:, 2, 2] = s22
    xty[:, 0] = sy
    xty[:, 1] = s1y
    xty[:, 2] = s2y

    ready = np.isfinite(xtx).all(axis=(1, 2)) & np.isfinite(xty).all(axis=1)
    ready &= count >= window
    positions = np.flatnonzero(ready)
    output = np.full(n, np.nan, dtype=float)
    if not len(positions):
        return pd.Series(output, index=log_close.index, name="adf_t_beta")

    matrices = xtx[positions]
    vectors = xty[positions]
    determinants = np.linalg.det(matrices)
    nonsingular = np.isfinite(determinants) & (np.abs(determinants) > 1e-10)
    positions = positions[nonsingular]
    matrices = matrices[nonsingular]
    vectors = vectors[nonsingular]
    if not len(positions):
        return pd.Series(output, index=log_close.index, name="adf_t_beta")

    coefficients = np.linalg.solve(matrices, vectors[..., np.newaxis]).squeeze(-1)
    inverses = np.linalg.inv(matrices)
    residual_ss = syy[positions] - np.einsum("ij,ij->i", coefficients, vectors)
    residual_ss = np.maximum(residual_ss, 0.0)
    sigma2 = residual_ss / max(window - 3, 1)
    beta_variance = sigma2 * inverses[:, 1, 1]
    usable = np.isfinite(beta_variance) & (beta_variance > 0.0)
    output_positions = positions[usable]
    output[output_positions] = (
        coefficients[usable, 1] / np.sqrt(beta_variance[usable])
    )
    return pd.Series(output, index=log_close.index, name="adf_t_beta")


def _rolling_ar1_half_life(log_close: pd.Series, window: int) -> tuple[pd.Series, pd.Series]:
    lag = log_close.shift(1)
    current = log_close
    pairs = max(window - 1, 2)
    sx = lag.rolling(pairs, min_periods=pairs).sum()
    sy = current.rolling(pairs, min_periods=pairs).sum()
    sxx = lag.mul(lag).rolling(pairs, min_periods=pairs).sum()
    sxy = lag.mul(current).rolling(pairs, min_periods=pairs).sum()
    denominator = sxx - sx.mul(sx) / pairs
    phi = (sxy - sx.mul(sy) / pairs) / denominator.replace(0.0, np.nan)
    half_life = pd.Series(np.nan, index=log_close.index, dtype=float)
    valid = phi.gt(0.0) & phi.lt(1.0)
    half_life.loc[valid] = -math.log(2.0) / np.log(phi.loc[valid])
    return phi.rename("ar1_phi"), half_life.rename("half_life_min")


def build_stationarity_features(
    minutes: pd.DataFrame,
    *,
    estimation_window_min: int = ESTIMATION_WINDOW_MIN,
    shock_threshold: float = 1.60,
) -> pd.DataFrame:
    close = minutes["close"].astype(float)
    log_close = np.log(close)
    ret1 = log_close.diff()

    path60 = ret1.abs().rolling(60, min_periods=60).sum()
    efficiency60 = (log_close - log_close.shift(60)).abs() / path60.replace(
        0.0, np.nan
    )
    sigma120 = ret1.rolling(120, min_periods=120).std(ddof=0)
    momentum60 = (log_close - log_close.shift(60)) / (
        sigma120 * math.sqrt(60)
    ).replace(0.0, np.nan)

    ret10 = log_close - log_close.shift(10)
    variance_ratio10 = ret10.rolling(
        estimation_window_min, min_periods=estimation_window_min
    ).var(ddof=0) / (
        10.0
        * ret1.rolling(
            estimation_window_min, min_periods=estimation_window_min
        ).var(ddof=0)
    ).replace(0.0, np.nan)

    phi, half_life = _rolling_ar1_half_life(log_close, estimation_window_min)
    adf_t = _rolling_adf_t_stat(log_close, estimation_window_min)

    sigma15 = ret1.rolling(15, min_periods=15).std(ddof=0)
    shock_ratio = sigma15 / sigma120.replace(0.0, np.nan)
    shock_max10 = shock_ratio.rolling(10, min_periods=10).max()
    shock = shock_max10.ge(shock_threshold)

    weak_trend = efficiency60.le(0.30) & momentum60.abs().le(1.50)
    strong_trend = efficiency60.ge(0.40) & momentum60.abs().ge(1.75)
    mean_reversion_stats = (
        half_life.between(3.0, 30.0)
        & adf_t.le(-2.5)
        & variance_ratio10.le(0.90)
    )
    revertible = weak_trend & mean_reversion_stats & ~shock
    trending = strong_trend & (
        variance_ratio10.ge(1.05) | ~mean_reversion_stats
    ) & ~shock

    structure_state = pd.Series("mixed", index=minutes.index, dtype="object")
    ready = pd.concat(
        [
            efficiency60,
            momentum60,
            variance_ratio10,
            half_life,
            adf_t,
            shock_max10,
        ],
        axis=1,
    ).notna().all(axis=1)
    structure_state.loc[~ready] = "unknown"
    structure_state.loc[ready & shock] = "shock"
    structure_state.loc[ready & revertible] = "revertible"
    structure_state.loc[ready & trending] = "trend"

    return pd.DataFrame(
        {
            "efficiency60": efficiency60,
            "momentum60_score": momentum60,
            "variance_ratio10": variance_ratio10,
            "ar1_phi": phi,
            "half_life_min": half_life,
            "adf_t_beta": adf_t,
            "shock_ratio": shock_ratio,
            "shock_max10": shock_max10,
            "structure_state": structure_state,
        },
        index=minutes.index,
    )
