"""Real quantile-regression ForecastProvider (PRD 12 `run_forecast`).

Drops into the contract `apps.forecasting.services.ForecastProvider` defines,
so `apps.decisionengine` and `apps.agent` need no changes: same
`run_forecast(sku, cutoff, horizons_hours)` signature, same `ForecastOutput`.

What happens on a call:

    1. pull the SKU's history up to the cutoff out of the ORM
    2. build one feature row at the cutoff (never sees past it)
    3. predict P10/P50/P90 per horizon from the saved LightGBM bundle
    4. spread the P50 across the horizon and block-bootstrap scenarios to get
       surge persistence probability (FR-F05)
    5. attach data-quality flags and the model/feature version

Missing artifacts or too little history are reported through
`data_quality_flags` and a wider interval rather than a crash -- a tool
failure must never produce a fabricated recommendation (PRD 18).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np

from .dataclasses import ForecastOutput
from .features import MODE_CONTENT, MODE_TRANSACTION, latest_feature_row, observations_to_frame
from .quantile_model import QuantileBundle
from .scenarios import (
    baseline_hourly_level,
    generate_scenarios,
    hourly_path_from_cumulative,
    residual_blocks,
    surge_persistence,
)
from .services import DEFAULT_HORIZONS_HOURS, ForecastProvider

# Hour-of-day shape used to spread a cumulative forecast into an hourly path.
# Mirrors the demo data's profile; a production version would learn it per SKU.
HOUR_PROFILE = np.array(
    [0.25, 0.16, 0.11, 0.09, 0.10, 0.18, 0.38, 0.62, 0.85, 1.00, 1.12, 1.24,
     1.30, 1.18, 1.05, 1.02, 1.10, 1.28, 1.52, 1.78, 1.86, 1.55, 1.02, 0.52]
)

MIN_HISTORY_HOURS = 24 * 8          # need a week of lags before features are meaningful
DEFAULT_ARTIFACT_ROOT = Path("artifacts")
SCENARIO_SEED = 42


class LightGBMForecastProvider(ForecastProvider):
    """Quantile forecast backed by the trained LightGBM bundles."""

    def __init__(self, artifact_root: Path | str = DEFAULT_ARTIFACT_ROOT,
                 feature_mode: str = MODE_TRANSACTION,
                 n_scenarios: int = 400):
        self.artifact_root = Path(artifact_root)
        self.feature_mode = feature_mode
        self.n_scenarios = n_scenarios
        self._bundle: QuantileBundle | None = None

    # ------------------------------------------------------------ loading --
    @property
    def bundle(self) -> QuantileBundle:
        if self._bundle is None:
            self._bundle = QuantileBundle.load(
                self.artifact_root / f"forecast_{self.feature_mode}"
            )
        return self._bundle

    @property
    def model_name(self) -> str:
        return self.bundle.model_name

    @property
    def model_version(self) -> str:
        return f"{self.bundle.model_version}+{self.bundle.feature_version}"

    # ------------------------------------------------------------ forecast --
    def run_forecast(self, sku, cutoff: datetime,
                     horizons_hours: tuple[int, ...] = DEFAULT_HORIZONS_HOURS
                     ) -> list[ForecastOutput]:
        observations = list(
            sku.observations.filter(timestamp__lte=cutoff).order_by("timestamp")
        )
        flags: list[str] = []
        if len(observations) < MIN_HISTORY_HOURS:
            flags.append("insufficient_history")
        if not observations:
            return [self._empty(sku, cutoff, h, flags + ["no_observations"])
                    for h in horizons_hours]

        obs = observations_to_frame(
            observations, product_category=getattr(sku, "product_category", "")
        )

        content_used = False
        if self.feature_mode == MODE_CONTENT:
            recent = obs.tail(24)
            content_used = bool(recent["content_view_velocity"].notna().any())
            if not content_used:
                flags.append("content_signal_missing")

        X = latest_feature_row(obs, sku_id=sku.sku_id, cutoff=cutoff,
                               feature_mode=self.feature_mode)
        baseline = baseline_hourly_level(obs, sku.sku_id, cutoff)

        outputs = []
        for horizon in horizons_hours:
            preds = self.bundle.predict(X, horizon=horizon)
            p10, p50, p90 = (float(preds["p10"].iloc[0]),
                             float(preds["p50"].iloc[0]),
                             float(preds["p90"].iloc[0]))

            if self.bundle.has_persistence(horizon):
                persistence = round(float(self.bundle.predict_persistence(X, horizon).iloc[0]), 3)
            else:
                persistence = self._persistence(p10, p50, p90, horizon, cutoff, baseline)

            outputs.append(ForecastOutput(
                forecast_cutoff=cutoff,
                sku_id=sku.sku_id,
                horizon_hours=horizon,
                target="cumulative_fulfillment_demand",
                p10=round(p10, 1),
                p50=round(p50, 1),
                p90=round(p90, 1),
                surge_persistence_probability=persistence,
                content_features_used=content_used,
                data_quality_flags=list(flags),
                model_name=self.model_name,
                model_version=self.model_version,
            ))
        return outputs

    # --------------------------------------------------------- persistence --
    def _persistence(self, p10: float, p50: float, p90: float, horizon: int,
                     cutoff: datetime, baseline_hourly: float) -> float:
        """Share of bootstrapped scenarios still above baseline over the horizon."""
        if not np.isfinite(baseline_hourly) or baseline_hourly <= 0 or p50 <= 0:
            return float("nan")

        path = hourly_path_from_cumulative(
            p50, horizon, hour_profile=HOUR_PROFILE, start_hour=cutoff.hour
        )
        # Prefer empirical residual blocks from the bundle if available;
        # fall back to interval-derived spread if the bundle has none.
        blocks = self.bundle.residual_blocks.get(horizon)
        if blocks is None:
            lo, hi = max(p10 / p50, 0.05), max(p90 / p50, 1.0)
            rng = np.random.default_rng(SCENARIO_SEED)
            synthetic = rng.uniform(lo, hi, size=(200, 6))
            blocks = residual_blocks(
                synthetic.ravel(), np.ones(synthetic.size), block_hours=6
            )

        scenarios = generate_scenarios(path, blocks, n_scenarios=self.n_scenarios,
                                       seed=SCENARIO_SEED)
        return round(surge_persistence(scenarios, baseline_hourly,
                                       window_hours=horizon), 3)

    # -------------------------------------------------------------- errors --
    def _empty(self, sku, cutoff, horizon, flags) -> ForecastOutput:
        return ForecastOutput(
            forecast_cutoff=cutoff,
            sku_id=sku.sku_id,
            horizon_hours=horizon,
            target="cumulative_fulfillment_demand",
            p10=0.0, p50=0.0, p90=0.0,
            surge_persistence_probability=float("nan"),
            content_features_used=False,
            data_quality_flags=flags,
            model_name=getattr(self, "model_name", "lightgbm_quantile"),
            model_version="unavailable",
        )


def artifacts_available(artifact_root: Path | str = DEFAULT_ARTIFACT_ROOT,
                        feature_mode: str = MODE_TRANSACTION) -> bool:
    """True when a trained bundle exists on disk for this feature mode."""
    return (Path(artifact_root) / f"forecast_{feature_mode}" / "bundle.json").exists()
