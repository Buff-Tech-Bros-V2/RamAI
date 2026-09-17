"""Replace the absolute commitment deadline with a rolling decision window.

The stored timestamp expired: once it passed, every recommendation for the
SKU was scored against a deadline in the past. The window is re-anchored on
each decision instead (see DecisionConfig.deadline_from).
"""

from django.db import migrations, models


def deadline_to_window(apps, schema_editor):
    """Preserve each SKU's effective window: deadline - latest observation."""
    DecisionConfig = apps.get_model("skus", "DecisionConfig")
    HourlyObservation = apps.get_model("skus", "HourlyObservation")

    for config in DecisionConfig.objects.all():
        latest = (
            HourlyObservation.objects.filter(sku_id=config.sku_id)
            .order_by("-timestamp")
            .first()
        )
        if latest is None or config.commitment_deadline is None:
            continue
        hours = (config.commitment_deadline - latest.timestamp).total_seconds() / 3600
        config.decision_window_hours = max(1, min(round(hours), 720))
        config.save(update_fields=["decision_window_hours"])


def window_to_deadline(apps, schema_editor):
    """Reverse: anchor the window on each SKU's latest observation."""
    from datetime import timedelta

    DecisionConfig = apps.get_model("skus", "DecisionConfig")
    HourlyObservation = apps.get_model("skus", "HourlyObservation")

    for config in DecisionConfig.objects.all():
        latest = (
            HourlyObservation.objects.filter(sku_id=config.sku_id)
            .order_by("-timestamp")
            .first()
        )
        if latest is None:
            continue
        config.commitment_deadline = latest.timestamp + timedelta(
            hours=config.decision_window_hours
        )
        config.save(update_fields=["commitment_deadline"])


class Migration(migrations.Migration):

    dependencies = [
        ("skus", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="decisionconfig",
            name="decision_window_hours",
            field=models.PositiveSmallIntegerField(
                default=24,
                help_text="How many hours from now the commitment decision must be made.",
            ),
        ),
        migrations.AlterField(
            model_name="decisionconfig",
            name="daily_capacity_minutes",
            field=models.FloatField(
                blank=True,
                null=True,
                help_text="Shop-level production/handling capacity per day.",
            ),
        ),
        migrations.RunPython(deadline_to_window, window_to_deadline),
        migrations.RemoveField(
            model_name="decisionconfig",
            name="commitment_deadline",
        ),
    ]
