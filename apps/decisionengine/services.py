"""
Decision engine: turns a forecast + constraints into the three candidate
actions from PRD section 7/11 (commit now, staged commitment, wait) and
picks a recommendation.

Unlike forecasting/explanation, this module is REAL MVP logic (not a
placeholder): a simple, deterministic enumeration of three commitment
sizes scored with the contribution formula from PRD section 11. It does
not require the real regression model or an LLM to be useful -- it just
needs a ForecastOutput (dummy or real) as input. When the real
quantile-regression forecast is wired in, this module should keep
working unchanged since it only depends on the ForecastOutput contract.

Known simplification vs. PRD: this evaluates one SKU at a time, so
FR-O07 (cross-SKU capacity allocation) is not implemented (marked P1 in
the PRD).
"""

from apps.forecasting.dataclasses import ForecastOutput
from apps.skus.models import DecisionConfig, SKU

from .dataclasses import ActionCandidate, DecisionResult

# Simplified delay-risk assumption: fraction of committed units treated as
# "too late to fully capture horizon demand" for each action. Placeholder
# until a real transaction-risk / lead-time model exists (PRD 10.4).
DELAY_RISK_FRACTION = {
    "COMMIT_NOW": 0.0,
    "STAGED_COMMITMENT": 0.05,
    "WAIT": 0.15,
}


class DecisionEngine:
    def evaluate_actions(
        self,
        sku: SKU,
        decision_config: DecisionConfig,
        forecast: ForecastOutput,
        current_stock: int,
        now,
        overrides: dict | None = None,
    ) -> DecisionResult:
        overrides = overrides or {}

        capacity_minutes = overrides.get(
            "daily_capacity_minutes", decision_config.daily_capacity_minutes
        )
        capital_limit = float(
            overrides.get("working_capital_limit", decision_config.working_capital_limit)
        )
        deadline = overrides.get("commitment_deadline", decision_config.commitment_deadline)

        production_minutes_per_unit = decision_config.production_minutes_per_unit
        unit_selling_price = float(decision_config.unit_selling_price)
        unit_variable_cost = float(decision_config.unit_variable_cost)
        salvage_value_per_unit = float(decision_config.salvage_value_per_unit)

        # `daily_capacity_minutes` is a per-day figure, but demand is cumulative
        # over the whole forecast horizon (e.g. 48h = 2 days) -- scale capacity
        # to the horizon so it isn't under-counted by ~horizon_days times.
        horizon_days = forecast.horizon_hours / 24
        max_units_capacity = (
            (capacity_minutes * horizon_days) / production_minutes_per_unit
            if production_minutes_per_unit
            else float("inf")
        )
        max_units_capital = (
            capital_limit / unit_variable_cost if unit_variable_cost else float("inf")
        )
        max_units = min(max_units_capacity, max_units_capital)

        demand_p50 = forecast.p50
        demand_p90 = forecast.p90

        gap_p50 = max(demand_p50 - current_stock, 0)
        gap_p90 = max(demand_p90 - current_stock, 0)

        def clamp(units: float) -> float:
            return max(0.0, min(units, max_units))

        commit_now_full = clamp(gap_p90)
        commit_now_staged = clamp(gap_p50)
        commit_later_staged = clamp(gap_p90 - commit_now_staged)
        wait_units = clamp(gap_p90)

        midpoint = now + (deadline - now) / 2

        candidates = [
            self._score_candidate(
                action="COMMIT_NOW",
                commit_now_units=commit_now_full,
                commit_later_units=0,
                reevaluate_at=deadline,
                demand_p50=demand_p50,
                unit_selling_price=unit_selling_price,
                unit_variable_cost=unit_variable_cost,
                salvage_value_per_unit=salvage_value_per_unit,
            ),
            self._score_candidate(
                action="STAGED_COMMITMENT",
                commit_now_units=commit_now_staged,
                commit_later_units=commit_later_staged,
                reevaluate_at=midpoint,
                demand_p50=demand_p50,
                unit_selling_price=unit_selling_price,
                unit_variable_cost=unit_variable_cost,
                salvage_value_per_unit=salvage_value_per_unit,
            ),
            self._score_candidate(
                action="WAIT",
                commit_now_units=0,
                commit_later_units=wait_units,
                reevaluate_at=deadline,
                demand_p50=demand_p50,
                unit_selling_price=unit_selling_price,
                unit_variable_cost=unit_variable_cost,
                salvage_value_per_unit=salvage_value_per_unit,
            ),
        ]

        recommended = max(candidates, key=lambda c: c.expected_contribution)
        alternatives = [c for c in candidates if c is not recommended]

        if "insufficient_history" in forecast.data_quality_flags:
            confidence = "LOW"
        elif not forecast.content_features_used:
            confidence = "MEDIUM"
        else:
            confidence = "HIGH"

        assumptions = [
            f"Kapasitas produksi {capacity_minutes:.0f} menit/hari.",
            f"Modal kerja tersedia Rp{capital_limit:,.0f}.",
            f"Commitment deadline {deadline:%Y-%m-%d %H:%M}.",
            "Risiko keterlambatan staged/wait pakai asumsi placeholder "
            f"({DELAY_RISK_FRACTION['STAGED_COMMITMENT']:.0%}/"
            f"{DELAY_RISK_FRACTION['WAIT']:.0%}), belum dari model transaction risk.",
        ]

        return DecisionResult(
            decision_time=now,
            sku_id=sku.sku_id,
            operation_mode=decision_config.operation_mode,
            constraint_profile=decision_config.constraint_profile,
            recommended=recommended,
            alternatives=alternatives,
            surge_persistence_48h=forecast.surge_persistence_probability,
            confidence=confidence,
            assumptions=assumptions,
        )

    def _score_candidate(
        self,
        action: str,
        commit_now_units: float,
        commit_later_units: float,
        reevaluate_at,
        demand_p50: float,
        unit_selling_price: float,
        unit_variable_cost: float,
        salvage_value_per_unit: float,
    ) -> ActionCandidate:
        committed_total = commit_now_units + commit_later_units
        delay_risk = DELAY_RISK_FRACTION[action]

        # `committed_total` units are produced/paid for regardless of delay
        # risk. Delay risk only shrinks how much of the demand-matching
        # portion actually arrives on time to be sold (delivered/lost); it
        # must NOT shrink the base used for the residual-stock calculation,
        # otherwise a riskier, later commitment would look like it wastes
        # less stock than committing now -- which is backwards.
        raw_deliverable = min(committed_total, demand_p50)
        delivered = raw_deliverable * (1 - delay_risk)
        lost_units = max(demand_p50 - delivered, 0)
        residual_units = max(committed_total - delivered, 0)

        revenue = delivered * unit_selling_price
        variable_cost = committed_total * unit_variable_cost
        residual_cost = residual_units * (unit_variable_cost - salvage_value_per_unit)
        lost_sales_penalty = lost_units * (unit_selling_price - unit_variable_cost) * 0.5

        expected_contribution = revenue - variable_cost - residual_cost - lost_sales_penalty
        fill_rate = delivered / demand_p50 if demand_p50 > 0 else 1.0
        required_capital = committed_total * unit_variable_cost

        return ActionCandidate(
            action=action,
            commit_now_units=round(commit_now_units),
            commit_later_units=round(commit_later_units),
            reevaluate_at=reevaluate_at,
            required_capital=round(required_capital, 2),
            expected_contribution=round(expected_contribution, 2),
            expected_fill_rate=round(fill_rate, 2),
            expected_lost_units=round(lost_units, 1),
            residual_stock_risk_units=round(residual_units, 1),
        )
