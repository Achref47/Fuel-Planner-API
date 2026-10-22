"""The error format in the corners that ordinary requests do not reach."""

import json
from decimal import Decimal

import pytest
from django.test import RequestFactory
from rest_framework import exceptions as drf
from rest_framework.test import APIClient

from fuelplanner.api.exceptions import api_exception_handler, error_body, json_404, json_500
from fuelplanner.errors import (
    FuelDataUnavailableError,
    PlannerError,
    RoutingProviderUnavailableError,
)
from fuelplanner.models import Station
from fuelplanner.services.stations import reset_station_index

CONTEXT = {"view": object()}


def test_error_body_shape():
    assert error_body("x", "msg") == {"error": {"code": "x", "message": "msg", "details": {}}}
    assert error_body("x", "m", {"a": 1})["error"]["details"] == {"a": 1}


def test_planner_errors_keep_their_status_code_message_and_details():
    err = FuelDataUnavailableError("no data", {"hint": "load it"})
    response = api_exception_handler(err, CONTEXT)
    assert response.status_code == 503
    assert response.data == error_body("fuel_data_unavailable", "no data", {"hint": "load it"})


def test_retry_after_header_only_for_numeric_seconds():
    numeric = RoutingProviderUnavailableError("busy", {"retry_after_seconds": "12"})
    assert api_exception_handler(numeric, CONTEXT)["Retry-After"] == "12"
    http_date = RoutingProviderUnavailableError(
        "busy", {"retry_after_seconds": "Wed, 21 Oct 2026 07:28:00 GMT"}
    )
    response = api_exception_handler(http_date, CONTEXT)
    assert not response.has_header("Retry-After")  # never emit a malformed header
    assert response.data["error"]["details"]["retry_after_seconds"].startswith("Wed")


def test_plain_validation_errors_become_non_field_errors():
    response = api_exception_handler(drf.ValidationError(["Something is off."]), CONTEXT)
    assert response.status_code == 400
    assert response.data["error"]["details"] == {"non_field_errors": ["Something is off."]}


def test_nested_validation_errors_are_plain_json_types():
    exc = drf.ValidationError({"options": {"geometry": [drf.ErrorDetail("Bad.", code="x")]}})
    response = api_exception_handler(exc, CONTEXT)
    assert response.data["error"]["details"] == {"options": {"geometry": ["Bad."]}}
    json.dumps(response.data)  # must be serialisable without DRF types


def test_throttled_without_a_wait_has_no_retry_details():
    response = api_exception_handler(drf.Throttled(), CONTEXT)
    assert response.status_code == 429
    assert response.data["error"]["code"] == "rate_limited"
    assert response.data["error"]["details"] == {}


def test_unmapped_http_errors_get_a_generic_code():
    response = api_exception_handler(drf.PermissionDenied("nope"), CONTEXT)
    assert response.status_code == 403
    assert response.data["error"]["code"] == "http_403"


def test_unexpected_exceptions_become_500_without_details(caplog):
    with caplog.at_level("ERROR"):
        response = api_exception_handler(RuntimeError("top secret"), CONTEXT)
    assert response.status_code == 500
    assert response.data["error"]["code"] == "internal_error"
    assert "top secret" not in json.dumps(response.data)
    assert "top secret" in caplog.text  # ...but it is logged for operators


def test_django_level_404_and_500_handlers_use_the_same_format():
    request = RequestFactory().get("/missing")
    not_found, server_error = json_404(request), json_500(request)
    assert (not_found.status_code, server_error.status_code) == (404, 500)
    assert json.loads(not_found.content)["error"]["code"] == "not_found"
    assert json.loads(server_error.content)["error"]["code"] == "internal_error"


def test_every_planner_error_is_a_documented_http_error():
    from fuelplanner import errors

    seen = {}
    for name in dir(errors):
        cls = getattr(errors, name)
        if isinstance(cls, type) and issubclass(cls, PlannerError) and cls is not PlannerError:
            assert cls.code and cls.code != PlannerError.code, name
            assert 400 <= cls.http_status < 600, name
            assert cls.code not in seen, f"duplicate code {cls.code}"
            seen[cls.code] = cls.http_status
    assert seen["no_feasible_fuel_plan"] == 422 and seen["routing_provider_timeout"] == 504
    assert len(seen) >= 12


def test_unsupported_accept_header_is_a_json_406():
    response = APIClient().post(
        "/api/v1/fuel-route/", {}, format="json", HTTP_ACCEPT="application/xml"
    )
    assert response.status_code == 406
    assert json.loads(response.content)["error"]["code"] == "not_acceptable"


@pytest.mark.django_db
def test_health_survives_a_missing_place_table(settings, tmp_path):
    Station.objects.create(
        opis_id=1, name="S", address="A", city="C", state="TX", price=Decimal("3"),
        latitude=32.0, longitude=-97.0, location_source="place",
    )  # fmt: skip
    reset_station_index()
    settings.FUEL_PLANNER = {**settings.FUEL_PLANNER, "GAZETTEER_PATH": tmp_path / "gone.csv.gz"}
    response = APIClient().get("/api/v1/health/")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok" and body["gazetteer_places"] == 0 and body["stations_loaded"] == 1
