from django.contrib import admin

from .models import ActionPlanDraft


@admin.register(ActionPlanDraft)
class ActionPlanDraftAdmin(admin.ModelAdmin):
    list_display = (
        "sku",
        "recommended_action",
        "commit_now_units",
        "commit_later_units",
        "approved_at",
    )
