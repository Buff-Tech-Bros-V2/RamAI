"""Seasonal naive baseline (PRD FR-F01, 10.1).

This is the "what would they have done anyway" reference the enriched model
has to beat. It is deliberately dumb:

    seasonal naive: cumulative demand over the next H hours == the demand
                    observed over the same H hours one week earlier

A moving-average variant is provided as a second reference point (PRD 10.1
allows it as an additional baseline).

Both produce a P50 only; intervals come from the residual spread on the
training split, so the baseline can still be scored on quantile loss and
interval coverage alongside the quantile models.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

SEASONAL_PERIOD_HOURS = 24 * 7

MODEL_SEASONAL_NAIVE = "seasonal_naive"
MODEL_MOVING_AVERAGE = "moving_average"
BASELINE_VERSION = "baseline-v1"


def _fulfillment(obs: pd.DataFrame) -> pd.DataFrame:
    df = obs.sort_values(["sku_id", "timestamp"]).reset_index(drop=True).copy()
    df["fulfillment_demand"] = (
        df["orders_created"] - df["orders_cancelled_pre_ship"]
    ).astype(float)
    return df


def seasonal_naive_forecast(obs: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Cumulative demand over the H hours ending one week before the cutoff.

    At cutoff ``t`` the window [t-168-H+1, t-168] is fully observed, so this
    uses nothing from the future.
    """
    df = _fulfillment(obs)
    parts = []
    for sku_id, g in df.groupby("sku_id", sort=False):
        g = g.sort_values("timestamp").reset_index(drop=True)
        rolled = g["fulfillment_demand"].rolling(horizon, min_periods=horizon).sum()
        parts.append(pd.DataFrame({
            "timestamp": g["timestamp"],
            "sku_id": sku_id,
            "prediction": rolled.shift(SEASONAL_PERIOD_HOURS).to_numpy(),
        }))
    return pd.concat(parts, ignore_index=True)


def moving_average_forecast(obs: pd.DataFrame, horizon: int,
                            window_hours: int = 72) -> pd.DataFrame:
    """Recent hourly rate carried forward across the horizon."""
    df = _fulfillment(obs)
    parts = []
    for sku_id, g in df.groupby("sku_id", sort=False):
        g = g.sort_values("timestamp").reset_index(drop=True)
        rate = g["fulfillment_demand"].rolling(window_hours, min_periods=1).mean()
        parts.append(pd.DataFrame({
            "timestamp": g["timestamp"],
            "sku_id": sku_id,
            "prediction": (rate * horizon).to_numpy(),
        }))
    return pd.concat(parts, ignore_index=True)


def baseline_quantiles(predictions: pd.Series, residual_ratios: np.ndarray,
                       quantiles=(0.1, 0.5, 0.9)) -> pd.DataFrame:
    """Turn point predictions into quantiles using training residual ratios.

    Uses multiplicative residuals (actual / predicted) so the interval widens
    with the level of demand rather than staying a constant number of units.
    """
    out = {}
    finite = residual_ratios[np.isfinite(residual_ratios)]
    for q in quantiles:
        factor = float(np.quantile(finite, q)) if finite.size else 1.0
        out[f"p{int(q * 100)}"] = (predictions * factor).clip(lower=0.0)
    return pd.DataFrame(out, index=predictions.index)


def fit_residual_ratios(actual: pd.Series, predicted: pd.Series) -> np.ndarray:
    """Multiplicative residuals on rows where the prediction is usable."""
    a = actual.to_numpy(dtype=float)
    p = predicted.to_numpy(dtype=float)
    mask = np.isfinite(a) & np.isfinite(p) & (p > 1e-6)
    return a[mask] / p[mask]


def build_baseline_predictions(obs: pd.DataFrame, labels: pd.DataFrame, horizon: int,
                               model: str = MODEL_SEASONAL_NAIVE,
                               drop_censored: bool = True) -> pd.DataFrame:
    """Predictions joined to the same labels the quantile models are scored on."""
    if model == MODEL_SEASONAL_NAIVE:
        preds = seasonal_naive_forecast(obs, horizon)
    elif model == MODEL_MOVING_AVERAGE:
        preds = moving_average_forecast(obs, horizon)
    else:
        raise ValueError(f"unknown baseline model {model!r}")

    target_col = f"target_demand_{horizon}h"
    censored_col = f"target_censored_{horizon}h"
    cols = ["timestamp", "sku_id", target_col, censored_col]
    if "split" in labels.columns:
        cols.append("split")

    merged = preds.merge(labels[cols], on=["timestamp", "sku_id"], how="inner")
    keep = merged[target_col].notna() & merged["prediction"].notna()
    if drop_censored:
        keep &= merged[censored_col].fillna(1).astype(int) == 0
    merged = merged[keep].reset_index(drop=True)
    return merged.rename(columns={target_col: "actual"})
