"""Root URL configuration."""

from django.urls import include, path

urlpatterns = [
    path("api/v1/", include("fuelplanner.api.urls")),
]
