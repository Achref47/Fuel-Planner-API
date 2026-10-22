"""Boundary and unusual journeys through the whole API."""

import random
from decimal import Decimal

import numpy as np
import pytest

from fuelplanner.models import Station

from .helpers import (
    CHICAGO,
    DALLAS,
    HOUSTON,
    OSRM_URL,
    add_station,
    error,
    mock_route,
    osrm_response,
    post,
)
from .invariants import check_success

pytestmark = pytest.mark.django_db


def stations_by_id():
    return {s.opis_id: s for s in Station.objects.all()}


# --- degenerate routes ---------------------------------------------------------------------------


def test_a_route_of_zero_length_is_answered_without_error(client, http):
    """Two different towns that the router snaps to the same road point."""
    point = np.array([[DALLAS[0], DALLAS[1]], [DALLAS[0], DALLAS[1]]])
    http.get(OSRM_URL, json=osrm_response(point, 0.0))
    response = post(client, "Dallas, TX", "Houston, TX")
    assert response.status_code == 200, response.content
    data = response.json()
    assert data["route"]["distance_miles"] == 0 and data["fuel_stops"] == []
    assert data["summary"]["total_fuel_cost"] == 0 and data["summary"]["fuel_used_gallons"] == 0


def test_a_two_point_geometry_is_a_valid_route(client, http):
    mock_route(http, DALLAS, HOUSTON, distance=240.0, n=2)
    data = post(client, "Dallas, TX", "Houston, TX").json()
    assert data["route"]["geometry_points"] == 2 and data["summary"]["fuel_stops"] == 0


def test_a_route_of_exactly_the_starting_range_needs_no_stop(client, http):
    mock_route(http, DALLAS, HOUSTON, distance=500.0)
    data = post(client, "Dallas, TX", "Houston, TX").json()
    assert data["fuel_stops"] == []
    assert data["summary"]["fuel_remaining_at_destination_gallons"] == 0


def test_a_hair_over_the_starting_range_needs_a_tiny_purchase(client, http):
    coords = mock_route(http, DALLAS, HOUSTON, distance=500.4)
    add_station(1, coords, 500.4, 100, "3.000")
    data = post(client, "Dallas, TX", "Houston, TX").json()
    (stop,) = data["fuel_stops"]
    assert stop["gallons_purchased"] == pytest.approx(0.04, abs=0.006)  # 0.4 mile of fuel
    assert stop["cost"] == pytest.approx(0.12, abs=0.011)
    check_success(data, stations_by_id(), 5, 1.0)


def test_with_no_station_a_trip_over_the_range_reports_missing_fuel_data(client, http):
    mock_route(http, DALLAS, HOUSTON, distance=500.4)
    response = post(client, "Dallas, TX", "Houston, TX")
    assert (response.status_code, error(response)["code"]) == (503, "fuel_data_unavailable")


def test_starting_empty_needs_a_station_right_at_the_start(client, http):
    coords = mock_route(http, DALLAS, HOUSTON, distance=300.0)
    add_station(1, coords, 300.0, 0, "3.000")
    data = post(client, "Dallas, TX", "Houston, TX", options={"initial_fuel_fraction": 0}).json()
    (stop,) = data["fuel_stops"]
    assert stop["route_mile"] == 0 and stop["distance_from_previous_miles"] == 0
    assert stop["gallons_purchased"] == pytest.approx(30.0, abs=0.01)
    assert stop["cost"] == pytest.approx(90.0, abs=0.01)
    assert data["vehicle"]["initial_fuel_gallons"] == 0
    assert data["legs"][0]["distance_miles"] == 0


def test_starting_empty_with_no_station_at_the_start_is_infeasible(client, http):
    coords = mock_route(http, DALLAS, HOUSTON, distance=300.0)
    add_station(1, coords, 300.0, 5, "3.000")  # five miles away is five miles too far
    response = post(client, "Dallas, TX", "Houston, TX", options={"initial_fuel_fraction": 0})
    assert response.status_code == 422
    err = error(response)
    assert err["code"] == "no_feasible_fuel_plan"
    assert err["details"]["last_reachable_route_mile"] == 0
    assert err["details"]["shortfall_miles"] == pytest.approx(5, abs=0.3)


def test_a_station_exactly_one_tank_away_is_reachable_and_its_leg_is_exactly_the_range(
    client, http
):
    """The inclusive range boundary, end to end: arrive with an empty tank, refuel, finish."""
    coords = mock_route(http, CHICAGO, DALLAS, distance=1000.0)
    add_station(1, coords, 1000.0, 500, "3.000")
    data = post(client).json()
    (stop,) = data["fuel_stops"]
    assert stop["route_mile"] == 500.0 and stop["distance_from_previous_miles"] == 500.0
    assert stop["fuel_on_arrival_gallons"] == 0 and stop["gallons_purchased"] == 50.0
    assert stop["cost"] == 150.0 and data["legs"][1]["distance_miles"] == 500.0
    check_success(data, stations_by_id(), 5, 1.0)


def test_a_station_a_tenth_of_a_mile_beyond_one_tank_is_unreachable(client, http):
    coords = mock_route(http, CHICAGO, DALLAS, distance=1000.0)
    add_station(1, coords, 1000.0, 500.5, "3.000")
    response = post(client)
    assert (response.status_code, error(response)["code"]) == (422, "no_feasible_fuel_plan")


# --- unusual station sets ----------------------------------------------------------------------


def test_many_stations_in_one_town_collapse_to_the_cheapest(client, http):
    coords = mock_route(http, CHICAGO, DALLAS, distance=800.0)
    add_station(1, coords, 800.0, 400, "3.900")
    for index in range(2, 42):  # forty rivals at exactly the same spot
        Station.objects.filter(opis_id=index).delete()
        base = Station.objects.get(opis_id=1)
        Station.objects.create(
            opis_id=index, name=f"RIVAL {index}", address="x", city="Same", state="IL",
            price=Decimal("3.000") + Decimal(index) / 1000, latitude=base.latitude,
            longitude=base.longitude, location_source="place",
        )  # fmt: skip
    data = post(client).json()
    assert data["meta"]["candidate_stations"] == 1  # one location, one candidate
    assert data["fuel_stops"][0]["station"]["opis_id"] == 2  # the cheapest of the forty-one
    assert data["fuel_stops"][0]["price_per_gallon"] == pytest.approx(3.002)


def test_identical_prices_use_the_fewest_possible_stops(client, http):
    coords = mock_route(http, CHICAGO, DALLAS, distance=1400.0)
    for i, mile in enumerate(range(50, 1400, 50), start=1):
        add_station(i, coords, 1400.0, mile, "3.000")
    data = post(client).json()
    # (1400 - 500) miles must be bought somewhere; two stops are the minimum that works.
    assert data["summary"]["fuel_stops"] == 2
    assert data["summary"]["total_fuel_cost"] == pytest.approx(90 * 3.0, abs=0.05)
    check_success(data, stations_by_id(), 5, 1.0)


def test_real_world_prices_with_many_decimals_are_costed_exactly(client, http):
    coords = mock_route(http, CHICAGO, DALLAS, distance=700.0)
    add_station(1, coords, 700.0, 300, "3.00733333")
    (stop,) = post(client).json()["fuel_stops"]
    assert stop["gallons_purchased"] == 20.0
    assert stop["price_per_gallon"] == 3.007  # shown to 3 decimals...
    assert stop["cost"] == 60.15  # ...but billed at the full price: 20 x 3.00733333 = 60.1467


def test_a_6000_mile_trip_uses_a_dozen_stops_and_stays_consistent(client, http):
    coords = mock_route(http, CHICAGO, DALLAS, distance=6000.0, n=2000)
    rng = random.Random(8)
    for i in range(1, 160):
        add_station(
            i, coords, 6000.0, rng.uniform(0, 6000), f"{rng.uniform(2.8, 4.4):.3f}",
            off=rng.choice([0, 0, 1, 3]),
        )  # fmt: skip
    response = post(client)
    assert response.status_code == 200, response.content
    data = response.json()
    assert data["summary"]["fuel_stops"] >= 11  # (6000 - 500) / 500
    check_success(data, stations_by_id(), 5, 1.0)


def test_both_directions_of_a_journey_are_valid_and_symmetric_with_uniform_prices(client, http):
    coords = mock_route(http, CHICAGO, DALLAS, distance=1000.0)
    for i, mile in enumerate(range(60, 1000, 60), start=1):
        add_station(i, coords, 1000.0, mile, "3.250")
    forward = post(client).json()
    http.reset()
    mock_route(http, DALLAS, CHICAGO, distance=1000.0)
    backward = post(client, "Dallas, TX", "Chicago, IL").json()
    for data in (forward, backward):
        check_success(data, stations_by_id(), 5, 1.0)
        assert data["summary"]["total_fuel_cost"] == pytest.approx(50 * 3.25, abs=0.05)


@pytest.mark.parametrize("detour", [0.5, 25])
def test_detour_limits_are_inclusive(client, http, detour):
    mock_route(http, DALLAS, HOUSTON, distance=240.0)
    response = post(client, "Dallas, TX", "Houston, TX", options={"max_detour_miles": detour})
    assert response.status_code == 200
    assert response.json()["meta"]["max_detour_miles"] == detour


@pytest.mark.parametrize("detour", [0.49, 25.01, -1, 0])
def test_detour_limits_outside_the_documented_range_are_rejected(client, http, detour):
    response = post(client, options={"max_detour_miles": detour})
    assert response.status_code == 400
    assert "max_detour_miles" in error(response)["details"]["options"]


# --- the vehicle is configuration, not a constant -----------------------------------------------


def test_a_different_vehicle_is_honoured_throughout_the_response(client, http, settings):
    """A 300-mile tank at 20 mpg (15 gallons): the ledger, legs and response all follow it."""
    settings.FUEL_PLANNER = {**settings.FUEL_PLANNER, "MAX_RANGE_MILES": 300.0, "MPG": 20.0}
    coords = mock_route(http, CHICAGO, DALLAS, distance=700.0)
    add_station(1, coords, 700.0, 250, "4.000")
    add_station(2, coords, 700.0, 500, "4.000")
    data = post(client).json()

    assert data["vehicle"] == {
        "max_range_miles": 300.0,
        "miles_per_gallon": 20.0,
        "tank_gallons": 15.0,
        "initial_fuel_gallons": 15.0,
    }
    assert data["summary"]["fuel_stops"] == 2
    assert all(leg["distance_miles"] <= 300.05 for leg in data["legs"])
    for leg in data["legs"]:
        assert leg["fuel_used_gallons"] == pytest.approx(leg["distance_miles"] / 20, abs=0.011)
    # 700 miles at 20 mpg = 35 gallons burned; 15 were on board, so 20 gallons at $4.00 = $80.
    assert data["summary"]["fuel_used_gallons"] == pytest.approx(35.0, abs=0.05)
    assert data["summary"]["fuel_purchased_gallons"] == pytest.approx(20.0, abs=0.05)
    assert data["summary"]["total_fuel_cost"] == pytest.approx(80.0, abs=0.1)
    assert max(s["fuel_on_departure_gallons"] for s in data["fuel_stops"]) <= 15.01
    assert "15 gallons" in data["assumptions"][0] and "300-mile" in data["assumptions"][1]
