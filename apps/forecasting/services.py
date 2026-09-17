"""
Forecasting service layer.

`ForecastProvider` is the contract the rest of the app (decision engine,
agent) depends on. `DummyForecastProvider` is a PLACEHOLDER: it derives
P10/P50/P90 and surge-persistence heuristically from recent observed
orders so the pipeline has believable numbers to work with end to end.

TODO (teammate): replace `DummyForecastProvider` with the real
quantile-regression pipeline described in PRD section 10.3 (LightGBM /
XGBoost quantile models trained on lag/rolling/content features). Keep
the `ForecastProvider.run_forecast` signature and `ForecastOutput`
contract unchanged so `apps.decisionengine` and `apps.agent` do not need
to change.
"""

import random
from abc import ABC, abstractmethod
from datetime import datetime

from apps.skus.models import SKU

from .dataclasses import ForecastOutput

DEFAULT_HORIZONS_HOURS = (24, 48, 72)

# Which trained bundle the app serves. Transaction-only is the default because
# the content features did not show a reliable gain on the demo data -- see
# data/README.md "What the models found on this data". Switch to
# features.MODE_CONTENT to serve the enriched model instead.
FORECAST_FEATURE_MODE = "transaction"


class ForecastProvider(ABC):
    @abstractmethod
    def run_forecast(
        self,
        sku: SKU,
        cutoff: datetime,
        horizons_hours: tuple[int, ...] = DEFAULT_HORIZONS_HOURS,
    ) -> list[ForecastOutput]:
        """Return one ForecastOutput per requested horizon."""


class DummyForecastProvider(ForecastProvider):
    """PLACEHOLDER forecasting implementation -- see module docstring."""

    model_name = "dummy_heuristic_placeholder"
    model_version = "placeholder-v0"

    def run_forecast(
        self,
        sku: SKU,
        cutoff: datetime,
        horizons_hours: tuple[int, ...] = DEFAULT_HORIZONS_HOURS,
    ) -> list[ForecastOutput]:
        recent = list(
            sku.observations.filter(timestamp__lte=cutoff).order_by("-timestamp")[:24]
        )
        baseline_window = list(
            sku.observations.filter(timestamp__lte=cutoff).order_by("-timestamp")[
                24 * 6 : 24 * 8
            ]
        )

        recent_avg = self._avg_orders(recent) if recent else 0.0
        baseline_avg = self._avg_orders(baseline_window) if baseline_window else recent_avg

        surge_ratio = (recent_avg + 0.5) / (baseline_avg + 0.5)
        surge_persistence_probability = round(min(0.95, max(0.05, 1 - 1 / surge_ratio)), 2)

        content_features_used = bool(recent) and recent[0].product_views is not None
        data_quality_flags = []
        if not content_features_used:
            data_quality_flags.append("content_signal_missing")
        if len(recent) < 24:
            data_quality_flags.append("insufficient_history")

        rng = random.Random(f"{sku.sku_id}:{cutoff.isoformat()}")

        outputs = []
        for horizon in horizons_hours:
            decay = 1.0 if surge_ratio > 1 else 0.9
            hourly_rate = recent_avg * (decay ** (horizon / 24))
            p50 = max(0.0, hourly_rate * horizon)
            spread = 0.25 + rng.uniform(0, 0.1)
            p10 = round(p50 * (1 - spread), 1)
            p90 = round(p50 * (1 + spread), 1)

            outputs.append(
                ForecastOutput(
                    forecast_cutoff=cutoff,
                    sku_id=sku.sku_id,
                    horizon_hours=horizon,
                    target="cumulative_fulfillment_demand",
                    p10=p10,
                    p50=round(p50, 1),
                    p90=p90,
                    surge_persistence_probability=surge_persistence_probability,
                    content_features_used=content_features_used,
                    data_quality_flags=data_quality_flags,
                    model_name=self.model_name,
                    model_version=self.model_version,
                )
            )
        return outputs

    @staticmethod
    def _avg_orders(observations) -> float:
        if not observations:
            return 0.0
        return sum(o.orders_created for o in observations) / len(observations)


def get_forecast_provider() -> ForecastProvider:
    """Factory so callers don't import a concrete provider directly.

    Returns the trained LightGBM provider when artifacts exist on disk, and
    falls back to the heuristic placeholder when they do not -- so the app
    still runs on a fresh checkout before anyone has trained a model.

    Train the artifacts with:  python scripts/train_forecast.py
    """
    from .provider import LightGBMForecastProvider, artifacts_available

    if artifacts_available(feature_mode=FORECAST_FEATURE_MODE):
        return LightGBMForecastProvider(feature_mode=FORECAST_FEATURE_MODE)
    return DummyForecastProvider()
