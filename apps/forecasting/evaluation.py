"""Forecast evaluation metrics (PRD 16.1).

Every policy in the PRD 16.3 experiment matrix is scored with these same
functions on the same rows, so the seasonal-naive baseline, the
transaction-only model and the transaction-plus-content model are directly
comparable.

Surge metrics are scored per episode type, because the interesting question
is not "is the model accurate on average" but "does it catch a real surge
early without chasing a FALSE_CONTENT spike".
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# --------------------------------------------------------------- point ----

def mae(actual, predicted) -> float:
    a, p = _clean(actual, predicted)
    return float(np.mean(np.abs(a - p))) if a.size else float("nan")


def rmse(actual, predicted) -> float:
    a, p = _clean(actual, predicted)
    return float(np.sqrt(np.mean((a - p) ** 2))) if a.size else float("nan")


def wape(actual, predicted) -> float:
    """Weighted absolute percentage error. Undefined when the denominator is 0."""
    a, p = _clean(actual, predicted)
    denom = np.sum(np.abs(a))
    if denom <= 1e-9:
        return float("nan")
    return float(np.sum(np.abs(a - p)) / denom)


def bias(actual, predicted) -> float:
    """Mean signed error; positive means the model under-forecasts."""
    a, p = _clean(actual, predicted)
    return float(np.mean(a - p)) if a.size else float("nan")


# ------------------------------------------------------------ quantile ----

def quantile_loss(actual, predicted, q: float) -> float:
    """Pinball loss for one quantile."""
    a, p = _clean(actual, predicted)
    if not a.size:
        return float("nan")
    diff = a - p
    return float(np.mean(np.maximum(q * diff, (q - 1) * diff)))


def interval_coverage(actual, lower, upper) -> float:
    """Share of actuals falling inside [lower, upper]. Target for P10-P90 is 0.80."""
    a = np.asarray(actual, dtype=float)
    lo = np.asarray(lower, dtype=float)
    hi = np.asarray(upper, dtype=float)
    mask = np.isfinite(a) & np.isfinite(lo) & np.isfinite(hi)
    if not mask.any():
        return float("nan")
    return float(np.mean((a[mask] >= lo[mask]) & (a[mask] <= hi[mask])))


def interval_width(lower, upper) -> float:
    lo, hi = _clean(lower, upper)
    return float(np.mean(hi - lo)) if lo.size else float("nan")


def evaluate_quantiles(actual, p10, p50, p90) -> dict:
    """The standard block of numbers reported for every model and horizon."""
    return {
        "n": int(np.sum(np.isfinite(np.asarray(actual, dtype=float)))),
        "mae": mae(actual, p50),
        "rmse": rmse(actual, p50),
        "wape": wape(actual, p50),
        "bias": bias(actual, p50),
        "qloss_p10": quantile_loss(actual, p10, 0.10),
        "qloss_p50": quantile_loss(actual, p50, 0.50),
        "qloss_p90": quantile_loss(actual, p90, 0.90),
        "coverage_p10_p90": interval_coverage(actual, p10, p90),
        "interval_width": interval_width(p10, p90),
    }


# --------------------------------------------------------------- surge ----

def label_surge_rows(meta: pd.DataFrame, truth: pd.DataFrame,
                     threshold: float = 1.5) -> pd.Series:
    """True where the evaluator's latent process was genuinely above baseline.

    Uses ``latent_truth.csv`` -- evaluator only, never a feature.
    """
    merged = meta.merge(
        truth[["timestamp", "sku_id", "demand_multiplier", "episode_type"]],
        on=["timestamp", "sku_id"], how="left",
    )
    return (merged["demand_multiplier"].fillna(1.0) >= threshold).to_numpy()


def surge_detection_metrics(actual, predicted, is_surge, alarm_ratio: float = 1.3,
                            reference=None) -> dict:
    """How often a surge is called, and how often it is called wrongly.

    An 'alarm' is the model predicting at least ``alarm_ratio`` times the
    reference level (default: the actual's own median, i.e. a normal window).
    """
    a, p = np.asarray(actual, dtype=float), np.asarray(predicted, dtype=float)
    surge = np.asarray(is_surge, dtype=bool)
    mask = np.isfinite(a) & np.isfinite(p)
    a, p, surge = a[mask], p[mask], surge[mask]
    if not a.size:
        return {"recall": float("nan"), "false_alarm_rate": float("nan"), "precision": float("nan")}

    ref = float(np.median(a)) if reference is None else float(reference)
    alarm = p >= alarm_ratio * ref

    tp = int(np.sum(alarm & surge))
    fp = int(np.sum(alarm & ~surge))
    fn = int(np.sum(~alarm & surge))
    tn = int(np.sum(~alarm & ~surge))
    return {
        "recall": tp / (tp + fn) if (tp + fn) else float("nan"),
        "false_alarm_rate": fp / (fp + tn) if (fp + tn) else float("nan"),
        "precision": tp / (tp + fp) if (tp + fp) else float("nan"),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
    }


def metrics_by_episode_type(meta: pd.DataFrame, truth: pd.DataFrame,
                            actual, predicted) -> pd.DataFrame:
    """Per-episode-type error, so FALSE_CONTENT failures cannot hide in the mean."""
    merged = meta.reset_index(drop=True).merge(
        truth[["timestamp", "sku_id", "episode_type"]],
        on=["timestamp", "sku_id"], how="left",
    )
    merged["episode_type"] = merged["episode_type"].fillna("").replace("", "NORMAL")
    merged["actual"] = np.asarray(actual, dtype=float)
    merged["predicted"] = np.asarray(predicted, dtype=float)

    rows = []
    for etype, g in merged.groupby("episode_type"):
        rows.append({
            "episode_type": etype,
            "n": len(g),
            "mae": mae(g["actual"], g["predicted"]),
            "wape": wape(g["actual"], g["predicted"]),
            "bias": bias(g["actual"], g["predicted"]),
        })
    return pd.DataFrame(rows).sort_values("n", ascending=False).reset_index(drop=True)


def _clean(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    mask = np.isfinite(a) & np.isfinite(b)
    return a[mask], b[mask]
