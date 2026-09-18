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

import math
from dataclasses import replace
from datetime import timedelta

from django.db.models import Sum

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

# PRD 11 lists shipping-failure, return and holding costs as explicit terms of
# the objective, but PRD 9.2's decision configuration carries no parameters for
# any of them. PRD 11 settles that case itself: "Jika parameter ekonomi belum
# tersedia, simulator menggunakan asumsi yang ditampilkan di UI."
#
# Cancellation, delivery-failure and return rates are therefore measured from
# the SKU's own history rather than assumed -- PRD 9.1 records every fate an
# order can have. Only the holding rate, which no table records, falls back to
# the stated assumption below. Both paths are reported in `assumptions`.
FULFILMENT_RATE_WINDOW_HOURS = 24 * 30
HOLDING_COST_RATE_PER_DAY = 0.001  # share of unit_variable_cost, per unit, per day

# Every candidate is scored against three demand scenarios -- the forecast's
# P10, P50 and P90 -- instead of P50 alone. Sizing orders to cover P90 while
# valuing them only at P50 made any safety stock a guaranteed loss on paper:
# the buffer could never be sold in the one scenario it was scored in. These
# are the Swanson's-rule weights (0.3 / 0.4 / 0.3), the standard way to turn
# P10/P50/P90 into an expected value when the full distribution is unknown.
DEMAND_SCENARIO_WEIGHTS = (("p10", 0.3), ("p50", 0.4), ("p90", 0.3))


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

        demand_scenarios = [
            (weight, float(getattr(forecast, quantile)))
            for quantile, weight in DEMAND_SCENARIO_WEIGHTS
        ]

        gap_p50 = max(demand_p50 - current_stock, 0)
        gap_p90 = max(demand_p90 - current_stock, 0)

        def feasible(units: float, ceiling: float) -> int:
            """Clamp to the capacity/capital ceiling, honour the MOQ, return whole units.

            A batch smaller than `minimum_commitment` cannot be placed at all,
            so it is either rounded up to the MOQ (when the ceilings allow) or
            dropped entirely -- never silently ordered below the minimum.

            Quantities are whole units from here on, before anything is scored:
            you cannot produce or order 318.2 units, and rounding only at
            display time made the UI show a batch of 318 while the economics
            behind it were computed on 318.2 -- numbers a reader could not
            reproduce (PRD 19: "Semua angka rekomendasi dapat ditelusuri ke
            tool output"). Rounding is downward so a batch can never breach
            the capital or capacity ceiling it was just clamped to.
            """
            units = math.floor(max(0.0, min(units, ceiling)))
            moq = math.ceil(minimum_commitment)
            if units <= 0 or moq <= 0 or units >= moq:
                return units
            return moq if moq <= ceiling else 0

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

        fulfilment_rates = self._fulfilment_rates(sku, now)

        candidates = [
            self._score_candidate(
                action="COMMIT_NOW",
                commit_now_units=commit_now_full,
                commit_later_units=0,
                reevaluate_at=deadline,
                demand_scenarios=demand_scenarios,
                current_stock=current_stock,
                fulfilment_rates=fulfilment_rates,
                horizon_days=horizon_days,
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
                demand_scenarios=demand_scenarios,
                current_stock=current_stock,
                fulfilment_rates=fulfilment_rates,
                horizon_days=horizon_days,
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
                demand_scenarios=demand_scenarios,
                current_stock=current_stock,
                fulfilment_rates=fulfilment_rates,
                horizon_days=horizon_days,
                unit_selling_price=unit_selling_price,
                unit_variable_cost=unit_variable_cost,
                salvage_value_per_unit=effective_salvage,
                delay_risk=blended_risk("WAIT", [(wait_units, deadline)]),
                material_per_unit=material_per_unit,
            ),
        ]

        # Buying nothing is always an option, and every purchase has to beat
        # it. Without this baseline the engine could only pick the least-bad
        # of three purchases, and would recommend one even when all three
        # lose money. Scored the same way, so an under-stocked shelf still
        # pays its lost-sales penalty here.
        buy_nothing = self._score_candidate(
            action="NO_BUY_UNPROFITABLE",
            commit_now_units=0,
            commit_later_units=0,
            reevaluate_at=deadline,
            demand_scenarios=demand_scenarios,
            current_stock=current_stock,
            fulfilment_rates=fulfilment_rates,
            horizon_days=horizon_days,
            unit_selling_price=unit_selling_price,
            unit_variable_cost=unit_variable_cost,
            salvage_value_per_unit=effective_salvage,
            delay_risk=0.0,
            material_per_unit=material_per_unit,
        )

        # On a tie, the plan that ties up less stock -- and commits it later --
        # wins: equal expected value for less money at risk. Without this,
        # `max` returns whichever candidate happens to be listed first.
        def rank(c: ActionCandidate):
            return (
                c.expected_contribution,
                -(c.commit_now_units + c.commit_later_units),
                -c.commit_now_units,
            )

        # When nothing can or should be bought, all three candidates collapse
        # to the same zero-unit plan with identical economics. Report the
        # actual state instead, and drop the alternatives: there is nothing to
        # compare.
        if all(c.commit_now_units + c.commit_later_units == 0 for c in candidates):
            recommended = replace(
                candidates[0],
                action="NO_BUY_NEEDED" if gap_p90 <= 0 else "NO_BUY_POSSIBLE",
                reevaluate_at=deadline,
            )
            alternatives = candidates[1:]
        else:
            best = max(candidates, key=rank)
            if rank(best) <= rank(buy_nothing):
                # Every purchase is expected to earn less than holding off,
                # so recommend holding off -- and keep all three purchases as
                # alternatives so the seller can see what each would cost.
                recommended = buy_nothing
                alternatives = candidates
            else:
                recommended = best
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

        # PRD 11 requires every economic assumption to be visible in the UI.
        if fulfilment_rates["measured"]:
            assumptions.append(
                f"Rate pemenuhan dari histori {FULFILMENT_RATE_WINDOW_HOURS / 24:.0f} hari "
                f"terakhir: batal sebelum kirim {fulfilment_rates['cancel_rate']:.1%}, "
                f"gagal kirim {fulfilment_rates['delivery_failure_rate']:.1%}, "
                f"retur {fulfilment_rates['return_rate']:.1%}."
            )
        else:
            assumptions.append(
                "Belum ada histori order untuk mengukur rate batal/gagal kirim/retur; "
                "ketiganya dianggap 0%."
            )
        assumptions.append(
            "Untung tiap opsi adalah rata-rata tertimbang tiga skenario permintaan: "
            + ", ".join(
                f"{quantile.upper()} {getattr(forecast, quantile):.0f} unit ({weight:.0%})"
                for quantile, weight in DEMAND_SCENARIO_WEIGHTS
            )
            + ". Opsi beli hanya direkomendasikan bila untungnya melebihi tidak membeli."
        )
        assumptions.append(
            f"Biaya simpan {HOLDING_COST_RATE_PER_DAY:.1%} dari biaya per unit per hari "
            "(asumsi placeholder: tidak ada parameter biaya simpan di konfigurasi)."
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

    @staticmethod
    def _fulfilment_rates(sku: SKU, now) -> dict:
        """Measure cancel / delivery-failure / return rates from PRD 9.1 history.

        PRD 6.2: these affect expected contribution, they never reduce
        fulfilment demand -- so they are money-side only and the committed
        quantities stay untouched.
        """
        since = now - timedelta(hours=FULFILMENT_RATE_WINDOW_HOURS)
        totals = sku.observations.filter(
            timestamp__lte=now, timestamp__gte=since
        ).aggregate(
            created=Sum("orders_created"),
            cancelled=Sum("orders_cancelled_pre_ship"),
            shipped=Sum("orders_shipped"),
            delivered=Sum("orders_delivered"),
            returned=Sum("orders_returned"),
        )
        created, cancelled, shipped, delivered, returned = (
            totals[key] or 0
            for key in ("created", "cancelled", "shipped", "delivered", "returned")
        )

        def share(part, whole) -> float:
            return min(max(part / whole, 0.0), 1.0) if whole else 0.0

        return {
            "cancel_rate": share(cancelled, created),
            "delivery_failure_rate": share(shipped - delivered, shipped),
            "return_rate": share(returned, delivered),
            "measured": created > 0,
        }

    def _score_candidate(
        self,
        action: str,
        commit_now_units: float,
        commit_later_units: float,
        reevaluate_at,
        demand_scenarios: list[tuple[float, float]],
        current_stock: float,
        unit_selling_price: float,
        unit_variable_cost: float,
        salvage_value_per_unit: float,
        delay_risk: float,
        fulfilment_rates: dict,
        horizon_days: float,
        material_per_unit: float | None = None,
    ) -> ActionCandidate:
        """Probability-weighted outcome of one commitment across demand scenarios."""
        committed_total = commit_now_units + commit_later_units
        outcomes = [
            (
                weight,
                self._scenario_outcome(
                    demand=demand,
                    committed_total=committed_total,
                    current_stock=current_stock,
                    unit_selling_price=unit_selling_price,
                    unit_variable_cost=unit_variable_cost,
                    salvage_value_per_unit=salvage_value_per_unit,
                    delay_risk=delay_risk,
                    fulfilment_rates=fulfilment_rates,
                    horizon_days=horizon_days,
                ),
            )
            for weight, demand in demand_scenarios
        ]
        total_weight = sum(weight for weight, _ in outcomes)

        def expected(key: str) -> float:
            return sum(weight * outcome[key] for weight, outcome in outcomes) / total_weight

        expected_contribution = expected("contribution")
        fill_rate = expected("fill_rate")
        lost_units = expected("lost_units")
        residual_units = expected("residual_units")
        ending_inventory_units = expected("ending_inventory_units")
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
            ending_inventory_units=round(ending_inventory_units, 1),
            required_material=(
                round(committed_total * material_per_unit, 2)
                if material_per_unit
                else None
            ),
        )

    @staticmethod
    def _scenario_outcome(
        demand: float,
        committed_total: float,
        current_stock: float,
        unit_selling_price: float,
        unit_variable_cost: float,
        salvage_value_per_unit: float,
        delay_risk: float,
        fulfilment_rates: dict,
        horizon_days: float,
    ) -> dict:
        """PRD 11 contribution of one commitment if demand turns out to be `demand`."""
        # Stock already on the shelf serves demand first: it is a sunk
        # purchase, so it removes lost sales but earns this decision no
        # revenue. Only the newly committed units are scored on their own
        # economics -- otherwise a SKU whose shelf already covers demand
        # would be charged a lost-sales penalty for demand it fully serves.
        served_from_stock = min(current_stock, demand)
        unmet_demand = max(demand - served_from_stock, 0)

        # `committed_total` units are produced/paid for regardless of delay
        # risk. Delay risk only shrinks how much of the demand-matching
        # portion actually arrives on time to be sold (delivered/lost); it
        # must NOT shrink the base used for the residual-stock calculation,
        # otherwise a riskier, later commitment would look like it wastes
        # less stock than committing now -- which is backwards.
        raw_deliverable = min(committed_total, unmet_demand)
        delivered = raw_deliverable * (1 - delay_risk)
        lost_units = max(unmet_demand - delivered, 0)
        residual_units = max(committed_total - delivered, 0)

        # PRD 6.2 / 11: cancellations, failed deliveries and returns move money,
        # never fulfilment demand -- so they thin out the units that actually
        # turn into `successful_sales` without changing what was committed.
        cancelled_units = delivered * fulfilment_rates["cancel_rate"]
        shipped_units = delivered - cancelled_units
        failed_delivery_units = shipped_units * fulfilment_rates["delivery_failure_rate"]
        arrived_units = shipped_units - failed_delivery_units
        returned_units = arrived_units * fulfilment_rates["return_rate"]
        successful_sales = arrived_units - returned_units

        # PRD 11: "Biaya tidak boleh dihitung dua kali." Every committed unit is
        # charged its procurement cost exactly once, here and nowhere else. What
        # separates the outcomes is how much value comes back afterwards:
        #   sold        -> selling price
        #   returned    -> goods back on the shelf, worth their terminal value
        #   cancelled   -> never shipped, still on the shelf
        #   unsold      -> ending inventory, PRD 11's "explicit terminal value"
        #   lost in transit -> nothing recovered; that IS the shipping-failure cost
        delivered_revenue = successful_sales * unit_selling_price
        procurement_cost = committed_total * unit_variable_cost
        recovered_units = cancelled_units + returned_units + residual_units
        terminal_value = recovered_units * salvage_value_per_unit

        # Ending inventory (FR-O04 / PRD 16.2): what is still on the shelf once
        # the horizon closes. Units leave for good only by being sold or lost in
        # transit; returns and cancellations come back.
        ending_inventory_units = max(
            current_stock
            + committed_total
            - served_from_stock
            - successful_sales
            - failed_delivery_units,
            0.0,
        )
        # Holding is charged only on the inventory this decision creates. Stock
        # that was already on the shelf is sunk: it earns this decision no
        # revenue, so it must not be charged its carrying cost here either --
        # and since that cost is identical across all candidates it never
        # changes the ranking, it would only push "buy nothing" below zero.
        holding_cost = (
            (committed_total + residual_units)
            / 2
            * unit_variable_cost
            * HOLDING_COST_RATE_PER_DAY
            * horizon_days
        )

        lost_sales_penalty = lost_units * (unit_selling_price - unit_variable_cost) * 0.5

        contribution = (
            delivered_revenue
            - procurement_cost
            + terminal_value
            - holding_cost
            - lost_sales_penalty
        )
        # Fill rate answers "how much demand gets served", so it counts both
        # sources -- unlike contribution, which counts only new units.
        fill_rate = (served_from_stock + delivered) / demand if demand > 0 else 1.0

        return {
            "contribution": contribution,
            "fill_rate": fill_rate,
            "lost_units": lost_units,
            "residual_units": residual_units,
            "ending_inventory_units": ending_inventory_units,
        }
