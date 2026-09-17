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

from datetime import timedelta

from apps.forecasting.dataclasses import ForecastOutput
from apps.skus.models import DecisionConfig, SKU

from .dataclasses import ActionCandidate, DecisionResult

# Fallback delay-risk assumption, used only when the SKU has no
# `supplier_lead_time_hours` configured (typically PRODUCTION mode, where
# there is no supplier to wait for). When a lead time IS configured it is
# used directly -- see `_delay_risk` below.
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
        window_hours = overrides.get(
            "decision_window_hours", decision_config.decision_window_hours
        )
        deadline = now + timedelta(hours=float(window_hours))

        production_minutes_per_unit = decision_config.production_minutes_per_unit
        unit_selling_price = float(decision_config.unit_selling_price)
        unit_variable_cost = float(decision_config.unit_variable_cost)
        salvage_value_per_unit = float(decision_config.salvage_value_per_unit)

        minimum_commitment = float(decision_config.minimum_commitment or 0)
        lead_time_hours = decision_config.supplier_lead_time_hours
        shelf_life_hours = decision_config.shelf_life_hours
        material_per_unit = decision_config.material_per_unit
        horizon_hours = forecast.horizon_hours

        # `daily_capacity_minutes` is a per-day figure, but demand is cumulative
        # over the whole forecast horizon (e.g. 48h = 2 days) -- scale capacity
        # to the horizon so it isn't under-counted by ~horizon_days times.
        horizon_days = forecast.horizon_hours / 24
        # Capacity only binds when both halves are known: minutes available per
        # day AND minutes consumed per unit. Replenishment SKUs have neither.
        max_units_capacity = (
            (capacity_minutes * horizon_days) / production_minutes_per_unit
            if production_minutes_per_unit and capacity_minutes
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

        def feasible(units: float, ceiling: float) -> float:
            """Clamp to the capacity/capital ceiling, then honour the MOQ.

            A batch smaller than `minimum_commitment` cannot be placed at all,
            so it is either rounded up to the MOQ (when the ceilings allow) or
            dropped entirely -- never silently ordered below the minimum.
            """
            units = max(0.0, min(units, ceiling))
            if units <= 0 or minimum_commitment <= 0 or units >= minimum_commitment:
                return units
            return minimum_commitment if minimum_commitment <= ceiling else 0.0

        commit_now_full = feasible(gap_p90, max_units)
        commit_now_staged = feasible(gap_p50, max_units)
        # The later tranche only gets whatever capacity/capital the first
        # tranche left over, and must clear the MOQ on its own.
        staged_remaining = max(max_units - commit_now_staged, 0.0)
        commit_later_staged = feasible(gap_p90 - commit_now_staged, staged_remaining)
        wait_units = feasible(gap_p90, max_units)

        moq_blocked = minimum_commitment > 0 and max_units < minimum_commitment

        midpoint = now + (deadline - now) / 2

        def delay_risk(action: str, commit_at) -> float:
            """Fraction of the horizon already gone by the time stock arrives.

            With a known supplier lead time this is a real quantity: order at
            `commit_at`, goods land `lead_time_hours` later, and everything
            before that point is demand the commitment cannot serve. Without a
            lead time (production in-house) we fall back to the placeholder.
            """
            if lead_time_hours is None or horizon_hours <= 0:
                return DELAY_RISK_FRACTION[action]
            hours_until_commit = max((commit_at - now).total_seconds() / 3600, 0.0)
            return min((hours_until_commit + lead_time_hours) / horizon_hours, 1.0)

        def blended_risk(action: str, tranches) -> float:
            """Unit-weighted delay risk across tranches committed at different times."""
            total = sum(units for units, _ in tranches)
            if total <= 0:
                return delay_risk(action, now)
            return sum(units * delay_risk(action, at) for units, at in tranches) / total

        # Stock that outlives the horizon is only worth its salvage value if it
        # is still sellable. Lead time eats into shelf life before the goods
        # even reach the shop, so subtract it first.
        usable_shelf_life = (
            shelf_life_hours - (lead_time_hours or 0.0)
            if shelf_life_hours is not None
            else None
        )
        residual_spoils = (
            usable_shelf_life is not None and usable_shelf_life <= horizon_hours
        )
        effective_salvage = 0.0 if residual_spoils else salvage_value_per_unit

        candidates = [
            self._score_candidate(
                action="COMMIT_NOW",
                commit_now_units=commit_now_full,
                commit_later_units=0,
                reevaluate_at=deadline,
                demand_p50=demand_p50,
                unit_selling_price=unit_selling_price,
                unit_variable_cost=unit_variable_cost,
                salvage_value_per_unit=effective_salvage,
                delay_risk=blended_risk("COMMIT_NOW", [(commit_now_full, now)]),
                material_per_unit=material_per_unit,
            ),
            self._score_candidate(
                action="STAGED_COMMITMENT",
                commit_now_units=commit_now_staged,
                commit_later_units=commit_later_staged,
                reevaluate_at=midpoint,
                demand_p50=demand_p50,
                unit_selling_price=unit_selling_price,
                unit_variable_cost=unit_variable_cost,
                salvage_value_per_unit=effective_salvage,
                delay_risk=blended_risk(
                    "STAGED_COMMITMENT",
                    [(commit_now_staged, now), (commit_later_staged, midpoint)],
                ),
                material_per_unit=material_per_unit,
            ),
            self._score_candidate(
                action="WAIT",
                commit_now_units=0,
                commit_later_units=wait_units,
                reevaluate_at=deadline,
                demand_p50=demand_p50,
                unit_selling_price=unit_selling_price,
                unit_variable_cost=unit_variable_cost,
                salvage_value_per_unit=effective_salvage,
                delay_risk=blended_risk("WAIT", [(wait_units, deadline)]),
                material_per_unit=material_per_unit,
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
            f"Modal kerja tersedia Rp{capital_limit:,.0f}.",
            f"Keputusan harus diambil dalam {float(window_hours):.0f} jam "
            f"(paling lambat {deadline:%Y-%m-%d %H:%M}).",
        ]

        if capacity_minutes and production_minutes_per_unit:
            assumptions.insert(
                0, f"Kapasitas produksi {capacity_minutes:.0f} menit/hari."
            )
        else:
            assumptions.insert(
                0,
                "Tanpa batas kapasitas produksi (mode replenishment); kuantitas "
                "dibatasi modal kerja saja.",
            )

        if lead_time_hours is not None:
            assumptions.append(
                f"Lead time supplier {lead_time_hours:.0f} jam: risiko keterlambatan "
                f"dihitung dari porsi horizon {horizon_hours:.0f} jam yang sudah lewat "
                "saat barang tiba."
            )
        else:
            assumptions.append(
                "Tidak ada lead time supplier (produksi sendiri); risiko keterlambatan "
                f"staged/wait pakai asumsi placeholder "
                f"({DELAY_RISK_FRACTION['STAGED_COMMITMENT']:.0%}/"
                f"{DELAY_RISK_FRACTION['WAIT']:.0%})."
            )

        if minimum_commitment > 0:
            assumptions.append(
                f"Minimum produksi/pemesanan {minimum_commitment:.0f} unit per batch; "
                "batch di bawah itu dibulatkan naik atau dibatalkan."
            )
        if moq_blocked:
            assumptions.append(
                f"Kapasitas/modal hanya cukup untuk {max_units:.0f} unit, di bawah "
                f"minimum {minimum_commitment:.0f} unit -- semua opsi commit jadi 0 unit."
            )

        if shelf_life_hours is not None:
            if residual_spoils:
                assumptions.append(
                    f"Masa simpan {shelf_life_hours:.0f} jam (sisa {usable_shelf_life:.0f} "
                    f"jam setelah lead time) tidak melewati horizon {horizon_hours:.0f} jam: "
                    "stok sisa dihitung kedaluwarsa, nilai sisa Rp0."
                )
            else:
                assumptions.append(
                    f"Masa simpan {shelf_life_hours:.0f} jam melewati horizon "
                    f"{horizon_hours:.0f} jam: stok sisa masih bisa dijual, nilai sisa "
                    f"Rp{salvage_value_per_unit:,.0f}/unit."
                )

        if material_per_unit:
            assumptions.append(
                f"Kebutuhan material {material_per_unit:g} per unit; total per opsi "
                "ditampilkan sebagai kebutuhan material."
            )

        return DecisionResult(
            decision_time=now,
            sku_id=sku.sku_id,
            operation_mode=decision_config.operation_mode,
            constraint_profile=decision_config.constraint_profile,
            recommended=recommended,
            alternatives=alternatives,
            deadline=deadline,
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
        delay_risk: float,
        material_per_unit: float | None = None,
    ) -> ActionCandidate:
        committed_total = commit_now_units + commit_later_units

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
            required_material=(
                round(committed_total * material_per_unit, 2)
                if material_per_unit
                else None
            ),
        )
