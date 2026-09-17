"""
Persists an approved (but non-binding, non-executed) action plan draft.
See PRD FR-A05 / `draft_action_plan` tool contract: MVP never places a
real purchase/production order, it only records that a human approved a
recommendation snapshot.
"""

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models

from apps.skus.models import SKU


class ActionPlanDraft(models.Model):
    sku = models.ForeignKey(SKU, on_delete=models.CASCADE, related_name="action_plan_drafts")
    approved_at = models.DateTimeField(auto_now_add=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_plans",
    )
    recommended_action = models.CharField(max_length=32)
    commit_now_units = models.IntegerField()
    commit_later_units = models.IntegerField()
    required_capital = models.DecimalField(max_digits=14, decimal_places=2)
    expected_contribution = models.DecimalField(max_digits=14, decimal_places=2)
    decision_snapshot = models.JSONField(
        default=dict, blank=True, encoder=DjangoJSONEncoder
    )

    class Meta:
        ordering = ["-approved_at"]

    def __str__(self):
        return f"{self.sku_id} draft ({self.recommended_action}) @ {self.approved_at:%Y-%m-%d %H:%M}"
