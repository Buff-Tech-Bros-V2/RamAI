"""Explanation packet contract (PRD section 9.5 / `build_explanation_packet`).

The LLM (real or placeholder) must only see this packet -- never raw
model internals -- so it cannot "invent" numbers (FR-A02, FR-A07).
"""

from dataclasses import dataclass, field

from apps.decisionengine.dataclasses import DecisionResult
from apps.forecasting.dataclasses import ForecastOutput


@dataclass
class ExplanationPacket:
    sku_id: str
    forecast: ForecastOutput
    decision: DecisionResult
    warnings: list[str] = field(default_factory=list)
