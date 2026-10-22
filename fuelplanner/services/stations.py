"""Fuel-station index and route-corridor matching.

Given a resampled route, find every station within ``max_detour_miles`` of it and
record *where along the route* it sits and *how far off the route* it is. The
optimiser then works purely with those two numbers per station.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import numpy as np
from scipy.spatial import cKDTree

from ..errors import FuelDataUnavailableError
from .geometry import RoutePath, chord_to_miles, miles_to_chord, unit_vectors


@dataclass(frozen=True)
class StationRecord:
    """Immutable station data used by the planner (decoupled from the ORM)."""

    opis_id: int
    name: str
    address: str
    city: str
    state: str
    price: Decimal  # USD per gallon
    latitude: float
    longitude: float
    location_precision: str = "city"
    location_source: str = ""


@dataclass(frozen=True)
class Candidate:
    """A station that can be reached from the route with a small detour."""

    station: StationRecord
    route_mile: float  # distance along the route to the closest route point
    off_route_miles: float  # straight-line distance from the route to the station


class StationIndex:
    """All known stations with a precomputed unit-sphere array for fast spatial queries."""

    def __init__(self, stations: list[StationRecord]):
        self.stations = list(stations)
        self._xyz = unit_vectors(
            [s.latitude for s in self.stations], [s.longitude for s in self.stations]
        )

    def __len__(self) -> int:
        return len(self.stations)

    def require_data(self) -> None:
        """Raise ``FuelDataUnavailableError`` if no stations have been loaded."""
        if not self.stations:
            raise FuelDataUnavailableError(
                "No fuel stations are loaded. Run `python manage.py load_fuel_data` first "
                "(see the README for where to place the dataset)."
            )

    def candidates_along(self, route: RoutePath, max_detour_miles: float) -> list[Candidate]:
        """Stations within ``max_detour_miles`` of the route, ordered by route mile.

        Each station is assigned to its *nearest* route sample. Because samples are at most
        ``RESAMPLE_STEP_MILES`` apart, the off-route distance is overstated by at most half a
        step (0.25 mi for a station right on the route, ~0.006 mi at 5 mi away) and the route
        mile is accurate to half a step. Corridors below one step are therefore unreliable.
        Stations at identical coordinates are collapsed to the cheapest one (the others can
        never be a better choice: same place, higher price); ties go to the lowest id.
        """
        self.require_data()
        if max_detour_miles < 0:
            raise ValueError("max_detour_miles must be >= 0")

        tree = cKDTree(unit_vectors(route.lat, route.lon))
        chord, nearest = tree.query(
            self._xyz, distance_upper_bound=miles_to_chord(max_detour_miles) + 1e-12
        )
        inside = np.flatnonzero(np.isfinite(chord))
        off_miles = chord_to_miles(chord[inside])
        route_miles = route.miles[nearest[inside]]

        cheapest: dict[tuple[float, float], Candidate] = {}
        for i, off, mile in zip(inside, off_miles, route_miles, strict=True):
            station = self.stations[i]
            cand = Candidate(station, float(mile), float(off))
            key = (station.latitude, station.longitude)
            held = cheapest.get(key)
            if held is None or (station.price, station.opis_id) < (
                held.station.price,
                held.station.opis_id,
            ):
                cheapest[key] = cand
        return sorted(cheapest.values(), key=lambda c: (c.route_mile, c.station.opis_id))


_index_cache: StationIndex | None = None


def load_station_index(*, refresh: bool = False) -> StationIndex:
    """Build (once per process) the index from the database.

    An empty table is never cached, so a server that started before ``load_fuel_data`` was run
    picks the data up on the next request. After re-importing, restart the server to refresh a
    non-empty index.
    """
    global _index_cache
    if _index_cache is not None and not refresh:
        return _index_cache

    from django.db import DatabaseError

    from ..models import Station

    try:
        records = [
            StationRecord(
                opis_id=s.opis_id,
                name=s.name,
                address=s.address,
                city=s.city,
                state=s.state,
                price=s.price,
                latitude=s.latitude,
                longitude=s.longitude,
                location_precision=s.location_precision,
                location_source=s.location_source,
            )
            for s in Station.objects.all().iterator()
        ]
    except DatabaseError as exc:
        raise FuelDataUnavailableError(
            "The station database is not ready. Run `python manage.py migrate` and "
            "`python manage.py load_fuel_data` (see the README).",
            {"reason": "database_not_ready"},
        ) from exc

    index = StationIndex(records)
    if records:
        _index_cache = index
    return index


def reset_station_index() -> None:
    """Forget the cached index (after a re-import, and between tests)."""
    global _index_cache
    _index_cache = None
