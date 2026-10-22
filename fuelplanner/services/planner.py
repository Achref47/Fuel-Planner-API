"""End-to-end trip planning: locations -> route -> candidate stations -> optimal stops -> ledger."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from django.conf import settings

from ..errors import NoFeasiblePlanError, SameLocationError
from .accounting import FuelLedger, build_ledger
from .geometry import RoutePath, haversine_miles, resample_route
from .locations import ResolvedLocation, resolve_location
from .optimizer import EPS, FuelOption, OptimalPlan, plan_fuel_stops
from .routing import RouteResult, get_route
from .stations import Candidate, load_station_index

logger = logging.getLogger(__name__)

# Two points closer than this are treated as the same place.
SAME_PLACE_MILES = 0.1


@dataclass(frozen=True)
class TripPlan:
    start: ResolvedLocation
    finish: ResolvedLocation
    route: RouteResult
    route_cache: str  # "hit" or "miss"
    path: RoutePath
    candidates: list[Candidate]
    plan: OptimalPlan
    ledger: FuelLedger
    max_detour_miles: float
    initial_fuel_fraction: float


def locate_on_route(path: RoutePath, mile: float) -> dict:
    """Coordinates of the point ``mile`` miles along the route."""
    return {
        "route_mile": round(float(mile), 1),
        "lat": round(float(np.interp(mile, path.miles, path.lat)), 5),
        "lon": round(float(np.interp(mile, path.miles, path.lon)), 5),
    }


def plan_trip(
    start_input,
    finish_input,
    *,
    max_detour_miles: float,
    initial_fuel_fraction: float,
) -> TripPlan:
    cfg = settings.FUEL_PLANNER
    max_range, mpg = cfg["MAX_RANGE_MILES"], cfg["MPG"]

    start = resolve_location(start_input)
    finish = resolve_location(finish_input)
    if haversine_miles(start.lat, start.lon, finish.lat, finish.lon) < SAME_PLACE_MILES:
        raise SameLocationError(
            "The start and finish are the same place. Choose two different locations.",
            {"start": start.label, "finish": finish.label},
        )

    lookup = get_route((start.lat, start.lon), (finish.lat, finish.lon))
    route = lookup.route
    path = resample_route(
        route.coordinates, cfg["RESAMPLE_STEP_MILES"], total_miles=route.distance_miles
    )

    initial_fuel_miles = initial_fuel_fraction * max_range
    # Stations only matter when the starting fuel cannot cover the whole route, so a short
    # trip is answered correctly ($0, no stops) even before any fuel data has been loaded.
    needs_fuel = path.length > initial_fuel_miles + EPS
    candidates = load_station_index().candidates_along(path, max_detour_miles) if needs_fuel else []
    options = [
        FuelOption(c.route_mile, c.off_route_miles, float(c.station.price)) for c in candidates
    ]
    try:
        plan = plan_fuel_stops(
            options,
            path.length,
            max_range=max_range,
            mpg=mpg,
            initial_fuel_miles=initial_fuel_miles,
        )
    except NoFeasiblePlanError as exc:
        exc.details["gap_start"] = locate_on_route(path, exc.details["last_reachable_route_mile"])
        exc.details["gap_end"] = locate_on_route(path, exc.details["next_option_route_mile"])
        exc.details["max_detour_miles"] = max_detour_miles
        logger.info(
            "no feasible plan: %d candidates, shortfall %.0f mi",
            len(candidates),
            exc.details["shortfall_miles"],
        )
        raise

    ledger = build_ledger(plan, candidates, route_length=path.length, max_range=max_range, mpg=mpg)
    logger.info(
        "planned trip: %.0f mi, %d candidates, %d stops, total $%s",
        path.length,
        len(candidates),
        len(plan.stops),
        ledger.total_cost,
    )
    return TripPlan(
        start=start,
        finish=finish,
        route=route,
        route_cache=lookup.cache_status,
        path=path,
        candidates=candidates,
        plan=plan,
        ledger=ledger,
        max_detour_miles=max_detour_miles,
        initial_fuel_fraction=initial_fuel_fraction,
    )
