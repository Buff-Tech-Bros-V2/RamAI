"""Typed output contracts for the decision engine (PRD section 9.3)."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ActionCandidate:
    action: str  # COMMIT_NOW | STAGED_COMMITMENT | WAIT
    commit_now_units: int
    commit_later_units: int
    reevaluate_at: datetime
    required_capital: float
    expected_contribution: float
    expected_fill_rate: float
    expected_lost_units: float
    residual_stock_risk_units: float
    # Total material consumed by this commitment (units x
    # DecisionConfig.material_per_unit). None when the SKU has no material
    # profile configured.
    required_material: float | None = None


@dataclass
class DecisionResult:
    decision_time: datetime
    sku_id: str
    operation_mode: str
    constraint_profile: str
    recommended: ActionCandidate
    alternatives: list[ActionCandidate]
    # Absolute deadline for THIS decision, derived from the SKU's rolling
    # decision window at `decision_time`.
    deadline: datetime
    surge_persistence_48h: float
    confidence: str  # HIGH | MEDIUM | LOW
    assumptions: list[str] = field(default_factory=list)
