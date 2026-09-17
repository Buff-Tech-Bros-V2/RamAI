"""Typed output contract for forecasting (PRD section 9.4)."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ForecastOutput:
    forecast_cutoff: datetime
    sku_id: str
    horizon_hours: int
    target: str
    p10: float
    p50: float
    p90: float
    surge_persistence_probability: float
    content_features_used: bool
    data_quality_flags: list[str] = field(default_factory=list)
    model_name: str = "unknown"
    model_version: str = "unknown"
