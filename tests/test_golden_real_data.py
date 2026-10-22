"""Whole-pipeline regression tests on the real assessment dataset, fully offline.

OSRM responses for four journeys were recorded once (``tests/fixtures/osrm``; routing by OSRM,
map data (c) OpenStreetMap contributors, ODbL) so the tests need no network. The real CSV is
not part of the repository, so these tests are skipped when it is absent. Exact expectations
(stops, dollars) are only asserted when the CSV is byte-for-byte the one they were recorded
against; with any other CSV the tests still check every invariant.
"""

import hashlib
import json
from pathlib import Path

import pytest
from django.conf import settings as django_settings

from fuelplanner.importer import clean_and_geocode, read_rows
from fuelplanner.services.gazetteer import Gazetteer, get_gazetteer
from fuelplanner.services.stations import StationIndex, StationRecord

from .helpers import OSRM_URL, error, post
from .invariants import check_success

CSV = Path(django_settings.FUEL_PLANNER["FUEL_CSV_PATH"])
SHIPPED_GAZETTEER = Path(django_settings.BASE_DIR) / "data" / "us_places.csv.gz"
OSRM_FIXTURES = Path(__file__).parent / "fixtures" / "osrm"
RECORDED_AGAINST_SHA256 = "c704371f141ded9c54df6c32d488a0ba2ceb589f88c936c967daa5330e0cd241"

pytestmark = pytest.mark.skipif(
    not CSV.exists() or not SHIPPED_GAZETTEER.exists(),
    reason="the assessment CSV (data/fuel-prices-for-be-assessment.csv) is not available",
)


def warm_up_gazetteer():
    get_gazetteer().nearest_place_miles(39.0, -98.0)


def stop_fingerprint(stops) -> str:
    """Identify the chosen stations without writing dataset identifiers into the repository.

    The CSV is not redistributed, so the expected OPIS ids and cities are pinned as a short
    SHA-256 digest of ``id|city|state`` for each stop, in order.
    """
    items = [
        f"{s['station']['opis_id']}|{s['station']['city']}|{s['station']['state']}" for s in stops
    ]
    return hashlib.sha256(";".join(items).encode()).hexdigest()[:24]


def dataset_is_the_recorded_one() -> bool:
    return hashlib.sha256(CSV.read_bytes()).hexdigest() == RECORDED_AGAINST_SHA256


@pytest.fixture(scope="module")
def real_index():
    imported, _ = clean_and_geocode(read_rows(CSV), Gazetteer.from_file(SHIPPED_GAZETTEER))
    return StationIndex(
        [
            StationRecord(
                s.opis_id, s.name, s.address, s.city, s.state, s.price,
                s.latitude, s.longitude, "city", s.location_source,
            )
            for s in imported
        ]
    )  # fmt: skip


@pytest.fixture
def real_world(settings, monkeypatch, real_index, http):
    """Real stations, the shipped place table, and a mocked router serving recorded routes."""
    settings.FUEL_PLANNER = {**settings.FUEL_PLANNER, "GAZETTEER_PATH": SHIPPED_GAZETTEER}
    monkeypatch.setattr("fuelplanner.services.planner.load_station_index", lambda: real_index)
    by_id = {s.opis_id: s for s in real_index.stations}

    def serve(name):
        http.get(OSRM_URL, json=json.loads((OSRM_FIXTURES / f"{name}.json").read_text()))
        return by_id

    return serve


def test_a_short_trip_costs_nothing_whatever_the_data(client, real_world):
    real_world("dallas_houston")
    data = post(client, "Dallas, TX", "Houston, TX").json()
    assert data["fuel_stops"] == [] and data["summary"]["total_fuel_cost"] == 0
    assert data["route"]["distance_miles"] == pytest.approx(238.3, abs=0.1)


def test_chicago_to_dallas(client, real_world):
    by_id = real_world("chicago_dallas")
    data = post(client).json()
    check_success(data, by_id, 5, 1.0)
    assert data["route"]["distance_miles"] == pytest.approx(961.5, abs=0.1)
    assert data["summary"]["fuel_stops"] >= 2  # 961 miles cannot be done on one tank
    if dataset_is_the_recorded_one():
        stops = data["fuel_stops"]
        assert stop_fingerprint(stops) == "f204d9df7ede9b4e0c937d03"  # the exact two stations
        assert [s["station"]["state"] for s in stops] == ["IL", "TX"]
        assert [s["gallons_purchased"] for s in stops] == [30.24, 16.13]
        assert data["summary"]["total_fuel_cost"] == pytest.approx(134.01, abs=0.011)


def test_new_york_to_los_angeles(client, real_world):
    by_id = real_world("new_york_los_angeles")
    data = post(client, "New York, NY", "Los Angeles, CA").json()
    check_success(data, by_id, 5, 1.0)
    assert data["route"]["distance_miles"] == pytest.approx(2810.4, abs=0.1)
    assert data["summary"]["fuel_stops"] >= 5
    assert max(leg["distance_miles"] for leg in data["legs"]) <= 500
    if dataset_is_the_recorded_one():
        stops = data["fuel_stops"]
        assert stop_fingerprint(stops) == "a2842ea3db21b6e1cc3dfe21"  # the exact eight stations
        assert [s["station"]["state"] for s in stops] == [
            "OH", "IL", "NE", "NE", "NE", "CO", "UT", "NV",
        ]  # fmt: skip
        assert data["summary"]["total_fuel_cost"] == pytest.approx(713.35, abs=0.011)
        assert data["summary"]["fuel_purchased_gallons"] == pytest.approx(233.27, abs=0.02)
        assert max(s["fuel_on_departure_gallons"] for s in stops) == 50.0  # one full fill-up


def test_seattle_to_san_diego_has_a_gap_the_vehicle_cannot_cross(client, real_world):
    real_world("seattle_san_diego")
    response = post(client, "Seattle, WA", "San Diego, CA")
    if not dataset_is_the_recorded_one():
        pytest.skip("the expected gap depends on the supplied dataset")
    assert response.status_code == 422
    err = error(response)
    assert err["code"] == "no_feasible_fuel_plan"
    details = err["details"]
    assert details["last_reachable_route_mile"] == pytest.approx(259.5, abs=1)
    assert details["distance_to_next_option_miles"] > 900
    assert 43 < details["gap_start"]["lat"] < 45.5  # in western Oregon, near Eugene
    assert -124 < details["gap_start"]["lon"] < -122
    assert details["candidate_stations"] == 17


def test_a_repeated_journey_is_identical_and_makes_no_external_call(client, real_world, http):
    real_world("chicago_dallas")
    first = post(client).json()
    second = post(client).json()
    assert len(http.calls) == 1
    assert second["meta"]["routing_cache"] == "hit"
    assert second["meta"]["external_calls"] == {"routing": 0, "geocoding": 0}
    assert second["fuel_stops"] == first["fuel_stops"]
    assert second["summary"] == first["summary"]


def test_what_if_options_move_the_cost_in_the_expected_direction(client, real_world):
    real_world("new_york_los_angeles")
    base = post(client, "New York, NY", "Los Angeles, CA").json()
    wider = post(client, "New York, NY", "Los Angeles, CA", options={"max_detour_miles": 25}).json()
    emptier = post(
        client, "New York, NY", "Los Angeles, CA", options={"initial_fuel_fraction": 0.4}
    ).json()
    assert wider["meta"]["candidate_stations"] > base["meta"]["candidate_stations"]
    assert wider["summary"]["total_fuel_cost"] <= base["summary"]["total_fuel_cost"] + 0.005
    assert emptier["summary"]["total_fuel_cost"] >= base["summary"]["total_fuel_cost"] - 0.005
    assert emptier["summary"]["fuel_purchased_gallons"] > base["summary"]["fuel_purchased_gallons"]


def test_planning_a_coast_to_coast_trip_on_real_data_is_fast(client, real_world):
    real_world("new_york_los_angeles")
    warm_up_gazetteer()  # what the server does at start-up (runserver / the health endpoint)
    data = post(client, "New York, NY", "Los Angeles, CA").json()
    print("coast-to-coast, uncached route: elapsed_ms =", data["meta"]["elapsed_ms"])
    # Everything except the (mocked) network: parse 3,300 points, match 6,600 stations,
    # optimise, account, serialise.
    assert data["meta"]["elapsed_ms"] < 400
    cached = post(client, "New York, NY", "Los Angeles, CA").json()
    print("coast-to-coast, cached route:   elapsed_ms =", cached["meta"]["elapsed_ms"])
    assert cached["meta"]["elapsed_ms"] < 250
