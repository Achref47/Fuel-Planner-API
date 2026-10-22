"""HTTP endpoints."""

import logging
import time

import django
from django.conf import settings
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from ..services import callcount
from ..services.planner import plan_trip
from ..services.routing import get_routing_provider
from ..services.warmup import warm_up
from .presenters import build_geojson, build_payload
from .serializers import FuelRouteRequestSerializer

logger = logging.getLogger(__name__)

OUTPUT_FORMATS = ("json", "geojson")


class FuelRouteView(APIView):
    """POST: plan the cheapest fuel stops for a drive between two US locations."""

    def post(self, request):
        output = request.query_params.get("format", "json").lower()
        if output not in OUTPUT_FORMATS:
            raise serializers.ValidationError(
                {"format": [f"Use one of: {', '.join(OUTPUT_FORMATS)}."]}
            )
        serializer = FuelRouteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        options = serializer.validated_options()

        started = time.perf_counter()
        calls = callcount.start()
        trip = plan_trip(
            serializer.validated_data["start"],
            serializer.validated_data["finish"],
            max_detour_miles=options["max_detour_miles"],
            initial_fuel_fraction=options["initial_fuel_fraction"],
        )
        payload = build_payload(
            trip,
            geometry_mode=options["geometry"],
            started=started,
            calls=calls,
        )
        logger.info(
            "fuel-route ok: %s stops, routing=%s, %d external calls, %d ms",
            payload["summary"]["fuel_stops"],
            trip.route_cache,
            sum(calls.values()),
            payload["meta"]["elapsed_ms"],
        )
        return Response(build_geojson(payload) if output == "geojson" else payload)


class HealthView(APIView):
    """GET: liveness plus readiness of the data the planner needs (also warms the caches)."""

    throttle_classes: list = []

    def get(self, request):
        loaded = warm_up()
        ready = loaded["stations_loaded"] > 0
        body = {
            "status": "ok" if ready else "unavailable",
            "stations_loaded": loaded["stations_loaded"],
            "gazetteer_places": loaded["gazetteer_places"],
            "routing_provider": get_routing_provider().name,
            "django_version": django.get_version(),
            "check_ms": loaded["warmup_ms"],
        }
        if notice := settings.FUEL_PLANNER.get("DATA_NOTICE"):
            body["data_notice"] = notice
        if not ready:
            body["message"] = (
                "No fuel stations are loaded. Run `python manage.py load_fuel_data` "
                "(see the README for where to place the dataset)."
            )
        return Response(body, status=200 if ready else 503)
