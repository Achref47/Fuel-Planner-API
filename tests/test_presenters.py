"""Behaviour of the response builder that is not obvious from the response alone."""

import time
from unittest import mock

import pytest

from fuelplanner.api import presenters
from fuelplanner.services.geometry import simplify_route

from .helpers import CHICAGO, DALLAS, HOUSTON, mock_route, post, seed_chicago_dallas

pytestmark = pytest.mark.django_db


def test_elapsed_ms_includes_the_time_spent_building_the_response(client, http):
    """The diagnostic must not flatter the server: slow geometry work shows up in the number."""
    coords = mock_route(http, CHICAGO, DALLAS, distance=970.0, n=500)
    seed_chicago_dallas(coords)

    def slow_simplify(c, tolerance):
        time.sleep(0.25)
        return simplify_route(c, tolerance)

    with mock.patch.object(presenters, "simplify_route", slow_simplify):
        data = post(client).json()
    assert data["meta"]["elapsed_ms"] >= 250


def test_simplified_geometry_is_computed_once_per_route(client, http):
    coords = mock_route(http, CHICAGO, DALLAS, distance=970.0, n=500)
    seed_chicago_dallas(coords)
    with mock.patch.object(presenters, "simplify_route", wraps=simplify_route) as spy:
        first = post(client).json()
        second = post(client).json()
        third = post(client, options={"max_detour_miles": 2}).json()  # same route, other options
    assert spy.call_count == 1
    assert first["route"]["geometry"] == second["route"]["geometry"] == third["route"]["geometry"]


def test_a_different_tolerance_or_route_is_simplified_separately(client, http, settings):
    coords = mock_route(http, CHICAGO, DALLAS, distance=970.0, n=500)
    seed_chicago_dallas(coords)
    with mock.patch.object(presenters, "simplify_route", wraps=simplify_route) as spy:
        post(client)
        settings.FUEL_PLANNER = {**settings.FUEL_PLANNER, "GEOMETRY_TOLERANCE_MILES": 0.5}
        post(client)
        assert spy.call_count == 2
        http.reset()
        mock_route(http, DALLAS, HOUSTON, distance=240.0, n=300)
        post(client, "Dallas, TX", "Houston, TX")
        assert spy.call_count == 3


def test_full_and_none_modes_never_simplify(client, http):
    coords = mock_route(http, CHICAGO, DALLAS, distance=970.0, n=500)
    seed_chicago_dallas(coords)
    with mock.patch.object(presenters, "simplify_route", wraps=simplify_route) as spy:
        post(client, options={"geometry": "full"})
        post(client, options={"geometry": "none"})
    assert spy.call_count == 0


def test_rounding_helpers_round_half_up_and_return_plain_numbers():
    from decimal import Decimal

    assert presenters.money(Decimal("2.675")) == 2.68  # half-up, not banker's rounding
    assert presenters.money(Decimal("2.665")) == 2.67
    assert presenters.gallons(Decimal("0.005")) == 0.01
    assert presenters.miles(2.26) == 2.3 and presenters.miles(2.24) == 2.2
    assert isinstance(presenters.money(Decimal("1")), float)
