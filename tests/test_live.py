"""Smoke tests against the real external services.

Excluded from the normal run (``-m 'not live'``); run on demand with ``pytest -m live``.
They make a handful of polite requests (the OSRM demo server allows one per second).
"""

import time

import pytest

from fuelplanner.services.locations import resolve_location
from fuelplanner.services.routing import get_route

pytestmark = pytest.mark.live


def test_live_osrm_route_chicago_to_dallas_and_cache():
    first = get_route((41.8781, -87.6298), (32.7767, -96.7970))
    assert first.cache_status == "miss"
    assert 880 < first.route.distance_miles < 1050
    assert 12 < first.route.duration_hours < 20
    assert len(first.route.coordinates) > 1000
    assert first.route.coordinates[0][0] == pytest.approx(-87.63, abs=0.1)  # starts near Chicago
    assert get_route((41.8781, -87.6298), (32.7767, -96.7970)).cache_status == "hit"


def test_live_census_geocoder_for_a_street_address():
    time.sleep(1)
    loc = resolve_location("1600 Pennsylvania Ave NW, Washington, DC 20500")
    assert (loc.lat, loc.lon) == pytest.approx((38.8987, -77.0352), abs=0.01)
    assert loc.resolved_by == "census_geocoder"
