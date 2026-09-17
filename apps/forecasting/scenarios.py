"""Demand scenario generator and surge persistence (PRD FR-F05, 10.3, 12).

Per-hour quantiles are NOT a trajectory: sampling each hour independently from
its own quantile would destroy the temporal dependence that makes a surge a
surge. We use **block bootstrap on model residuals** instead, so a scenario
that starts high tends to stay high the way a real episode does.

    scenarios = generate_scenarios(hourly_p50, residual_blocks, n_scenarios=400)
    persistence = surge_persistence(scenarios, baseline_level, horizon=48)

Surge persistence answers the question the decision engine actually asks:
"will demand still be above baseline when my extra stock lands?" -- not
"how many units in total".
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

SCENARIO_VERSION = "scenarios-v1"
DEFAULT_N_SCENARIOS = 400
DEFAULT_BLOCK_HOURS = 6


@dataclass
class ScenarioSet:
    """``trajectories`` is (n_scenarios, horizon_hours) of hourly demand."""

    trajectories: np.ndarray
    horizon_hours: int
    n_scenarios: int
    seed: int
    block_hours: int = DEFAULT_BLOCK_HOURS
    version: str = SCENARIO_VERSION

    @property
    def cumulative(self) -> np.ndarray:
        """Total demand per scenario over the whole horizon."""
        return self.trajectories.sum(axis=1)

    def quantiles(self, qs=(0.1, 0.5, 0.9)) -> dict:
        cum = self.cumulative
        return {f"p{int(q * 100)}": float(np.quantile(cum, q)) for q in qs}

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            self.trajectories,
            columns=[f"h{i + 1}" for i in range(self.horizon_hours)],
        )


# ------------------------------------------------------------- residuals --

def residual_blocks(actual: np.ndarray, predicted: np.ndarray,
                    block_hours: int = DEFAULT_BLOCK_HOURS) -> np.ndarray:
    """Split multiplicative residuals into contiguous blocks of ``block_hours``.

    Multiplicative (actual / predicted) rather than additive, so the spread
    scales with the level of demand instead of being a fixed unit count.
    """
    a = np.asarray(actual, dtype=float)
    p = np.asarray(predicted, dtype=float)
    mask = np.isfinite(a) & np.isfinite(p) & (p > 1e-6)
    ratios = np.clip(a[mask] / p[mask], 0.0, 5.0)

    n_blocks = len(ratios) // block_hours
    if n_blocks == 0:
        return np.ones((1, block_hours))
    return ratios[: n_blocks * block_hours].reshape(n_blocks, block_hours)


# -------------------------------------------------------------- generate --

def generate_scenarios(hourly_median: np.ndarray, blocks: np.ndarray,
                       n_scenarios: int = DEFAULT_N_SCENARIOS,
                       seed: int = 42) -> ScenarioSet:
    """Build temporally coherent hourly demand trajectories.

    ``hourly_median`` is the expected hourly demand path over the horizon; each
    scenario multiplies it by a residual path stitched from sampled blocks.
    """
    median = np.asarray(hourly_median, dtype=float)
    horizon = len(median)
    block_hours = blocks.shape[1]
    rng = np.random.default_rng(seed)

    n_needed = int(np.ceil(horizon / block_hours))
    idx = rng.integers(0, len(blocks), size=(n_scenarios, n_needed))
    multipliers = blocks[idx].reshape(n_scenarios, -1)[:, :horizon]

    trajectories = np.maximum(median[None, :] * multipliers, 0.0)
    return ScenarioSet(
        trajectories=trajectories,
        horizon_hours=horizon,
        n_scenarios=n_scenarios,
        seed=seed,
        block_hours=block_hours,
    )


def hourly_path_from_cumulative(cumulative_p50: float, horizon_hours: int,
                                hour_profile: np.ndarray | None = None,
                                start_hour: int = 0) -> np.ndarray:
    """Spread a cumulative P50 across the horizon using an hour-of-day shape.

    The quantile models predict a cumulative total; scenarios need an hourly
    path. A flat split would understate the day/night swing that decides when a
    stockout actually bites.
    """
    if hour_profile is None:
        return np.full(horizon_hours, cumulative_p50 / horizon_hours)

    hours = (np.arange(horizon_hours) + start_hour) % len(hour_profile)
    shape = hour_profile[hours]
    return cumulative_p50 * shape / shape.sum()


# ------------------------------------------------------------ persistence --

def surge_persistence(scenarios: ScenarioSet, baseline_hourly: float,
                      window_hours: int = 48, threshold: float = 1.2) -> float:
    """P(demand still above baseline over the window) -- FR-F05.

    A scenario "persists" when its mean hourly demand across the window is at
    least ``threshold`` times the baseline hourly level.
    """
    if baseline_hourly <= 1e-9:
        return float("nan")
    window = scenarios.trajectories[:, :window_hours]
    mean_rate = window.mean(axis=1)
    return float(np.mean(mean_rate >= threshold * baseline_hourly))


def persistence_curve(scenarios: ScenarioSet, baseline_hourly: float,
                      windows=(24, 48, 72), threshold: float = 1.2) -> dict:
    """Persistence probability at several horizons, for the decision engine."""
    return {
        f"surge_persistence_{w}h": surge_persistence(
            scenarios, baseline_hourly, window_hours=min(w, scenarios.horizon_hours),
            threshold=threshold,
        )
        for w in windows
    }


def baseline_hourly_level(obs: pd.DataFrame, sku_id: str, cutoff,
                          lookback_days: int = 14) -> float:
    """Pre-surge normal hourly demand: the MEAN hour over recent history.

    Mean, not median. Scenarios are compared against this as a mean hourly
    rate, and on a spiky day/night series the median sits far below the mean --
    using it made every scenario count as persisting and pinned the
    probability at 1.0.
    """
    history = obs[(obs["sku_id"] == sku_id) & (obs["timestamp"] <= cutoff)]
    history = history.sort_values("timestamp").tail(24 * lookback_days)
    if history.empty:
        return float("nan")
    demand = (history["orders_created"] - history["orders_cancelled_pre_ship"]).astype(float)
    return float(demand.mean())
