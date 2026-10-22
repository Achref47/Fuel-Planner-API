"""Defensive checks and argument validation in the service layer."""

import re
from decimal import Decimal

import pytest
import responses

from fuelplanner.errors import LocationNotFoundError, PlanInvariantError
from fuelplanner.services import routing
from fuelplanner.services.accounting import build_ledger
from fuelplanner.services.geometry import RoutePath, resample_route
from fuelplanner.services.locations import resolve_location
from fuelplanner.services.optimizer import OptimalPlan, PlannedStop, plan_fuel_stops
from fuelplanner.services.routing import OSRMProvider, get_routing_provider
from fuelplanner.services.stations import Candidate, StationRecord

from .helpers import osrm_response

OSRM_URL = re.compile(r"https://router\.project-osrm\.org/route/v1/driving/.*")


def candidate(mile=100.0, price="3.00"):
    station = StationRecord(1, "S", "A", "C", "TX", Decimal(price), 32.0, -97.0)
    return Candidate(station, mile, 0.0)


# --- the ledger refuses plans that are physically impossible ----------------------------------


@pytest.mark.parametrize("initial", [600.0, -5.0])
def test_ledger_rejects_a_starting_fuel_outside_the_tank(initial):
    plan = OptimalPlan((), 100.0, 0.0, initial)
    with pytest.raises(PlanInvariantError, match="starting fuel") as excinfo:
        build_ledger(plan, [], route_length=100.0)
    assert excinfo.value.http_status == 500 and excinfo.value.code == "plan_invariant_violation"


def test_a_leg_longer_than_one_tank_is_always_caught_by_the_fuel_checks():
    """No separate 'leg too long' rule is needed: such a leg means running dry or overfilling."""
    dry = OptimalPlan((PlannedStop(0, 520.0, 0.0, 0.0),), 10.0, 0.0, 500.0)
    with pytest.raises(PlanInvariantError, match="ran out of fuel before stop 1"):
        build_ledger(dry, [candidate()], route_length=530.0)
    overfilled = OptimalPlan((PlannedStop(0, 10.0, 490.0, 40.0),), 520.0, 0.0, 500.0)
    with pytest.raises(PlanInvariantError, match="overfilled"):
        build_ledger(overfilled, [candidate()], route_length=530.0)


def test_ledger_rejects_a_plan_that_drives_less_than_the_route():
    plan = OptimalPlan((), 100.0, 0.0, 500.0)  # claims to finish a 500-mile route in 100 miles
    with pytest.raises(PlanInvariantError, match="shorter than the route"):
        build_ledger(plan, [], route_length=500.0)


def test_a_valid_hand_built_plan_passes():
    plan = OptimalPlan((PlannedStop(0, 300.0, 200.0, 100.0),), 100.0, 30.0, 500.0)
    ledger = build_ledger(plan, [candidate(300.0, "3.00")], route_length=400.0)
    assert ledger.total_cost == Decimal("30.00")
    assert ledger.fuel_remaining_gallons == pytest.approx(Decimal("20"))  # 300 mi left - 100 mi


# --- optimiser argument validation ---------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_range": 0},
        {"max_range": -500},
        {"mpg": 0},
        {"mpg": -10},
        {"initial_fuel_miles": 600},
        {"initial_fuel_miles": -1},
    ],
)
def test_optimiser_rejects_impossible_vehicle_parameters(kwargs):
    with pytest.raises(ValueError):
        plan_fuel_stops([], 100.0, **kwargs)


def test_optimiser_rejects_a_negative_route_length():
    with pytest.raises(ValueError):
        plan_fuel_stops([], -1.0)


def test_a_vehicle_with_a_different_tank_and_efficiency_is_supported():
    """Nothing is hard-wired to 500 mi / 10 mpg: a 300-mile tank at 20 mpg works too."""
    from fuelplanner.services.optimizer import FuelOption

    options = [FuelOption(250, 0, 4.0)]
    plan = plan_fuel_stops(options, 500.0, max_range=300.0, mpg=20.0)
    (stop,) = plan.stops
    assert stop.fuel_on_arrival_miles == pytest.approx(50)
    assert stop.purchase_miles == pytest.approx(200)  # 250 miles to go, 50 already on board
    assert plan.cost == pytest.approx(200 / 20 * 4.0)  # 10 gallons at $4


# --- routing corners -----------------------------------------------------------------------------


@responses.activate
def test_missing_waypoint_distances_are_tolerated():
    import numpy as np

    body = osrm_response(np.array([[-97.0, 32.0], [-95.0, 30.0]]), 250.0)
    body["waypoints"] = [{"name": "a"}, "garbage"]
    responses.get(OSRM_URL, json=body)
    result = OSRMProvider().route((32.0, -97.0), (30.0, -95.0))
    assert result.start_snap_miles is None and result.end_snap_miles is None
    assert result.distance_miles == pytest.approx(250.0)


@responses.activate
def test_a_response_without_waypoints_is_fine():
    import numpy as np

    body = osrm_response(np.array([[-97.0, 32.0], [-95.0, 30.0]]), 250.0)
    del body["waypoints"]
    responses.get(OSRM_URL, json=body)
    assert OSRMProvider().route((32.0, -97.0), (30.0, -95.0)).start_snap_miles is None


def test_a_misconfigured_provider_path_fails_loudly(settings):
    settings.FUEL_PLANNER = {**settings.FUEL_PLANNER, "ROUTING_PROVIDER": "no.such.module.Provider"}
    with pytest.raises(ImportError):
        get_routing_provider()
    routing.reset_routing_provider()


def test_the_osrm_profile_and_base_url_come_from_configuration(settings):
    settings.FUEL_PLANNER = {
        **settings.FUEL_PLANNER,
        "OSRM_BASE_URL": "http://osrm.internal:5000/",
        "OSRM_PROFILE": "truck",
    }
    provider = OSRMProvider()
    assert provider.base_url == "http://osrm.internal:5000" and provider.profile == "truck"
    with responses.RequestsMock() as rsps:
        import numpy as np

        rsps.get(
            re.compile(r"http://osrm\.internal:5000/route/v1/truck/.*"),
            json=osrm_response(np.array([[-97.0, 32.0], [-95.0, 30.0]]), 250.0),
        )
        provider.route((32.0, -97.0), (30.0, -95.0))


# --- locations and geometry corners --------------------------------------------------------------


@pytest.mark.parametrize("text", ["USA", "United States", "us", ", ,", ",", " , USA"])
def test_inputs_with_nothing_but_a_country_or_commas_are_not_locations(text):
    with pytest.raises(LocationNotFoundError):
        resolve_location(text)


def test_route_path_reports_its_length():
    path = resample_route(
        __import__("numpy").array([[-97.0, 32.0], [-97.0, 33.0]]), step_miles=10.0
    )
    assert isinstance(path, RoutePath) and len(path) == len(path.miles) > 5
