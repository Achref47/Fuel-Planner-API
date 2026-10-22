"""Root URL configuration."""

from django.urls import include, path

urlpatterns = [
    path("api/v1/", include("fuelplanner.api.urls")),
]

# Unknown URLs and unexpected server errors answer with the same JSON error format.
handler404 = "fuelplanner.api.exceptions.json_404"
handler500 = "fuelplanner.api.exceptions.json_500"
