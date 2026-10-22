"""API routes (mounted under /api/v1/).

The trailing slash is optional: clients such as Postman or curl often omit it, and Django's
redirect cannot preserve a POST body, so both spellings are served directly.
"""

from django.urls import re_path

from .views import FuelRouteView, HealthView

urlpatterns = [
    re_path(r"^fuel-route/?$", FuelRouteView.as_view(), name="fuel-route"),
    re_path(r"^health/?$", HealthView.as_view(), name="health"),
]
