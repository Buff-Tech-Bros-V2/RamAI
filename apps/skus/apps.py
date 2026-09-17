from django.apps import AppConfig


class SkusConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.skus"
    verbose_name = "SKUs & historical data"
