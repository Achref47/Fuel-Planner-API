import time
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
from django.conf import settings

from fuelplanner.errors import FuelDataUnavailableError
from fuelplanner.models import Station
from fuelplanner.services.geometry import EARTH_RADIUS_MILES, resample_route
from fuelplanner.services.stations import (
    StationIndex,
    StationRecord,
    load_station_index,
    reset_station_index,
)

MILES_PER_DEGREE = EARTH_RADIUS_MILES * np.pi / 180


@pytest.fixture(autouse=True)
def _fresh_index():
    reset_station_index()
    yield
    reset_station_index()


def east_west_route(step=0.5):
    """~530-mile route along latitude 40, lon -100 -> -90, with a vertex every 0.05 degrees."""
    lon = np.linspace(-100, -90, 201)
    coords = np.column_stack((lon, np.full_like(lon, 40.0)))
    return resample_route(coords, step_miles=step)


def station(opis_id, lat, lon, price="3.50", **kwargs):
    return StationRecord(
        opis_id=opis_id,
        name=f"S{opis_id}",
        address="I-1",
        city="X",
        state="NE",
        price=Decimal(price),
        latitude=lat,
        longitude=lon,
        **kwargs,
    )


def north_of_route(lon, miles):
    return 40.0 + miles / MILES_PER_DEGREE, lon


def test_on_route_station_gets_zero_offset_and_expected_mile():
    route = east_west_route()
    index = StationIndex([station(1, 40.0, -95.0)])
    (cand,) = index.candidates_along(route, 5)
    assert cand.off_route_miles <= 0.25 + 1e-6  # at most half a resample step
    assert cand.route_mile == pytest.approx(route.length / 2, abs=0.5)


def test_offset_station_distance_is_measured_to_the_route():
    route = east_west_route()
    lat, lon = north_of_route(-95.0, 3.0)
    (cand,) = StationIndex([station(1, lat, lon)]).candidates_along(route, 5)
    assert cand.off_route_miles == pytest.approx(3.0, abs=0.1)


def test_corridor_filters_by_detour_distance():
    route = east_west_route()
    stations = [
        station(1, 40.0, -99.0),
        station(2, *north_of_route(-98.0, 3.0)),
        station(3, *north_of_route(-97.0, 10.0)),
        station(4, *north_of_route(-96.0, 24.0)),
    ]
    index = StationIndex(stations)
    ids = lambda detour: [c.station.opis_id for c in index.candidates_along(route, detour)]  # noqa: E731
    assert ids(2) == [1]
    assert ids(5) == [1, 2]
    assert ids(12) == [1, 2, 3]
    assert ids(25) == [1, 2, 3, 4]


def test_minimum_supported_corridor_includes_nearby_stations():
    route = east_west_route()
    lat, lon = north_of_route(-95.0, 0.3)
    assert StationIndex([station(1, lat, lon)]).candidates_along(route, 0.5)


def test_candidates_are_sorted_by_route_mile():
    route = east_west_route()
    stations = [station(i, 40.0, lon) for i, lon in enumerate([-92, -99, -95, -97, -91], start=1)]
    miles = [c.route_mile for c in StationIndex(stations).candidates_along(route, 5)]
    assert miles == sorted(miles)
    assert len(miles) == 5


def test_stations_off_either_end_of_the_route_are_limited_by_the_corridor():
    route = east_west_route()
    near_start = station(1, 40.0, -100.0 - 4 / (MILES_PER_DEGREE * np.cos(np.radians(40))))
    far_start = station(2, 40.0, -101.5)
    cands = StationIndex([near_start, far_start]).candidates_along(route, 5)
    assert [c.station.opis_id for c in cands] == [1]
    assert cands[0].route_mile == 0
    assert cands[0].off_route_miles == pytest.approx(4.0, abs=0.1)


def test_colocated_stations_collapse_to_the_cheapest():
    route = east_west_route()
    index = StationIndex(
        [
            station(1, 40.0, -95.0, "3.60"),
            station(2, 40.0, -95.0, "3.20"),
            station(3, 40.0, -95.0, "3.40"),
            station(4, 40.0, -94.0, "3.00"),
        ]
    )
    chosen = {c.station.opis_id for c in index.candidates_along(route, 5)}
    assert chosen == {2, 4}


def test_price_ties_keep_the_lowest_station_id():
    route = east_west_route()
    index = StationIndex([station(9, 40.0, -95.0, "3.30"), station(4, 40.0, -95.0, "3.30")])
    assert [c.station.opis_id for c in index.candidates_along(route, 5)] == [4]


def test_no_stations_near_the_route_returns_empty_list():
    route = east_west_route()
    assert StationIndex([station(1, 30.0, -95.0)]).candidates_along(route, 25) == []


def test_empty_index_raises_fuel_data_unavailable():
    with pytest.raises(FuelDataUnavailableError) as excinfo:
        StationIndex([]).candidates_along(east_west_route(), 5)
    assert excinfo.value.code == "fuel_data_unavailable"
    assert "load_fuel_data" in excinfo.value.message


def test_negative_detour_is_rejected():
    with pytest.raises(ValueError):
        StationIndex([station(1, 40, -95)]).candidates_along(east_west_route(), -1)


@pytest.mark.django_db
def test_index_loads_from_database_and_is_cached():
    Station.objects.create(
        opis_id=7,
        name="N",
        address="A",
        city="C",
        state="IL",
        price=Decimal("3.1"),
        latitude=41.0,
        longitude=-88.0,
        location_source="place",
    )
    index = load_station_index()
    assert len(index) == 1
    record = index.stations[0]
    assert (record.opis_id, record.price, record.location_precision) == (7, Decimal("3.1"), "city")
    assert load_station_index() is index
    Station.objects.create(
        opis_id=8,
        name="N2",
        address="A",
        city="C",
        state="IL",
        price=Decimal("3.2"),
        latitude=41.1,
        longitude=-88.1,
        location_source="place",
    )
    assert len(load_station_index()) == 1  # cached until refreshed / reset
    assert len(load_station_index(refresh=True)) == 2


@pytest.mark.django_db
def test_empty_database_reports_missing_data():
    with pytest.raises(FuelDataUnavailableError):
        load_station_index().candidates_along(east_west_route(), 5)


def test_matching_a_long_route_against_thousands_of_stations_is_fast():
    rng = np.random.default_rng(42)
    n_stations = 6_700
    lats = rng.uniform(26, 48, n_stations)
    lons = rng.uniform(-123, -70, n_stations)
    index = StationIndex(
        [
            station(i, float(la), float(lo))
            for i, (la, lo) in enumerate(zip(lats, lons, strict=True))
        ]
    )
    lon = np.linspace(-122, -75, 60_000)
    coords = np.column_stack((lon, 38 + 4 * np.sin(np.linspace(0, 5, 60_000))))
    start = time.perf_counter()
    route = resample_route(coords, 0.5)
    cands = index.candidates_along(route, 5)
    elapsed = time.perf_counter() - start
    assert route.length > 2500
    assert len(cands) > 20
    assert all(c.off_route_miles <= 5.001 for c in cands)
    assert elapsed < 0.5


REAL_CSV = Path(settings.FUEL_PLANNER["FUEL_CSV_PATH"])
REAL_GAZETTEER = Path(settings.FUEL_PLANNER["GAZETTEER_PATH"])


@pytest.mark.skipif(
    not (REAL_CSV.exists() and REAL_GAZETTEER.exists()), reason="assessment CSV not available"
)
def test_real_stations_are_found_along_a_chicago_to_dallas_corridor():
    from fuelplanner.importer import clean_and_geocode, read_rows
    from fuelplanner.services.gazetteer import Gazetteer

    imported, _ = clean_and_geocode(read_rows(REAL_CSV), Gazetteer.from_file(REAL_GAZETTEER))
    index = StationIndex(
        [
            StationRecord(
                s.opis_id, s.name, s.address, s.city, s.state, s.price, s.latitude, s.longitude
            )
            for s in imported
        ]
    )
    lon = np.linspace(-87.63, -96.80, 400)
    lat = np.linspace(41.88, 32.78, 400)
    route = resample_route(np.column_stack((lon, lat)), 0.5)
    cands = index.candidates_along(route, 15)
    assert len(cands) > 10
    assert [c.route_mile for c in cands] == sorted(c.route_mile for c in cands)
    assert all(c.off_route_miles <= 15.01 for c in cands)
