"""
Data layer for RamAI (PRD section 9.1 & 9.2).

These models hold SKU master data, per-SKU decision configuration
(constraints used by the decision engine), and hourly historical
observations. For the MVP, all rows are dummy/synthetic data created by
the `seed_dummy_data` management command -- there is no real
marketplace or ERP integration yet (see PRD section 21, Phase 1/2).
"""

from datetime import timedelta

from django.conf import settings
from django.db import models


class OperationMode(models.TextChoices):
    PRODUCTION = "PRODUCTION", "Production"
    REPLENISHMENT = "REPLENISHMENT", "Replenishment"


class ConstraintProfile(models.TextChoices):
    FOOD_DEMO = "FOOD_DEMO", "Food demo"
    FASHION = "FASHION", "Fashion"
    BEAUTY = "BEAUTY", "Beauty"
    ELECTRONICS = "ELECTRONICS", "Electronics"


# Starting points offered in the product form so a seller picking a profile
# does not face an empty grid. Every value stays editable -- these are
# defaults, not constraints, and only fill fields the user left blank.
CONSTRAINT_PROFILE_PRESETS = {
    ConstraintProfile.FOOD_DEMO: {
        "operation_mode": OperationMode.PRODUCTION,
        "shelf_life_hours": 48,
        "minimum_commitment": 20,
        "decision_window_hours": 6,
    },
    ConstraintProfile.FASHION: {
        "operation_mode": OperationMode.REPLENISHMENT,
        "shelf_life_hours": None,
        "minimum_commitment": 50,
        "decision_window_hours": 24,
    },
    ConstraintProfile.BEAUTY: {
        "operation_mode": OperationMode.REPLENISHMENT,
        "shelf_life_hours": 8760,
        "minimum_commitment": 30,
        "decision_window_hours": 24,
    },
    ConstraintProfile.ELECTRONICS: {
        "operation_mode": OperationMode.REPLENISHMENT,
        "shelf_life_hours": None,
        "minimum_commitment": 10,
        "decision_window_hours": 48,
    },
}


class SKU(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="skus",
        help_text="Seller account this product belongs to.",
    )
    sku_id = models.CharField(max_length=32, primary_key=True)
    name = models.CharField(max_length=120)
    product_category = models.CharField(max_length=60)
    shop_id = models.CharField(max_length=32, default="SHOP-DEMO")
    demo_scenario = models.CharField(
        max_length=60,
        blank=True,
        help_text="Which PRD demo scenario this SKU's dummy data illustrates.",
    )

    class Meta:
        ordering = ["sku_id"]

    def __str__(self):
        return f"{self.sku_id} - {self.name}"


class DecisionConfig(models.Model):
    """Constraint/economics profile used by the decision engine (PRD 9.2)."""

    sku = models.OneToOneField(
        SKU, on_delete=models.CASCADE, related_name="decision_config"
    )
    operation_mode = models.CharField(max_length=20, choices=OperationMode.choices)
    constraint_profile = models.CharField(
        max_length=20, choices=ConstraintProfile.choices
    )
    unit_selling_price = models.DecimalField(max_digits=12, decimal_places=2)
    unit_variable_cost = models.DecimalField(max_digits=12, decimal_places=2)
    production_minutes_per_unit = models.FloatField(null=True, blank=True)
    material_per_unit = models.FloatField(null=True, blank=True)
    supplier_lead_time_hours = models.FloatField(null=True, blank=True)
    minimum_commitment = models.PositiveIntegerField(default=0)
    shelf_life_hours = models.FloatField(null=True, blank=True)
    salvage_value_per_unit = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    # Only binding in PRODUCTION mode, where it pairs with
    # `production_minutes_per_unit`; replenishment sellers leave it blank.
    daily_capacity_minutes = models.FloatField(
        null=True,
        blank=True,
        help_text="Shop-level production/handling capacity per day.",
    )
    working_capital_limit = models.DecimalField(max_digits=14, decimal_places=2)
    # Rolling horizon rather than a fixed timestamp: an absolute deadline goes
    # stale the moment it passes, silently making every recommendation for the
    # SKU a calculation against a date in the past.
    decision_window_hours = models.PositiveSmallIntegerField(
        default=24,
        help_text="How many hours from now the commitment decision must be made.",
    )

    def deadline_from(self, now):
        """Absolute commitment deadline for a decision taken at `now`."""
        return now + timedelta(hours=self.decision_window_hours)

    def __str__(self):
        return f"DecisionConfig({self.sku_id})"


class HourlyObservation(models.Model):
    """One hourly row per SKU (PRD 9.1). Dummy data for MVP."""

    sku = models.ForeignKey(SKU, on_delete=models.CASCADE, related_name="observations")
    timestamp = models.DateTimeField()

    orders_created = models.PositiveIntegerField(default=0)
    orders_cancelled_pre_ship = models.PositiveIntegerField(default=0)
    orders_shipped = models.PositiveIntegerField(default=0)
    orders_delivered = models.PositiveIntegerField(default=0)
    orders_returned = models.PositiveIntegerField(default=0)

    stock_on_hand = models.IntegerField(default=0)
    incoming_stock = models.IntegerField(default=0)
    stockout_flag = models.BooleanField(default=False)

    price = models.DecimalField(max_digits=12, decimal_places=2)
    promotion_flag = models.BooleanField(default=False)

    # Content/affiliate signal - nullable because it may be unavailable
    # (PRD FR-D06: system must run with all content features null).
    product_views = models.PositiveIntegerField(null=True, blank=True)
    affiliate_orders = models.PositiveIntegerField(null=True, blank=True)
    active_affiliates = models.PositiveIntegerField(null=True, blank=True)
    content_view_velocity = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["timestamp"]
        unique_together = ("sku", "timestamp")
        indexes = [models.Index(fields=["sku", "timestamp"])]

    def __str__(self):
        return f"{self.sku_id} @ {self.timestamp:%Y-%m-%d %H:%M}"
