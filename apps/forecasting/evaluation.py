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

def baseline_hourly_series(obs: pd.DataFrame, lookback_hours: int = 24 * 14) -> pd.DataFrame:
    """Pre-surge normal hourly demand per SKU: trailing mean over recent history.

    Mean, not median: the baseline compares the future window's mean hourly rate
    against this, and a median over a spiky day/night series sits far below its
    mean, marking almost every window as a surge.
    """
    df = obs.sort_values(["sku_id", "timestamp"]).reset_index(drop=True).copy()
    df["fulfillment_demand"] = (
        df["orders_created"] - df["orders_cancelled_pre_ship"]
    ).astype(float)
    df["baseline_hourly"] = df.groupby("sku_id", sort=False)["fulfillment_demand"].transform(
        lambda x: x.rolling(lookback_hours, min_periods=24).mean()
    )
    return df[["timestamp", "sku_id", "baseline_hourly"]]


def baseline_cumulative_reference(meta: pd.DataFrame, base_series: pd.DataFrame,
                                  horizon: int) -> np.ndarray:
    """Expected baseline cumulative demand over ``horizon`` hours for each row in ``meta``."""
    merged = meta.merge(base_series, on=["timestamp", "sku_id"], how="left")
    return merged["baseline_hourly"].to_numpy(dtype=float) * horizon


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


def surge_detection_metrics(actual, predicted, is_surge, alarm_ratio: float = 1.2,
                            reference=None) -> dict:
    """How often a surge is called, and how often it is called wrongly.

    An 'alarm' is the model predicting at least ``alarm_ratio`` times the
    reference level (default: per-row reference array or actual's median).
    """
    a, p = np.asarray(actual, dtype=float), np.asarray(predicted, dtype=float)
    surge = np.asarray(is_surge, dtype=bool)
    mask = np.isfinite(a) & np.isfinite(p)
    a, p, surge = a[mask], p[mask], surge[mask]
    if not a.size:
        return {
            "recall": float("nan"), "false_alarm_rate": float("nan"),
            "precision": float("nan"), "f1": float("nan"),
            "tp": 0, "fp": 0, "fn": 0, "tn": 0, "alarm_count": 0,
        }

    if reference is None:
        ref = float(np.median(a))
    elif np.ndim(reference) > 0:
        ref = np.asarray(reference, dtype=float)
        if len(ref) == len(mask):
            ref = ref[mask]
    else:
        ref = float(reference)

    alarm = p >= alarm_ratio * ref

    tp = int(np.sum(alarm & surge))
    fp = int(np.sum(alarm & ~surge))
    fn = int(np.sum(~alarm & surge))
    tn = int(np.sum(~alarm & ~surge))
    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    far = fp / (fp + tn) if (fp + tn) else float("nan")
    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    f1 = (
        (2 * precision * recall / (precision + recall))
        if (np.isfinite(precision) and np.isfinite(recall) and (precision + recall) > 0)
        else float("nan")
    )
    return {
        "recall": recall,
        "false_alarm_rate": far,
        "precision": precision,
        "f1": f1,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "alarm_count": int(np.sum(alarm)),
    }


def surge_detection_lead_time(
    truth: pd.DataFrame,
    meta: pd.DataFrame,
    predicted,
    reference,
    alarm_ratio: float = 1.2,
    surge_multiplier_threshold: float = 1.5,
    max_anticipation_hours: int = 48,
    max_grace_hours: int = 12,
) -> dict:
    """Surge detection lead time (PRD 16.1).

    Measures hours between the first alarm and the episode's true onset in
    ``latent_truth.csv``. Positive values mean the model warned in advance
    before demand surged.
    """
    p = np.asarray(predicted, dtype=float)
    ref = np.asarray(reference, dtype=float)
    alarm = p >= alarm_ratio * ref

    df = meta[["timestamp", "sku_id"]].copy()
    df["alarm"] = alarm

    min_ts = df["timestamp"].min()
    max_ts = df["timestamp"].max()

    # Truth episodes that reach surge threshold
    surges = truth[
        (truth["demand_multiplier"] >= surge_multiplier_threshold)
        & truth["episode_id"].notna()
        & (truth["episode_id"] != "")
    ].copy()

    episode_records = []
    for ep_id, g in surges.groupby("episode_id"):
        first_onset = g["timestamp"].min()
        if not (min_ts <= first_onset <= max_ts):
            continue
        ep_type = g["episode_type"].iloc[0]
        sku = g["sku_id"].iloc[0]

        window = df[
            (df["sku_id"] == sku)
            & (df["timestamp"] >= first_onset - pd.Timedelta(hours=max_anticipation_hours))
            & (df["timestamp"] <= first_onset + pd.Timedelta(hours=max_grace_hours))
        ]
        ep_alarms = window[window["alarm"]]
        if not ep_alarms.empty:
            first_alarm = ep_alarms["timestamp"].min()
            lt = (first_onset - first_alarm).total_seconds() / 3600.0
            episode_records.append({
                "episode_id": ep_id,
                "episode_type": ep_type,
                "sku_id": sku,
                "true_onset": first_onset,
                "first_alarm": first_alarm,
                "lead_time_hours": float(lt),
                "detected": True,
            })
        else:
            episode_records.append({
                "episode_id": ep_id,
                "episode_type": ep_type,
                "sku_id": sku,
                "true_onset": first_onset,
                "first_alarm": None,
                "lead_time_hours": None,
                "detected": False,
            })

    detected_lts = [r["lead_time_hours"] for r in episode_records if r["detected"]]
    return {
        "total_episodes": len(episode_records),
        "detected_episodes": len(detected_lts),
        "detection_rate": len(detected_lts) / len(episode_records) if episode_records else float("nan"),
        "median_lead_time_hours": float(np.median(detected_lts)) if detected_lts else float("nan"),
        "mean_lead_time_hours": float(np.mean(detected_lts)) if detected_lts else float("nan"),
        "episodes": episode_records,
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
