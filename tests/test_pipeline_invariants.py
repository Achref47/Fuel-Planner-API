"""Randomised end-to-end checks through the real API.

For many random journeys and station sets (a straight-line routing provider stands in for
OSRM) every response must satisfy a long list of invariants, and the API's verdict on
feasibility and its candidate count are cross-checked against an independent, brute-force
re-implementation of station matching and reachability.
"""

import random

import pytest
from rest_framework.test import APIClient
from scipy.spatial import cKDTree

from fuelplanner.models import Station
from fuelplanner.services.geometry import (
    chord_to_miles,
    resample_route,
    unit_vectors,
)
from fuelplanner.services.locations import resolve_location
from fuelplanner.services.stations import reset_station_index

from .fake_provider import LineProvider
from .helpers import point_beside_route, station_object
from .invariants import check_success

URL = "/api/v1/fuel-route/"
PAIRS = [
    ("Chicago, IL", "Dallas, TX"),
    ("Harrisburg, PA", "Carson City, NV"),
    ("Houston, TX", "Chicago, IL"),
    ("St. Louis, MO", "Harrisburg, PA"),
    ("Hot Springs, AR", "Carson City, NV"),
    ("Dallas, TX", "Houston, TX"),
    ("Harrisburg, PA", "Houston, TX"),
]
CAP = 500.0
pytestmark = pytest.mark.django_db


def build_scenario(rng, start_text, finish_text):
    """Stations scattered along (and beside) the straight-line route between two cities."""
    start, finish = resolve_location(start_text), resolve_location(finish_text)
    route = LineProvider({}).route((start.lat, start.lon), (finish.lat, finish.lon))
    path = resample_route(route.coordinates, 0.5, total_miles=route.distance_miles)
    length = route.distance_miles

    specs = [
        (
            rng.uniform(0, length),
            rng.choice([0, 0, 0, 0.4, 1, 2.5, 4, 7, 11, 20]),
            round(rng.uniform(2.7, 4.6), 3),
        )
        for _ in range(rng.randint(25, 260))
    ]
    if rng.random() < 0.25 and length > 700:  # carve out a window nobody can cross
        lo = rng.uniform(100, length - 600)
        specs = [s for s in specs if not lo <= s[0] <= lo + 560]
    stations = []
    for index, (mile, off, price) in enumerate(specs, start=1):
        lat, lon = point_beside_route(path, mile, off)
        stations.append(station_object(index, lat, lon, price))
    return route, stations


def independent_candidates(route, stations, max_detour):
    """Brute force: dense 0.05-mile sampling of the route and a nearest-sample search."""
    dense = resample_route(route.coordinates, 0.05, total_miles=route.distance_miles)
    tree = cKDTree(unit_vectors(dense.lat, dense.lon))
    chord, nearest = tree.query(
        unit_vectors([s.latitude for s in stations], [s.longitude for s in stations])
    )
    off = chord_to_miles(chord)
    return [
        {"mile": float(dense.miles[nearest[i]]), "off": float(off[i])}
        for i in range(len(stations))
        if off[i] <= max_detour
    ]


def reachable(candidates, length, initial, slack):
    """Can the destination be reached at all? (forward closure, independent of the optimiser)"""
    nodes = sorted(candidates, key=lambda c: c["mile"])
    ok = []
    for j, c in enumerate(nodes):
        ok.append(
            c["mile"] + c["off"] <= initial + slack
            or any(
                ok[i] and nodes[i]["off"] + c["mile"] - nodes[i]["mile"] + c["off"] <= CAP + slack
                for i in range(j)
            )
        )
    return length <= initial + slack or any(
        ok[i] and nodes[i]["off"] + length - nodes[i]["mile"] <= CAP + slack
        for i in range(len(nodes))
    )


def test_random_journeys_satisfy_every_invariant_and_match_independent_checks(settings):
    settings.FUEL_PLANNER = {
        **settings.FUEL_PLANNER,
        "ROUTING_PROVIDER": "tests.fake_provider.LineProvider",
    }
    client = APIClient()
    rng = random.Random(2024)
    successes = failures = with_stops = 0

    for scenario in range(70):
        start_text, finish_text = rng.choice(PAIRS)
        route, stations = build_scenario(rng, start_text, finish_text)
        max_detour = rng.choice([0.5, 1, 2, 5, 5, 10, 25])
        fraction = rng.choice([1.0, 1.0, 1.0, 0.8, 0.4])
        Station.objects.all().delete()
        Station.objects.bulk_create(stations)
        reset_station_index()
        by_id = {s.opis_id: s for s in stations}
        label = f"scenario {scenario}: {start_text}->{finish_text} detour={max_detour} f={fraction}"

        response = client.post(
            URL,
            {
                "start": start_text,
                "finish": finish_text,
                "options": {"max_detour_miles": max_detour, "initial_fuel_fraction": fraction},
            },
            format="json",
        )
        candidates = independent_candidates(route, stations, max_detour)
        initial_miles = fraction * CAP
        length = route.distance_miles

        if response.status_code == 200:
            successes += 1
            data = response.json()
            assert reachable(candidates, length, initial_miles, slack=0.6), label
            check_success(data, by_id, max_detour, fraction)
            with_stops += bool(data["fuel_stops"])
            if length > initial_miles + 1e-6:  # station matching only happens when fuel is needed
                lower = len(independent_candidates(route, stations, max_detour - 0.3))
                upper = len(independent_candidates(route, stations, max_detour + 0.3))
                assert lower <= data["meta"]["candidate_stations"] <= upper, label
        else:
            failures += 1
            assert response.status_code == 422, label + str(response.content)
            assert response.json()["error"]["code"] == "no_feasible_fuel_plan", label
            assert not reachable(candidates, length, initial_miles, slack=-0.6), label

    assert successes >= 30 and failures >= 5 and with_stops >= 20
