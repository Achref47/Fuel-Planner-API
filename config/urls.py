"""Root URL configuration."""

from django.urls import include, path, re_path

from fuelplanner.api.exceptions import json_404

urlpatterns = [
    path("api/v1/", include("fuelplanner.api.urls")),
    # Anything else is a JSON 404, also with DEBUG on (when Django would otherwise render its
    # HTML debug page instead of calling handler404).
    re_path(r"^", json_404),
]

# Django-level errors answer with the same JSON error format as the API.
handler400 = "fuelplanner.api.exceptions.json_400"
handler404 = "fuelplanner.api.exceptions.json_404"
handler500 = "fuelplanner.api.exceptions.json_500"
