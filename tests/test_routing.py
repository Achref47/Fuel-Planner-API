import re
import threading

import numpy as np
import pytest
import requests
import responses

from fuelplanner.errors import (
    RouteNotFoundError,
    RoutingProviderError,
    RoutingProviderTimeoutError,
    RoutingProviderUnavailableError,
)
from fuelplanner.services import routing
from fuelplanner.services.routing import (
    OSRMProvider,
    RequestThrottle,
    get_route,
    get_routing_provider,
)

from .fake_provider import FakeProvider

OSRM_URL = re.compile(r"https://router\.project-osrm\.org/route/v1/driving/.*")
CHICAGO = (41.8781, -87.6298)
DALLAS = (32.7767, -96.7970)


def osrm_ok(distance_m=1609344.0, duration_s=36000.0, snaps=(12.0, 40.0)):
    return {
        "code": "Ok",
        "routes": [
            {
                "distance": distance_m,
                "duration": duration_s,
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-87.6298, 41.8781], [-92.0, 37.0], [-96.7970, 32.7767]],
                },
            }
        ],
        "waypoints": [{"distance": snaps[0]}, {"distance": snaps[1]}],
    }


def route_with(distance, coordinates):
    return {
        "code": "Ok",
        "routes": [{"distance": distance, "duration": 1, "geometry": {"coordinates": coordinates}}],
    }


# --- OSRM client ---------------------------------------------------------------------------------


@responses.activate
def test_osrm_request_and_parsing():
    responses.get(OSRM_URL, json=osrm_ok())
    result = OSRMProvider().route(CHICAGO, DALLAS)

    request = responses.calls[0].request
    # OSRM wants lon,lat order and full GeoJSON geometry in a single request.
    assert "/driving/-87.629800,41.878100;-96.797000,32.776700" in request.url
    assert "overview=full" in request.url and "geometries=geojson" in request.url
    assert "steps=false" in request.url
    assert request.headers["User-Agent"].startswith("spotter-fuel-route-assessment")

    assert result.distance_miles == pytest.approx(1000.0)
    assert result.duration_hours == pytest.approx(10.0)
    assert result.coordinates.shape == (3, 2)
    assert result.coordinates[0].tolist() == [-87.6298, 41.8781]  # [lon, lat]
    assert result.provider == "osrm" and "OpenStreetMap" in result.attribution
    assert result.start_snap_miles == pytest.approx(12 / 1609.344)
    assert result.end_snap_miles == pytest.approx(40 / 1609.344)


@responses.activate
@pytest.mark.parametrize("code", ["NoRoute", "NoSegment"])
@pytest.mark.parametrize("status", [200, 400])
def test_unroutable_locations(code, status):
    responses.get(OSRM_URL, status=status, json={"code": code, "message": "x"})
    with pytest.raises(RouteNotFoundError) as excinfo:
        OSRMProvider().route(CHICAGO, DALLAS)
    assert excinfo.value.http_status == 422
    assert excinfo.value.details["provider_code"] == code


@responses.activate
def test_rate_limiting_is_reported_with_retry_after():
    responses.get(OSRM_URL, status=429, headers={"Retry-After": "30"}, body="slow down")
    with pytest.raises(RoutingProviderUnavailableError) as excinfo:
        OSRMProvider().route(CHICAGO, DALLAS)
    assert excinfo.value.http_status == 503
    assert "rate limiting" in excinfo.value.message
    assert excinfo.value.details["retry_after_seconds"] == "30"


@responses.activate
@pytest.mark.parametrize("status", [502, 503, 504])
def test_upstream_outages_are_unavailable(status):
    responses.get(OSRM_URL, status=status, body="bad gateway")
    with pytest.raises(RoutingProviderUnavailableError):
        OSRMProvider().route(CHICAGO, DALLAS)


@responses.activate
def test_timeouts_and_connection_errors():
    responses.get(OSRM_URL, body=requests.Timeout("slow"))
    with pytest.raises(RoutingProviderTimeoutError) as excinfo:
        OSRMProvider().route(CHICAGO, DALLAS)
    assert excinfo.value.http_status == 504
    responses.replace(responses.GET, OSRM_URL, body=requests.ConnectionError("refused"))
    with pytest.raises(RoutingProviderUnavailableError):
        OSRMProvider().route(CHICAGO, DALLAS)


@responses.activate
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, "<html>not json</html>"),
        (500, "internal error"),
        (200, {"unexpected": "shape"}),
        (400, {"code": "InvalidQuery", "message": "bad"}),
        (200, {"code": "TooBig"}),
        (200, {"code": "Ok", "routes": []}),
        (200, route_with(1, [[1, 2]])),
        (200, route_with("x", [[1, 2], [3, 4]])),
        (200, route_with(5, [[1, 2], [3, None]])),
    ],
)
def test_malformed_or_failed_responses_are_provider_errors(status, body):
    kwargs = {"json": body} if isinstance(body, dict) else {"body": body}
    responses.get(OSRM_URL, status=status, **kwargs)
    with pytest.raises(RoutingProviderError) as excinfo:
        OSRMProvider().route(CHICAGO, DALLAS)
    assert excinfo.value.http_status == 502


def test_throttle_enforces_a_minimum_gap_between_requests():
    now = [100.0]
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    throttle = RequestThrottle(1.0, clock=lambda: now[0], sleep=fake_sleep)
    assert throttle.wait() == 0  # first request goes straight out
    now[0] += 0.25
    assert throttle.wait() == pytest.approx(0.75)
    now[0] += 5  # long idle period: no waiting
    assert throttle.wait() == 0
    assert sleeps == [pytest.approx(0.75)]
    assert RequestThrottle(0).wait() == 0


@responses.activate
def test_provider_waits_for_the_throttle_before_each_call():
    responses.get(OSRM_URL, json=osrm_ok())
    provider = OSRMProvider()
    waits = []
    provider.throttle = type("T", (), {"wait": lambda self: waits.append(1) or 0})()
    provider.route(CHICAGO, DALLAS)
    provider.route(DALLAS, CHICAGO)
    assert len(waits) == 2


def test_providers_share_a_throttle_per_server(settings):
    settings.FUEL_PLANNER = {**settings.FUEL_PLANNER, "OSRM_MIN_INTERVAL_SECONDS": 1.0}
    assert OSRMProvider().throttle is OSRMProvider().throttle


# --- cached lookups ------------------------------------------------------------------------------


@responses.activate
def test_identical_journey_is_served_from_cache_without_a_second_request():
    responses.get(OSRM_URL, json=osrm_ok())
    first = get_route(CHICAGO, DALLAS)
    second = get_route(CHICAGO, DALLAS)
    assert (first.cache_status, second.cache_status) == ("miss", "hit")
    assert len(responses.calls) == 1
    assert second.route.distance_miles == first.route.distance_miles
    assert np.array_equal(second.route.coordinates, first.route.coordinates)


@responses.activate
def test_cache_key_rounds_to_about_a_metre_and_distinguishes_direction_and_places():
    responses.get(OSRM_URL, json=osrm_ok())
    get_route(CHICAGO, DALLAS)
    assert get_route((CHICAGO[0] + 1e-7, CHICAGO[1]), DALLAS).cache_status == "hit"
    assert len(responses.calls) == 1
    assert get_route(DALLAS, CHICAGO).cache_status == "miss"  # reverse is a different journey
    assert get_route(CHICAGO, (32.80, -96.80)).cache_status == "miss"
    assert len(responses.calls) == 3


@responses.activate
def test_failures_are_not_cached():
    responses.get(OSRM_URL, status=503, body="down")
    for _ in range(2):
        with pytest.raises(RoutingProviderUnavailableError):
            get_route(CHICAGO, DALLAS)
    assert len(responses.calls) == 2
    responses.replace(responses.GET, OSRM_URL, json=osrm_ok())
    assert get_route(CHICAGO, DALLAS).cache_status == "miss"
    assert get_route(CHICAGO, DALLAS).cache_status == "hit"


def use_fake_provider(settings, delay=0.0):
    FakeProvider.calls = []
    FakeProvider.delay = delay
    settings.FUEL_PLANNER = {
        **settings.FUEL_PLANNER,
        "ROUTING_PROVIDER": "tests.fake_provider.FakeProvider",
    }


def test_routing_provider_is_replaceable_through_configuration(settings):
    use_fake_provider(settings)
    assert isinstance(get_routing_provider(), FakeProvider)
    lookup = get_route(CHICAGO, DALLAS)
    assert lookup.route.provider == "fake" and lookup.route.distance_miles == 123.4
    assert get_route(CHICAGO, DALLAS).cache_status == "hit"  # caching works for any provider
    assert len(FakeProvider.calls) == 1


def test_concurrent_identical_requests_make_one_provider_call(settings):
    use_fake_provider(settings, delay=0.2)
    statuses = []

    def worker():
        statuses.append(get_route(CHICAGO, DALLAS).cache_status)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(FakeProvider.calls) == 1
    assert sorted(statuses) == ["hit"] * 7 + ["miss"]


@responses.activate
def test_logs_do_not_contain_coordinates(caplog):
    responses.get(OSRM_URL, json=osrm_ok())
    with caplog.at_level("DEBUG", logger="fuelplanner"):
        get_route(CHICAGO, DALLAS)
        get_route(CHICAGO, DALLAS)
    assert "41.8781" not in caplog.text and "87.6298" not in caplog.text
    assert "routing ok" in caplog.text and "route cache hit" in caplog.text


def test_provider_instance_is_reused_between_requests():
    assert get_routing_provider() is get_routing_provider()
    routing.reset_routing_provider()
