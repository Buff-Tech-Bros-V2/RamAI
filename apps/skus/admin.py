from django.contrib import admin

from .models import DecisionConfig, HourlyObservation, SKU


@admin.register(SKU)
class SKUAdmin(admin.ModelAdmin):
    list_display = ("sku_id", "name", "product_category", "demo_scenario")


@admin.register(DecisionConfig)
class DecisionConfigAdmin(admin.ModelAdmin):
    list_display = (
        "sku",
        "operation_mode",
        "constraint_profile",
        "daily_capacity_minutes",
        "working_capital_limit",
        "commitment_deadline",
    )


@admin.register(HourlyObservation)
class HourlyObservationAdmin(admin.ModelAdmin):
    list_display = (
        "sku",
        "timestamp",
        "orders_created",
        "stock_on_hand",
        "stockout_flag",
    )
    list_filter = ("sku", "stockout_flag")
