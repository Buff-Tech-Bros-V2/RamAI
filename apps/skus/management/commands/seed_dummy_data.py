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
from pathlib import Path

import pandas as pd
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
        daily_capacity_minutes=360,
        working_capital_limit=800000,
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
        daily_capacity_minutes=300,
        working_capital_limit=700000,
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
        daily_capacity_minutes=300,
        working_capital_limit=600000,
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
        daily_capacity_minutes=240,
        working_capital_limit=500000,
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
        parser.add_argument(
            "--data-dir",
            default="data",
            help="Directory containing simulator files (default: data).",
        )
        parser.add_argument(
            "--use-sine-waves",
            action="store_true",
            help="Force using the heuristic sine-wave generator instead of simulator parquet files.",
        )

    def handle(self, *args, **options):
        rng = random.Random(SEED)
        data_dir = Path(options["data_dir"])

        if options["flush"]:
            HourlyObservation.objects.all().delete()
            DecisionConfig.objects.all().delete()
            SKU.objects.all().delete()
            self.stdout.write("Cleared existing SKU data.")

        # If official simulator dataset exists, load it directly (Stage 1 -> Stage 2 integration)
        if (
            (data_dir / "hourly_observations.parquet").exists()
            and (data_dir / "sku_master.csv").exists()
            and not options["use_sine_waves"]
        ):
            self.stdout.write(f"Loading official simulator data from {data_dir} ...")
            self._seed_from_simulator(data_dir)
            self.stdout.write(self.style.SUCCESS("Simulator data seeding complete."))
            return

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

    def _seed_from_simulator(self, data_dir: Path):
        sku_master = pd.read_csv(data_dir / "sku_master.csv")
        decision_cfg = (
            pd.read_csv(data_dir / "decision_config.csv")
            if (data_dir / "decision_config.csv").exists()
            else None
        )
        obs_df = pd.read_parquet(data_dir / "hourly_observations.parquet")

        scenario_map = {
            "SKU-001": "Persistent surge (Scenario B)",
            "SKU-002": "Fading surge (Scenario A)",
            "SKU-003": "Capacity conflict (Scenario C)",
            "SKU-004": "Baseline, content signal missing",
            "SKU-005": "Slow burn surge",
        }

        # 1. Seed SKUs
        sku_objs = {}
        for _, row in sku_master.iterrows():
            sku_id = str(row["sku_id"])
            sku, _ = SKU.objects.update_or_create(
                sku_id=sku_id,
                defaults={
                    "name": str(row["product_name"]),
                    "product_category": str(row["product_category"]),
                    "shop_id": "SHOP-DEMO-01",
                    "demo_scenario": scenario_map.get(sku_id, ""),
                },
            )
            sku_objs[sku_id] = sku

        # 2. Seed DecisionConfigs
        if decision_cfg is not None:
            for _, row in decision_cfg.iterrows():
                sku_id = str(row["sku_id"])
                if sku_id not in sku_objs:
                    continue
                sku = sku_objs[sku_id]
                lead_time = (
                    None
                    if pd.isna(row.get("supplier_lead_time_hours"))
                    else float(row["supplier_lead_time_hours"])
                )
                deadline = pd.to_datetime(row["commitment_deadline"])
                if timezone.is_naive(deadline):
                    deadline = timezone.make_aware(deadline)

                DecisionConfig.objects.update_or_create(
                    sku=sku,
                    defaults={
                        "operation_mode": str(row.get("operation_mode", "PRODUCTION")),
                        "constraint_profile": str(row.get("constraint_profile", "FOOD_DEMO")),
                        "unit_selling_price": float(row["unit_selling_price"]),
                        "unit_variable_cost": float(row["unit_variable_cost"]),
                        "production_minutes_per_unit": float(row["production_minutes_per_unit"]),
                        "material_per_unit": float(row["material_per_unit"]),
                        "supplier_lead_time_hours": lead_time,
                        "minimum_commitment": int(row["minimum_commitment"]),
                        "shelf_life_hours": float(row["shelf_life_hours"]),
                        "salvage_value_per_unit": float(row["salvage_value_per_unit"]),
                        "daily_capacity_minutes": float(row["daily_capacity_minutes"]),
                        "working_capital_limit": float(row["working_capital_limit"]),
                        "commitment_deadline": deadline,
                    },
                )

        # 3. Seed HourlyObservations
        HourlyObservation.objects.all().delete()
        batch = []
        for row in obs_df.itertuples():
            if row.sku_id not in sku_objs:
                continue
            sku = sku_objs[row.sku_id]
            ts = row.timestamp
            if timezone.is_naive(ts):
                ts = timezone.make_aware(ts)

            pv = None if pd.isna(row.product_views) else int(row.product_views)
            ao = None if pd.isna(row.affiliate_orders) else int(row.affiliate_orders)
            aa = None if pd.isna(row.active_affiliates) else int(row.active_affiliates)
            cvv = (
                None
                if pd.isna(row.content_view_velocity)
                else float(row.content_view_velocity)
            )

            batch.append(
                HourlyObservation(
                    sku=sku,
                    timestamp=ts,
                    orders_created=int(row.orders_created),
                    orders_cancelled_pre_ship=int(row.orders_cancelled_pre_ship),
                    orders_shipped=int(row.orders_shipped),
                    orders_delivered=int(row.orders_delivered),
                    orders_returned=int(row.orders_returned),
                    stock_on_hand=int(row.stock_on_hand),
                    incoming_stock=int(row.incoming_stock),
                    stockout_flag=bool(row.stockout_flag),
                    price=float(row.price),
                    promotion_flag=bool(row.promotion_flag),
                    product_views=pv,
                    affiliate_orders=ao,
                    active_affiliates=aa,
                    content_view_velocity=cvv,
                )
            )
            if len(batch) >= 2000:
                HourlyObservation.objects.bulk_create(batch)
                batch = []

        if batch:
            HourlyObservation.objects.bulk_create(batch)

        for s_id, s_obj in sku_objs.items():
            self.stdout.write(f"Seeded {s_id} ({s_obj.name} - {s_obj.demo_scenario}) from simulator.")

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
