"""
Seeds a second demo seller: a fashion/apparel shop, so the app has more
than one tenant to prove per-user product scoping actually works.

Creates (idempotently, via update_or_create/get_or_create):
  - a "fashion_seller" user account
  - 5 clothing SKUs owned by that user, in REPLENISHMENT mode with the
    FASHION constraint profile
  - 90 days of hourly synthetic history per SKU (same heuristic generator
    used by seed_dummy_data's sine-wave path)

Safe to re-run: it only touches its own user's SKUs, never the food
seller's data.
"""

import math
import random
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.skus.models import (
    ConstraintProfile,
    DecisionConfig,
    HourlyObservation,
    OperationMode,
    SKU,
)

N_DAYS = 90
SEED = 7

FASHION_USERNAME = "fashion_seller"
FASHION_PASSWORD = "fashion123"
FASHION_EMAIL = "fashion_seller@example.com"

SCENARIOS = [
    dict(
        sku_id="FASH-001",
        name="Kemeja Flanel Pria Lengan Panjang",
        category="Fashion Pria",
        demo_scenario="Persistent surge (affiliate haul video)",
        base_level=5,
        surge_start_hours_ago=40,
        surge_peak_multiplier=5,
        surge_decay=False,
        content_missing=False,
        unit_selling_price=145000,
        unit_variable_cost=85000,
        # Short local-supplier lead time: comfortably inside the 48h forecast
        # horizon, so a same-day restock isn't automatically drowned out by
        # delay risk (see DecisionEngine._score_candidate) -- demo target:
        # COMMIT_NOW.
        supplier_lead_time_hours=12,
        working_capital_limit=25000000,
        salvage_value_per_unit=60000,
        deadline_hours_from_now=24,
    ),
    dict(
        sku_id="FASH-002",
        name="Dress Wanita Casual Motif Bunga",
        category="Fashion Wanita",
        demo_scenario="Fading surge",
        base_level=6,
        surge_start_hours_ago=30,
        surge_peak_multiplier=6,
        surge_decay=True,
        content_missing=False,
        unit_selling_price=165000,
        unit_variable_cost=95000,
        # No salvage value: an off-trend dress is close to unsellable once
        # the surge fades, so over-committing to the P90 case in one shot is
        # risky. Capital is sized between the P50 and P90 capital needs (not
        # binding at P50) so the two commitment tranches genuinely differ in
        # size -- demo target: STAGED_COMMITMENT, hedging the extra P90-P50
        # units against a checkpoint at the reevaluation midpoint instead of
        # committing them now.
        supplier_lead_time_hours=8,
        working_capital_limit=100000000,
        salvage_value_per_unit=0,
        deadline_hours_from_now=24,
    ),
    dict(
        sku_id="FASH-003",
        name="Celana Jeans Slim Fit Pria",
        category="Fashion Pria",
        demo_scenario="Capacity/MOQ conflict",
        base_level=4,
        surge_start_hours_ago=24,
        surge_peak_multiplier=4.5,
        surge_decay=False,
        content_missing=False,
        unit_selling_price=220000,
        unit_variable_cost=130000,
        # Short lead time -- demo target: COMMIT_NOW.
        supplier_lead_time_hours=12,
        working_capital_limit=22000000,
        salvage_value_per_unit=90000,
        deadline_hours_from_now=48,
    ),
    dict(
        sku_id="FASH-004",
        name="Hoodie Oversize Unisex",
        category="Fashion Unisex",
        demo_scenario="Baseline, content signal missing",
        base_level=3,
        surge_start_hours_ago=None,
        surge_peak_multiplier=1,
        surge_decay=False,
        content_missing=True,
        unit_selling_price=185000,
        unit_variable_cost=105000,
        # Lead time at/above the 48h forecast horizon: goods can't land
        # inside the window, so no commitment is worth its delay risk --
        # demo target: NO_BUY_UNPROFITABLE (tahan dulu).
        supplier_lead_time_hours=72,
        working_capital_limit=9000000,
        salvage_value_per_unit=70000,
        deadline_hours_from_now=48,
    ),
    dict(
        sku_id="FASH-005",
        name="Kaos Polos Cotton Combed 30s",
        category="Fashion Unisex",
        demo_scenario="Slow burn surge",
        base_level=8,
        surge_start_hours_ago=60,
        surge_peak_multiplier=3,
        surge_decay=False,
        content_missing=False,
        unit_selling_price=75000,
        unit_variable_cost=42000,
        # Lead time exactly at the 48h horizon: delay risk still saturates --
        # demo target: NO_BUY_UNPROFITABLE (tahan dulu).
        supplier_lead_time_hours=48,
        working_capital_limit=15000000,
        salvage_value_per_unit=30000,
        deadline_hours_from_now=24,
    ),
]


class Command(BaseCommand):
    help = "Seed a fashion seller account and its clothing SKUs with 90 days of hourly history."

    def add_arguments(self, parser):
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Delete this seller's existing SKUs/observations before reseeding.",
        )

    def handle(self, *args, **options):
        rng = random.Random(SEED)

        owner, created = User.objects.get_or_create(
            username=FASHION_USERNAME,
            defaults={"email": FASHION_EMAIL},
        )
        if created:
            owner.set_password(FASHION_PASSWORD)
            owner.save()
            self.stdout.write(
                self.style.SUCCESS(
                    f"Created user '{FASHION_USERNAME}' (password: {FASHION_PASSWORD})."
                )
            )
        else:
            self.stdout.write(f"User '{FASHION_USERNAME}' already exists.")

        if options["flush"]:
            HourlyObservation.objects.filter(sku__owner=owner).delete()
            DecisionConfig.objects.filter(sku__owner=owner).delete()
            SKU.objects.filter(owner=owner).delete()
            self.stdout.write("Cleared existing fashion seller data.")

        now = timezone.now().replace(minute=0, second=0, microsecond=0)

        for scenario in SCENARIOS:
            sku, _ = SKU.objects.update_or_create(
                sku_id=scenario["sku_id"],
                defaults={
                    "owner": owner,
                    "name": scenario["name"],
                    "product_category": scenario["category"],
                    "shop_id": "SHOP-FASHION-01",
                    "demo_scenario": scenario["demo_scenario"],
                },
            )

            DecisionConfig.objects.update_or_create(
                sku=sku,
                defaults={
                    "operation_mode": OperationMode.REPLENISHMENT,
                    "constraint_profile": ConstraintProfile.FASHION,
                    "unit_selling_price": scenario["unit_selling_price"],
                    "unit_variable_cost": scenario["unit_variable_cost"],
                    "production_minutes_per_unit": None,
                    "material_per_unit": None,
                    "supplier_lead_time_hours": scenario["supplier_lead_time_hours"],
                    "minimum_commitment": 50,
                    "shelf_life_hours": None,
                    "salvage_value_per_unit": scenario["salvage_value_per_unit"],
                    "daily_capacity_minutes": None,
                    "working_capital_limit": scenario["working_capital_limit"],
                    "decision_window_hours": scenario["deadline_hours_from_now"],
                },
            )

            self._generate_history(sku, scenario, now, rng)
            self.stdout.write(f"Seeded {sku.sku_id} ({scenario['demo_scenario']}).")

        self.stdout.write(self.style.SUCCESS("Fashion seller seeding complete."))

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
