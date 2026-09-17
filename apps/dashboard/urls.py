from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.index, name="index"),
    path("<str:sku_id>/approve/", views.approve_plan, name="approve_plan"),
]
