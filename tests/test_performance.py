"""Speed, size and concurrency budgets.

Thresholds are deliberately generous (typically 5-10x the numbers measured on a laptop) so the
tests are stable on slow machines yet still catch an accidental order-of-magnitude regression.
Run with ``-s`` to see the measured numbers.
"""

import pickle
import random
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from django.conf import settings as django_settings
from rest_framework.test import APIClient

from fuelplanner.models import Station
from fuelplanner.services.gazetteer import Gazetteer
from fuelplanner.services.geometry import resample_route
from fuelplanner.services.locations import resolve_location
from fuelplanner.services.routing import RouteResult
from fuelplanner.services.stations import load_station_index, reset_station_index
from fuelplanner.services.warmup import warm_up

from .fake_provider import LineProvider
from .helpers import CHICAGO, DALLAS, URL, mock_route, point_beside_route, post, station_object
from .invariants import check_success

pytestmark = pytest.mark.django_db
SHIPPED_GAZETTEER = Path(django_settings.BASE_DIR) / "data" / "us_places.csv.gz"


def timed(function, repeat=3):
    """Best-of-N wall time in seconds (best-of damps scheduler noise)."""
    best = float("inf")
    for _ in range(repeat):
        started = time.perf_counter()
        function()
        best = min(best, time.perf_counter() - started)
    return best


def random_us_stations(count, seed=0):
    rng = random.Random(seed)
    return [
        station_object(
            i, rng.uniform(26, 48.5), rng.uniform(-123, -68), f"{rng.uniform(2.7, 4.6):.3f}"
        )
        for i in range(1, count + 1)
    ]


def stations_along(coords, distance, miles):
    path = resample_route(coords, 0.5, total_miles=distance)
    return [
        station_object(i, *point_beside_route(path, mile), "3.20")
        for i, mile in enumerate(miles, start=1)
    ]


# --- start-up -------------------------------------------------------------------------------------


@pytest.mark.skipif(not SHIPPED_GAZETTEER.exists(), reason="shipped gazetteer not present")
def test_loading_the_whole_place_table_takes_about_a_second():
    seconds = timed(lambda: Gazetteer.from_file(SHIPPED_GAZETTEER), repeat=2)
    print(f"\nplace table load (265k rows): {seconds * 1000:.0f} ms")
    assert seconds < 4.0


def test_loading_the_station_index_for_the_real_dataset_size_is_fast():
    Station.objects.bulk_create(random_us_stations(6700), batch_size=500)

    def load():
        reset_station_index()
        load_station_index()

    seconds = timed(load)
    print(f"\nstation index load (6,700 stations): {seconds * 1000:.0f} ms")
    assert seconds < 1.5


def test_warm_up_is_a_one_off_cost_of_a_few_seconds_at_most():
    Station.objects.bulk_create(random_us_stations(500), batch_size=500)
    started = time.perf_counter()
    loaded = warm_up()
    assert loaded["stations_loaded"] == 500 and time.perf_counter() - started < 6.0


def test_resolving_a_city_after_start_up_takes_microseconds():
    resolve_location("Chicago, IL")  # loads the (fixture) place table
    seconds = timed(lambda: [resolve_location("Dallas, TX") for _ in range(1000)])
    print(f"\n1,000 city look-ups: {seconds * 1000:.1f} ms")
    assert seconds < 0.5


# --- per request --------------------------------------------------------------------------------


def test_a_cached_journey_is_answered_quickly_end_to_end(client, http):
    coords = mock_route(http, CHICAGO, DALLAS, distance=970.0, n=9000)
    rng = random.Random(3)
    Station.objects.bulk_create(
        stations_along(coords, 970.0, sorted(rng.uniform(0, 970) for _ in range(120)))
    )
    reset_station_index()
    post(client)  # prime every cache
    seconds = timed(lambda: post(client), repeat=5)
    print(f"\ncached Chicago->Dallas request, end to end through DRF: {seconds * 1000:.0f} ms")
    assert seconds < 0.5


def test_response_sizes_stay_small_in_the_default_mode(client, http):
    coords = mock_route(http, CHICAGO, DALLAS, distance=970.0, n=12_000)
    Station.objects.bulk_create(stations_along(coords, 970.0, range(60, 970, 60)))
    sizes = {
        mode: len(post(client, options={"geometry": mode}).content)
        for mode in ("simplified", "full", "none")
    }
    print(f"\nresponse sizes (bytes): {sizes}")
    assert sizes["none"] < 8_000
    assert sizes["simplified"] < 100_000 and sizes["full"] > sizes["simplified"] * 3


def test_a_cached_route_is_a_modest_amount_of_memory_and_the_cache_is_bounded():
    coords = np.column_stack((np.linspace(-118, -74, 35_000), np.linspace(34, 40.7, 35_000)))
    size = len(pickle.dumps(RouteResult(2794.0, 50.0, coords, "osrm", "x")))
    print(f"\npickled 35,000-point route: {size / 1024:.0f} KB")
    assert size < 700 * 1024
    assert django_settings.CACHES["default"]["OPTIONS"]["MAX_ENTRIES"] <= 300


# --- concurrency --------------------------------------------------------------------------------


def test_a_burst_of_identical_requests_makes_one_routing_call_and_reports_it_truthfully(settings):
    """Eight clients ask for the same new journey at once: one router call, eight answers."""
    settings.FUEL_PLANNER = {
        **settings.FUEL_PLANNER,
        "ROUTING_PROVIDER": "tests.fake_provider.LineProvider",
    }
    start, finish = resolve_location("Chicago, IL"), resolve_location("Dallas, TX")
    route = LineProvider({}).route((start.lat, start.lon), (finish.lat, finish.lon))
    Station.objects.bulk_create(
        stations_along(
            route.coordinates, route.distance_miles, range(80, int(route.distance_miles), 80)
        )
    )
    reset_station_index()
    load_station_index()  # prime in this thread: worker threads must not touch the database
    LineProvider.calls, LineProvider.delay = [], 0.3
    results: list[dict] = []

    def worker():
        response = APIClient().post(
            URL, {"start": "Chicago, IL", "finish": "Dallas, TX"}, format="json"
        )
        results.append({"status": response.status_code, **response.json()})

    threads = [threading.Thread(target=worker) for _ in range(8)]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - started
    LineProvider.delay = 0.0

    assert len(LineProvider.calls) == 1  # the router was asked exactly once
    assert [r["status"] for r in results] == [200] * 8
    metas = [r["meta"] for r in results]
    assert sum(m["external_calls"]["routing"] for m in metas) == 1
    assert sorted(m["routing_cache"] for m in metas) == ["hit"] * 7 + ["miss"]
    assert len({r["summary"]["total_fuel_cost"] for r in results}) == 1  # identical answers
    assert elapsed < 3.0  # not eight queued 0.3 s router calls plus overhead


def test_the_api_is_safe_under_parallel_load(settings):
    """Many threads, mixed journeys: every response is well-formed and nothing crashes."""
    settings.FUEL_PLANNER = {
        **settings.FUEL_PLANNER,
        "ROUTING_PROVIDER": "tests.fake_provider.LineProvider",
    }
    Station.objects.bulk_create(random_us_stations(400, seed=9))
    reset_station_index()
    load_station_index()
    pairs = [
        ("Chicago, IL", "Dallas, TX"),
        ("Dallas, TX", "Houston, TX"),
        ("Houston, TX", "Chicago, IL"),
    ]
    statuses: list[int] = []
    failures: list[str] = []

    def worker(index):
        client = APIClient()
        for k in range(10):
            start, finish = pairs[(index + k) % len(pairs)]
            response = client.post(URL, {"start": start, "finish": finish}, format="json")
            body = response.json()
            if ("error" in body) == ("summary" in body):  # exactly one of the two shapes
                failures.append(f"{start}->{finish}: {body}")
            statuses.append(response.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not failures
    assert len(statuses) == 60 and set(statuses) <= {200, 422}


def test_responses_stay_correct_when_many_threads_use_the_same_cached_route(settings):
    settings.FUEL_PLANNER = {
        **settings.FUEL_PLANNER,
        "ROUTING_PROVIDER": "tests.fake_provider.LineProvider",
    }
    start, finish = resolve_location("Dallas, TX"), resolve_location("Chicago, IL")
    route = LineProvider({}).route((start.lat, start.lon), (finish.lat, finish.lon))
    Station.objects.bulk_create(
        stations_along(
            route.coordinates, route.distance_miles, range(70, int(route.distance_miles), 70)
        )
    )
    reset_station_index()
    index = load_station_index()
    by_id = {s.opis_id: s for s in index.stations}
    bodies: list[dict] = []

    def worker():
        for _ in range(5):
            response = APIClient().post(
                URL, {"start": "Dallas, TX", "finish": "Chicago, IL"}, format="json"
            )
            bodies.append(response.json())

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(bodies) == 30
    for body in bodies:
        check_success(body, by_id, 5, 1.0)
    assert len({tuple(s["station"]["opis_id"] for s in b["fuel_stops"]) for b in bodies}) == 1
