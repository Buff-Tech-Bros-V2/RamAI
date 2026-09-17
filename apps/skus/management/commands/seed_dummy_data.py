"""
Generates fully synthetic SKUs + 90 days of hourly history so the rest of
the pipeline (forecasting placeholder, decision engine, dashboard) has
something realistic to run against. See PRD section 10.2 / FR-D01-D07.

This is NOT a faithful implementation of the full synthetic-event
generator described in the PRD (no train/val/test split, no reproducible
event-seed catalogue). It only needs to produce plausible dummy numbers
for UI development. Replace/extend when the real simulator is built.
"""

import math
import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.skus.models import SKU, ConstraintProfile, DecisionConfig, HourlyObservation, OperationMode

N_DAYS = 90
SEED = 42

SCENARIOS = [
    dict(
        sku_id="SKU-001",
        name="Keripik Pedas Level 10",
        category="Makanan Kemasan",
        demo_scenario="Fading surge (Scenario A)",
        base_level=6,
        surge_start_hours_ago=30,
        surge_peak_multiplier=6,
        surge_decay=True,
        content_missing=False,
        unit_selling_price=18000,
        unit_variable_cost=11000,
        production_minutes_per_unit=3,
        daily_capacity_minutes=2850,
        working_capital_limit=21000000,
        salvage_value_per_unit=4000,
        deadline_hours_from_now=3,
    ),
    dict(
        sku_id="SKU-002",
        name="Granola Bar Cokelat",
        category="Makanan Kemasan",
        demo_scenario="Persistent surge (Scenario B)",
        base_level=5,
        surge_start_hours_ago=48,
        surge_peak_multiplier=5,
        surge_decay=False,
        content_missing=False,
        unit_selling_price=22000,
        unit_variable_cost=13000,
        production_minutes_per_unit=4,
        daily_capacity_minutes=2700,
        working_capital_limit=17500000,
        salvage_value_per_unit=5000,
        deadline_hours_from_now=3,
    ),
    dict(
        sku_id="SKU-003",
        name="Sambal Roa Botol",
        category="Makanan Kemasan",
        demo_scenario="Capacity conflict (Scenario C)",
        base_level=7,
        surge_start_hours_ago=24,
        surge_peak_multiplier=4.5,
        surge_decay=False,
        content_missing=False,
        unit_selling_price=25000,
        unit_variable_cost=15000,
        production_minutes_per_unit=5,
        daily_capacity_minutes=1800,
        working_capital_limit=11000000,
        salvage_value_per_unit=6000,
        deadline_hours_from_now=4,
    ),
    dict(
        sku_id="SKU-004",
        name="Kopi Drip Bag",
        category="Makanan Kemasan",
        demo_scenario="Baseline, content signal missing",
        base_level=4,
        surge_start_hours_ago=None,
        surge_peak_multiplier=1,
        surge_decay=False,
        content_missing=True,
        unit_selling_price=20000,
        unit_variable_cost=12000,
        production_minutes_per_unit=3,
        daily_capacity_minutes=400,
        working_capital_limit=1200000,
        salvage_value_per_unit=5000,
        deadline_hours_from_now=6,
    ),
]


class Command(BaseCommand):
    help = "Seed the database with dummy SKUs and 90 days of hourly synthetic data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Delete existing SKUs/observations before seeding.",
        )

    def handle(self, *args, **options):
        rng = random.Random(SEED)

        if options["flush"]:
            HourlyObservation.objects.all().delete()
            DecisionConfig.objects.all().delete()
            SKU.objects.all().delete()
            self.stdout.write("Cleared existing SKU data.")

        now = timezone.now().replace(minute=0, second=0, microsecond=0)

        for scenario in SCENARIOS:
            sku, _ = SKU.objects.update_or_create(
                sku_id=scenario["sku_id"],
                defaults={
                    "name": scenario["name"],
                    "product_category": scenario["category"],
                    "demo_scenario": scenario["demo_scenario"],
                },
            )

            DecisionConfig.objects.update_or_create(
                sku=sku,
                defaults={
                    "operation_mode": OperationMode.PRODUCTION,
                    "constraint_profile": ConstraintProfile.FOOD_DEMO,
                    "unit_selling_price": scenario["unit_selling_price"],
                    "unit_variable_cost": scenario["unit_variable_cost"],
                    "production_minutes_per_unit": scenario["production_minutes_per_unit"],
                    "material_per_unit": 1,
                    "supplier_lead_time_hours": None,
                    "minimum_commitment": 10,
                    "shelf_life_hours": 720,
                    "salvage_value_per_unit": scenario["salvage_value_per_unit"],
                    "daily_capacity_minutes": scenario["daily_capacity_minutes"],
                    "working_capital_limit": scenario["working_capital_limit"],
                    "commitment_deadline": now
                    + timedelta(hours=scenario["deadline_hours_from_now"]),
                },
            )

            self._generate_history(sku, scenario, now, rng)
            self.stdout.write(f"Seeded {sku.sku_id} ({scenario['demo_scenario']}).")

        self.stdout.write(self.style.SUCCESS("Dummy data seeding complete."))

    def _generate_history(self, sku, scenario, now, rng):
        HourlyObservation.objects.filter(sku=sku).delete()

        n_hours = N_DAYS * 24
        start = now - timedelta(hours=n_hours - 1)
        base_level = scenario["base_level"]
        surge_start_hours_ago = scenario["surge_start_hours_ago"]
        surge_peak_multiplier = scenario["surge_peak_multiplier"]
        surge_decay = scenario["surge_decay"]
        content_missing = scenario["content_missing"]

        stock_on_hand = round(base_level * 40)
        restock_qty = round(base_level * 60)
        price = scenario["unit_selling_price"]

        rows = []
        for h in range(n_hours):
            ts = start + timedelta(hours=h)
            hours_ago = n_hours - 1 - h

            hour_of_day = ts.hour
            seasonality = 1 + 0.3 * math.sin((hour_of_day - 14) / 24 * 2 * math.pi)
            multiplier = 1.0
            if surge_start_hours_ago is not None and hours_ago <= surge_start_hours_ago:
                progress = 1 - hours_ago / surge_start_hours_ago
                if surge_decay:
                    multiplier = 1 + (surge_peak_multiplier - 1) * math.sin(
                        progress * math.pi
                    )
                else:
                    multiplier = 1 + (surge_peak_multiplier - 1) * progress

            noise = rng.gauss(0, 0.5)
            orders_created = max(0, round(base_level * seasonality * multiplier + noise))

            incoming_stock = 0
            if h > 0 and h % (7 * 24) == 0:
                incoming_stock = restock_qty
                stock_on_hand += incoming_stock

            cancelled = round(orders_created * 0.03)
            to_ship = max(orders_created - cancelled, 0)
            shipped = min(to_ship, stock_on_hand)
            stockout_flag = shipped < to_ship
            stock_on_hand -= shipped

            delivered = shipped
            returned = round(delivered * 0.02)

            promotion_flag = rng.random() < 0.05
            row_price = round(price * 0.9) if promotion_flag else price

            if content_missing:
                product_views = affiliate_orders = active_affiliates = None
                content_view_velocity = None
            else:
                product_views = round(orders_created * rng.uniform(8, 15))
                affiliate_orders = round(orders_created * (0.4 if multiplier > 1.5 else 0.1))
                active_affiliates = max(1, round(affiliate_orders / 3))
                content_view_velocity = round(multiplier - 1, 3)

            rows.append(
                HourlyObservation(
                    sku=sku,
                    timestamp=ts,
                    orders_created=orders_created,
                    orders_cancelled_pre_ship=cancelled,
                    orders_shipped=shipped,
                    orders_delivered=delivered,
                    orders_returned=returned,
                    stock_on_hand=stock_on_hand,
                    incoming_stock=incoming_stock,
                    stockout_flag=stockout_flag,
                    price=row_price,
                    promotion_flag=promotion_flag,
                    product_views=product_views,
                    affiliate_orders=affiliate_orders,
                    active_affiliates=active_affiliates,
                    content_view_velocity=content_view_velocity,
                )
            )

        HourlyObservation.objects.bulk_create(rows, batch_size=500)
