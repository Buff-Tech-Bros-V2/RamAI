from django.urls import path

from . import api_views, views

app_name = "dashboard"

urlpatterns = [
    path("", views.overview, name="index"),
    path("decisions/", views.decision_center, name="decision_center_default"),
    path("decisions/<str:sku_id>/", views.decision_center, name="decision_center"),
    path("products/", views.product_list, name="product_list"),
    path("products/add/", views.product_create, name="product_create"),
    path("products/<str:sku_id>/", views.product_detail, name="product_detail"),
    path("products/<str:sku_id>/edit/", views.product_edit, name="product_edit"),
    path("plans/", views.plan_list, name="plan_list"),
    path("<str:sku_id>/approve/", views.approve_plan, name="approve_plan"),
    # HTMX partials & API
    path("api/chart-data/", api_views.chart_data, name="chart_data"),
    path("partials/dashboard/", api_views.dashboard_partial, name="dashboard_partial"),
    path("api/whatif/", api_views.whatif_recalculate, name="whatif_recalculate"),
    path(
        "decisions/<str:sku_id>/explanation/",
        api_views.explanation_partial,
        name="explanation",
    ),
]
