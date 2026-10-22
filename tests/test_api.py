"""End-to-end API tests: real views, serializers, planner and optimiser; HTTP mocked."""

import time
from decimal import Decimal

import numpy as np
import pytest
import requests
from rest_framework.throttling import AnonRateThrottle

from fuelplanner.models import Station

from .helpers import (
    CENSUS_URL,
    CHICAGO,
    DALLAS,
    HOUSTON,
    OSRM_URL,
    URL,
    add_station,
    error,
    mock_route,
    post,
    seed_chicago_dallas,
)

pytestmark = pytest.mark.django_db


# --- the happy path ----------------------------------------------------------------------------


def test_long_journey_returns_cheapest_stops_with_full_accounting(client, http):
    coords = mock_route(http)
    seed_chicago_dallas(coords)
    response = post(client)
    assert response.status_code == 200, response.content
    data = response.json()

    # B then D: arrive at B with 112 mi left, buy 27.6 gal to reach D empty, then 19.4 gal.
    stops = data["fuel_stops"]
    assert [s["station"]["opis_id"] for s in stops] == [2, 4]
    assert [s["sequence"] for s in stops] == [1, 2]
    assert stops[0]["gallons_purchased"] == pytest.approx(27.6, abs=0.1)
    assert stops[1]["gallons_purchased"] == pytest.approx(19.4, abs=0.1)
    assert data["summary"]["total_fuel_cost"] == pytest.approx(139.06, abs=0.6)
    assert data["summary"]["fuel_stops"] == 2
    assert data["summary"]["currency"] == "USD"

    # Money: each line is gallons x price to the cent, and the total is the sum of the lines.
    assert sum(s["cost"] for s in stops) == pytest.approx(data["summary"]["total_fuel_cost"])
    for stop in stops:
        assert stop["cost"] == pytest.approx(
            stop["gallons_purchased"] * stop["price_per_gallon"], abs=0.06
        )

    # Fuel: initial + purchased - used = remaining (within display rounding).
    s = data["summary"]
    balance = (
        s["initial_fuel_gallons"]
        + s["fuel_purchased_gallons"]
        - s["fuel_used_gallons"]
        - s["fuel_remaining_at_destination_gallons"]
    )
    assert balance == pytest.approx(0, abs=0.03)
    assert s["fuel_used_gallons"] == pytest.approx(s["driving_distance_miles"] / 10, abs=0.1)

    # Legs: start -> stop 1 -> stop 2 -> finish, none longer than the 500-mile range.
    legs = data["legs"]
    assert [(leg["from"], leg["to"]) for leg in legs] == [
        ("start", "stop 1"),
        ("stop 1", "stop 2"),
        ("stop 2", "finish"),
    ]
    assert all(leg["distance_miles"] <= 500 for leg in legs)
    assert sum(leg["distance_miles"] for leg in legs) == pytest.approx(
        s["driving_distance_miles"], abs=0.3
    )
    assert stops[1]["distance_from_previous_miles"] == legs[1]["distance_miles"]

    # Stops are in route order with positions along the route.
    assert stops[0]["route_mile"] < stops[1]["route_mile"] <= data["route"]["distance_miles"]
    assert stops[0]["fuel_on_arrival_gallons"] == pytest.approx(11.2, abs=0.1)

    # Station and location blocks.
    station = stops[0]["station"]
    assert station["location_precision"] == "city" and station["approximate_location"] is True
    assert set(station) >= {"opis_id", "name", "address", "city", "state", "lat", "lon"}
    assert data["start"]["label"] == "Chicago, IL" and data["start"]["precision"] == "city"
    assert data["finish"]["resolved_by"] == "gazetteer_city"
    assert data["vehicle"] == {
        "max_range_miles": 500.0,
        "miles_per_gallon": 10.0,
        "tank_gallons": 50.0,
        "initial_fuel_gallons": 50.0,
    }


def test_route_geometry_is_geojson_lon_lat_and_simplified_by_default(client, http):
    coords = mock_route(http, n=2000)
    seed_chicago_dallas(coords)
    route = post(client).json()["route"]
    assert route["distance_miles"] == 970.0
    assert route["geometry"]["type"] == "LineString"
    points = route["geometry"]["coordinates"]
    assert route["geometry_points"] == len(points) < 2000
    assert points[0] == pytest.approx(list(CHICAGO), abs=1e-4)  # [lon, lat]
    assert points[-1] == pytest.approx(list(DALLAS), abs=1e-4)
    assert route["geometry_mode"] == "simplified"


@pytest.mark.parametrize(("mode", "points"), [("full", 400), ("none", 0)])
def test_geometry_modes(client, http, mode, points):
    coords = mock_route(http)
    seed_chicago_dallas(coords)
    route = post(client, options={"geometry": mode}).json()["route"]
    assert route["geometry_points"] == points
    assert (route["geometry"] is None) == (mode == "none")


def test_short_journey_needs_no_stops_and_costs_nothing(client, http):
    mock_route(http, DALLAS, HOUSTON, distance=240.0)
    data = post(client, "Dallas, TX", "Houston, TX").json()
    assert data["fuel_stops"] == []
    assert data["summary"]["total_fuel_cost"] == 0.0
    assert data["summary"]["fuel_stops"] == 0
    assert [leg["to"] for leg in data["legs"]] == ["finish"]
    assert data["summary"]["fuel_used_gallons"] == pytest.approx(24.0)
    assert data["summary"]["fuel_remaining_at_destination_gallons"] == pytest.approx(26.0)
    assert "$0.00" in " ".join(data["assumptions"])


def test_no_stations_are_needed_to_answer_a_short_trip(client, http):
    mock_route(http, DALLAS, HOUSTON, distance=240.0)
    assert Station.objects.count() == 0
    assert post(client, "Dallas, TX", "Houston, TX").status_code == 200


def test_identical_request_is_served_from_cache_with_no_external_calls(client, http):
    coords = mock_route(http)
    seed_chicago_dallas(coords)
    first = post(client).json()
    second = post(client).json()
    assert first["meta"]["routing_cache"] == "miss"
    assert first["meta"]["external_calls"] == {"routing": 1, "geocoding": 0}
    assert second["meta"]["routing_cache"] == "hit"
    assert second["meta"]["external_calls"] == {"routing": 0, "geocoding": 0}
    assert len(http.calls) == 1
    assert second["fuel_stops"] == first["fuel_stops"]
    assert second["summary"] == first["summary"]


def test_attribution_and_assumptions_are_present(client, http):
    coords = mock_route(http)
    seed_chicago_dallas(coords)
    data = post(client).json()
    assert "OpenStreetMap" in data["meta"]["attribution"] and "OSRM" in data["meta"]["attribution"]
    text = " ".join(data["assumptions"])
    assert "starts with 50" in text and "centroid" in text and "5 miles" in text
    assert "city of 'Chicago, IL'" in text  # start/finish precision is disclosed


def test_street_address_costs_one_geocoding_call(client, http):
    coords = mock_route(http)
    seed_chicago_dallas(coords)
    http.get(
        CENSUS_URL,
        json={
            "result": {
                "addressMatches": [
                    {
                        "matchedAddress": "233 S WACKER DR, CHICAGO, IL, 60606",
                        "coordinates": {"x": CHICAGO[0], "y": CHICAGO[1]},
                    }
                ]
            }
        },
    )
    data = post(client, start="233 S Wacker Dr, Chicago, IL 60606").json()
    assert data["start"]["resolved_by"] == "census_geocoder"
    assert data["start"]["precision"] == "address"
    assert data["meta"]["external_calls"] == {"routing": 1, "geocoding": 1}
    again = post(client, start="233 S Wacker Dr, Chicago, IL 60606").json()
    assert again["meta"]["external_calls"] == {"routing": 0, "geocoding": 0}


def test_zip_and_coordinate_inputs(client, http):
    mock_route(http, CHICAGO, (-87.90, 41.98), distance=24.0)
    response = post(client, "60601", {"lat": 41.98, "lon": -87.90})
    assert response.status_code == 200, response.content
    data = response.json()
    assert (data["start"]["resolved_by"], data["start"]["precision"]) == (
        "gazetteer_zip",
        "zip_centroid",
    )
    assert data["start"]["label"] == "ZIP 60601"
    assert (data["finish"]["resolved_by"], data["finish"]["precision"]) == ("coordinates", "exact")
    assert data["meta"]["external_calls"] == {"routing": 1, "geocoding": 0}
    string_form = post(client, "41.8850,-87.6220", "41.98 -87.90")
    assert string_form.status_code == 200
    assert string_form.json()["start"]["resolved_by"] == "coordinates"


# --- options ------------------------------------------------------------------------------------


def test_detour_option_controls_which_stations_are_considered(client, http):
    coords = mock_route(http)
    add_station(1, coords, 970.0, 388, "3.60")  # on the route, pricey
    add_station(2, coords, 970.0, 388, "2.50", off=8.0)  # cheap but 8 miles off the route
    add_station(3, coords, 970.0, 776, "3.60")
    narrow = post(client, options={"max_detour_miles": 5}).json()
    wide = post(client, options={"max_detour_miles": 10}).json()
    assert narrow["meta"]["candidate_stations"] == 2
    assert wide["meta"]["candidate_stations"] == 3
    assert 2 not in [s["station"]["opis_id"] for s in narrow["fuel_stops"]]
    assert 2 in [s["station"]["opis_id"] for s in wide["fuel_stops"]]
    assert wide["summary"]["total_fuel_cost"] < narrow["summary"]["total_fuel_cost"]
    assert wide["summary"]["detour_miles"] > 0 == narrow["summary"]["detour_miles"]
    cheap = next(s for s in wide["fuel_stops"] if s["station"]["opis_id"] == 2)
    assert cheap["off_route_miles"] == pytest.approx(8.0, abs=0.3)
    assert cheap["detour_miles"] == pytest.approx(16.0, abs=0.6)


def test_initial_fuel_fraction_changes_the_plan(client, http):
    coords = mock_route(http)
    seed_chicago_dallas(coords)
    full = post(client).json()
    half = post(client, options={"initial_fuel_fraction": 0.5}).json()
    assert half["vehicle"]["initial_fuel_gallons"] == 25.0
    assert half["summary"]["fuel_purchased_gallons"] > full["summary"]["fuel_purchased_gallons"]
    assert half["summary"]["total_fuel_cost"] > full["summary"]["total_fuel_cost"]


# --- GeoJSON --------------------------------------------------------------------------------


def test_geojson_output(client, http):
    coords = mock_route(http)
    seed_chicago_dallas(coords)
    response = post(client, query="?format=geojson")
    assert response.status_code == 200
    data = response.json()
    assert data["type"] == "FeatureCollection"
    kinds = [f["properties"]["kind"] for f in data["features"]]
    assert kinds == ["route", "start", "finish", "fuel_stop", "fuel_stop"]
    route = data["features"][0]
    assert route["geometry"]["type"] == "LineString"
    stop = data["features"][3]
    assert stop["geometry"]["type"] == "Point" and stop["properties"]["sequence"] == 1
    lon, lat = stop["geometry"]["coordinates"]
    assert -100 < lon < -85 and 30 < lat < 45  # [lon, lat] order
    assert data["summary"]["total_fuel_cost"] > 0
    assert "OpenStreetMap" in data["attribution"]


def test_geojson_without_geometry_omits_the_route_feature(client, http):
    coords = mock_route(http)
    seed_chicago_dallas(coords)
    data = post(client, options={"geometry": "none"}, query="?format=geojson").json()
    assert "route" not in [f["properties"]["kind"] for f in data["features"]]


# --- errors: planning --------------------------------------------------------------------------


def test_infeasible_journey_explains_where_the_gap_is(client, http):
    coords = mock_route(http)
    add_station(1, coords, 970.0, 100, "3.00")  # nothing for the last 870 miles
    response = post(client)
    assert response.status_code == 422
    err = error(response)
    assert err["code"] == "no_feasible_fuel_plan"
    d = err["details"]
    assert d["last_reachable_route_mile"] == pytest.approx(100, abs=0.5)
    assert d["next_option_is_destination"] is True
    assert d["shortfall_miles"] == pytest.approx(370, abs=1.5)
    assert d["gap_start"]["route_mile"] == d["last_reachable_route_mile"]
    assert -100 < d["gap_start"]["lon"] < -85 and 30 < d["gap_start"]["lat"] < 45
    assert d["gap_end"]["lat"] == pytest.approx(CHICAGO[1] + (DALLAS[1] - CHICAGO[1]), abs=0.01)
    assert "route mile 100" in err["message"] and "500-mile" in err["message"]


def test_no_fuel_data_loaded(client, http):
    mock_route(http)
    response = post(client)
    assert response.status_code == 503
    err = error(response)
    assert err["code"] == "fuel_data_unavailable" and "load_fuel_data" in err["message"]


def test_same_start_and_finish(client, http):
    response = post(client, "Dallas, TX", "dallas,tx")
    assert (response.status_code, error(response)["code"]) == (400, "same_location")
    assert len(http.calls) == 0  # rejected before any routing request


@pytest.mark.parametrize(
    ("start", "finish", "code"),
    [
        ("Atlantis, TX", "Dallas, TX", "location_not_found"),
        ("Dallas, TX", "banana", "location_not_found"),
        ("Dallas, TX", "99999", "location_not_found"),
        ("Dallas, TX", "Anchorage, AK", "location_outside_usa"),
        ({"lat": 48.85, "lon": 2.35}, "Dallas, TX", "location_outside_usa"),
    ],
)
def test_location_errors_are_422_and_never_reach_the_router(client, http, start, finish, code):
    response = post(client, start, finish)
    assert response.status_code == 422
    assert error(response)["code"] == code
    assert len(http.calls) == 0


# --- errors: upstream ----------------------------------------------------------------------------


# A trip within one tank as the crow flies: the router is reached even with no station data.
SHORT = {"start": "Dallas, TX", "finish": "Houston, TX"}


def test_unroutable_pair(client, http):
    http.get(OSRM_URL, status=400, json={"code": "NoRoute", "message": "none"})
    response = post(client, **SHORT)
    assert (response.status_code, error(response)["code"]) == (422, "route_not_found")


def test_router_rate_limit_is_503_with_retry_after_header(client, http):
    http.get(OSRM_URL, status=429, headers={"Retry-After": "7"})
    response = post(client, **SHORT)
    assert response.status_code == 503
    assert error(response)["code"] == "routing_provider_unavailable"
    assert response["Retry-After"] == "7"


@pytest.mark.parametrize(
    ("kwargs", "status", "code"),
    [
        ({"body": requests.Timeout("slow")}, 504, "routing_provider_timeout"),
        ({"body": requests.ConnectionError("down")}, 503, "routing_provider_unavailable"),
        ({"status": 500, "body": "boom"}, 502, "routing_provider_error"),
        ({"status": 200, "body": "<html>"}, 502, "routing_provider_error"),
    ],
)
def test_router_failures_map_to_distinct_errors(client, http, kwargs, status, code):
    http.get(OSRM_URL, **kwargs)
    response = post(client, **SHORT)
    assert (response.status_code, error(response)["code"]) == (status, code)


def test_geocoder_failure_is_502(client, http):
    http.get(CENSUS_URL, status=500, body="down")
    response = post(client, start="1 Main St, Springfield, IL")
    assert (response.status_code, error(response)["code"]) == (502, "geocoding_provider_error")


# --- errors: validation ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({}, "start"),
        ({"start": "Dallas, TX"}, "finish"),
        ({"start": "", "finish": "Dallas, TX"}, "start"),
        ({"start": "   ", "finish": "Dallas, TX"}, "start"),
        ({"start": "x" * 201, "finish": "Dallas, TX"}, "start"),
        ({"start": 42, "finish": "Dallas, TX"}, "start"),
        ({"start": ["Dallas", "TX"], "finish": "Dallas, TX"}, "start"),
        ({"start": {"lat": 10}, "finish": "Dallas, TX"}, "start"),
        ({"start": {"lat": "abc", "lon": 1}, "finish": "Dallas, TX"}, "start"),
        ({"start": {"lat": 95, "lon": 1}, "finish": "Dallas, TX"}, "start"),
        ({"start": {"lat": 1, "lon": 1, "extra": 2}, "finish": "Dallas, TX"}, "start"),
        ({"start": "Dallas, TX", "finish": "Houston, TX", "bogus": 1}, "bogus"),
        (
            {"start": "Dallas, TX", "finish": "Houston, TX", "options": {"max_detour": 5}},
            "options",
        ),
        (
            {"start": "Dallas, TX", "finish": "Houston, TX", "options": {"max_detour_miles": 0.1}},
            "options",
        ),
        (
            {"start": "Dallas, TX", "finish": "Houston, TX", "options": {"max_detour_miles": 99}},
            "options",
        ),
        (
            {"start": "Dallas, TX", "finish": "Houston, TX", "options": {"max_detour_miles": "x"}},
            "options",
        ),
        (
            {
                "start": "Dallas, TX",
                "finish": "Houston, TX",
                "options": {"initial_fuel_fraction": 1.5},
            },
            "options",
        ),
        (
            {
                "start": "Dallas, TX",
                "finish": "Houston, TX",
                "options": {"initial_fuel_fraction": -1},
            },
            "options",
        ),
        (
            {"start": "Dallas, TX", "finish": "Houston, TX", "options": {"geometry": "huge"}},
            "options",
        ),
        ({"start": "Dallas, TX", "finish": "Houston, TX", "options": "fast"}, "options"),
    ],
)
def test_invalid_requests_are_400_with_field_details(client, http, body, field):
    response = client.post(URL, body, format="json")
    assert response.status_code == 400
    err = error(response)
    assert err["code"] == "validation_error"
    assert field in err["details"]
    assert len(http.calls) == 0


def test_malformed_json_body(client):
    response = client.post(URL, "{not json", content_type="application/json")
    assert (response.status_code, error(response)["code"]) == (400, "invalid_json")


def test_json_array_body(client):
    response = client.post(URL, [1, 2], format="json")
    assert response.status_code == 400
    assert error(response)["code"] == "validation_error"


def test_unknown_output_format(client):
    response = post(client, query="?format=xml")
    assert response.status_code == 400 and "format" in error(response)["details"]


def test_wrong_method_and_media_type_and_unknown_path(client):
    response = client.get(URL)
    assert (response.status_code, error(response)["code"]) == (405, "method_not_allowed")
    assert "POST" in response["Allow"]
    response = client.post(
        URL, "start=a&finish=b", content_type="application/x-www-form-urlencoded"
    )
    assert (response.status_code, error(response)["code"]) == (415, "unsupported_media_type")
    response = client.get("/api/v1/nope/")
    assert (response.status_code, error(response)["code"]) == (404, "not_found")


def test_rate_limiting(client, monkeypatch):
    monkeypatch.setattr(AnonRateThrottle, "THROTTLE_RATES", {"anon": "2/min"})
    codes = [post(client, "", "").status_code for _ in range(3)]
    assert codes == [400, 400, 429]
    response = post(client, "", "")
    err = error(response)
    assert err["code"] == "rate_limited" and err["details"]["retry_after_seconds"] >= 1
    assert int(response["Retry-After"]) >= 1


def test_unexpected_exceptions_do_not_leak_internals(client, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail /srv/app/db")

    monkeypatch.setattr("fuelplanner.api.views.plan_trip", boom)
    response = post(client)
    assert response.status_code == 500
    assert error(response)["code"] == "internal_error"
    assert "secret" not in response.content.decode()


# --- health -------------------------------------------------------------------------------------


def test_health_reports_readiness(client):
    response = client.get("/api/v1/health/")
    assert response.status_code == 503
    assert (
        response.json()["status"] == "unavailable"
        and "load_fuel_data" in response.json()["message"]
    )

    Station.objects.create(
        opis_id=1,
        name="S",
        address="A",
        city="C",
        state="TX",
        price=Decimal("3"),
        latitude=32.0,
        longitude=-97.0,
        location_source="place",
    )
    from fuelplanner.services.stations import reset_station_index

    reset_station_index()
    response = client.get("/api/v1/health/")
    body = response.json()
    assert response.status_code == 200 and body["status"] == "ok"
    assert body["stations_loaded"] == 1 and body["routing_provider"] == "osrm"
    assert body["gazetteer_places"] > 5


# --- performance ---------------------------------------------------------------------------------


def test_coast_to_coast_sized_route_is_fast_and_cached_repeat_is_faster(client, http):
    # Geometry is Chicago -> Dallas (known to the fixture gazetteer); the reported distance
    # stretches it to coast-to-coast length, as the planner rescales mileage to the provider's.
    coords = mock_route(http, CHICAGO, DALLAS, distance=2794.0, n=35_000)
    rng = np.random.default_rng(1)
    for i in range(1, 400):  # a dense national station set
        mile = float(rng.uniform(0, 2794))
        add_station(
            i,
            coords,
            2794.0,
            mile,
            f"{rng.uniform(2.8, 4.2):.3f}",
            off=float(rng.choice([0, 1, 3])),
        )
    start = time.perf_counter()
    first = post(
        client, {"lat": CHICAGO[1], "lon": CHICAGO[0]}, {"lat": DALLAS[1], "lon": DALLAS[0]}
    )
    miss_seconds = time.perf_counter() - start
    assert first.status_code == 200, first.content
    start = time.perf_counter()
    second = post(
        client, {"lat": CHICAGO[1], "lon": CHICAGO[0]}, {"lat": DALLAS[1], "lon": DALLAS[0]}
    )
    hit_seconds = time.perf_counter() - start

    data = first.json()
    assert data["summary"]["fuel_stops"] >= 5
    assert data["meta"]["external_calls"]["routing"] == 1
    assert second.json()["meta"]["external_calls"]["routing"] == 0
    assert miss_seconds < 3.0 and hit_seconds < 2.0
    assert data["route"]["geometry_points"] < 35_000
    # Display rounding, not drift: every stop reconciles to the cent.
    assert sum(s["cost"] for s in data["fuel_stops"]) == pytest.approx(
        data["summary"]["total_fuel_cost"], abs=0.005
    )


def test_urls_work_with_and_without_the_trailing_slash(client, http):
    mock_route(http, DALLAS, HOUSTON, distance=240.0)
    body = {"start": "Dallas, TX", "finish": "Houston, TX"}
    for url in ("/api/v1/fuel-route/", "/api/v1/fuel-route"):
        response = client.post(url, body, format="json")
        assert response.status_code == 200, url
    for url in ("/api/v1/health/", "/api/v1/health"):
        assert client.get(url).status_code in (200, 503), url  # a real answer, never a redirect
    assert client.get("/api/v1/fuel-route/extra").status_code == 404
    assert client.get("/api/v1/fuel-routes").status_code == 404


BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,*/*;q=0.8"


def test_opening_an_endpoint_in_a_browser_returns_json_not_a_template_error(client, settings):
    """Regression: with DEBUG on, DRF's HTML renderer crashed with TemplateDoesNotExist."""
    settings.DEBUG = True
    response = client.get("/api/v1/health/", HTTP_ACCEPT=BROWSER_ACCEPT)
    assert response.status_code in (200, 503)
    assert response["Content-Type"].startswith("application/json")
    assert "status" in response.json()
    assert client.get("/api/v1/fuel-route/", HTTP_ACCEPT=BROWSER_ACCEPT).status_code == 405


def test_the_renderer_list_is_json_only_whatever_the_debug_flag():
    from django.conf import settings as s

    assert s.REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] == ["rest_framework.renderers.JSONRenderer"]
