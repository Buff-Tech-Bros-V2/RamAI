from django.urls import path

from . import api_views, views

app_name = "dashboard"

urlpatterns = [
    path("", views.index, name="index"),
    path("<str:sku_id>/approve/", views.approve_plan, name="approve_plan"),
    # HTMX partials & API
    path("api/chart-data/", api_views.chart_data, name="chart_data"),
    path("partials/dashboard/", api_views.dashboard_partial, name="dashboard_partial"),
    path("api/whatif/", api_views.whatif_recalculate, name="whatif_recalculate"),
]
