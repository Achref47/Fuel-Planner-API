"""API routes (mounted under /api/v1/)."""

from django.urls import path

from .views import FuelRouteView, HealthView

urlpatterns = [
    path("fuel-route/", FuelRouteView.as_view(), name="fuel-route"),
    path("health/", HealthView.as_view(), name="health"),
]
