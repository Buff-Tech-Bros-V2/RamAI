"""Scope every SKU to a seller account.

The app is moving to per-seller login: each product must belong to exactly
one user so dashboards, decisions, and plans can be filtered by owner.
Existing SKUs are backfilled onto the first superuser (the only account
that existed before multi-seller support), since they were the demo food
seller's data.
"""

from django.conf import settings
from django.db import migrations, models


def assign_existing_owner(apps, schema_editor):
    SKU = apps.get_model("skus", "SKU")
    User = apps.get_model(settings.AUTH_USER_MODEL)

    unowned = SKU.objects.filter(owner__isnull=True)
    if not unowned.exists():
        return

    owner = User.objects.filter(is_superuser=True).order_by("id").first()
    if owner is None:
        owner = User.objects.order_by("id").first()
    if owner is None:
        return

    unowned.update(owner=owner)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("skus", "0002_decision_window_hours"),
    ]

    operations = [
        migrations.AddField(
            model_name="sku",
            name="owner",
            field=models.ForeignKey(
                null=True,
                on_delete=models.deletion.CASCADE,
                related_name="skus",
                to=settings.AUTH_USER_MODEL,
                help_text="Seller account this product belongs to.",
            ),
        ),
        migrations.RunPython(assign_existing_owner, noop_reverse),
        migrations.AlterField(
            model_name="sku",
            name="owner",
            field=models.ForeignKey(
                on_delete=models.deletion.CASCADE,
                related_name="skus",
                to=settings.AUTH_USER_MODEL,
                help_text="Seller account this product belongs to.",
            ),
        ),
    ]
