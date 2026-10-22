"""Shape a ``TripPlan`` into the public JSON (and GeoJSON) response.

Units: miles, gallons, USD per gallon, USD. Rounding (display only - all arithmetic is done at
full precision, and the total is the sum of the cent-rounded stop costs):

* miles 1 decimal, gallons 2 decimals, price per gallon 3 decimals, money 2 decimals
* coordinates 6 decimals for locations, 5 decimals along the route geometry
"""

from __future__ import annotations

import hashlib
import time
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.cache import cache

from ..services.geometry import simplify_route
from ..services.planner import TripPlan

PLACES_ATTRIBUTION = (
    "Place coordinates: U.S. Census Bureau Gazetteer and USGS GNIS (public domain)."
)


def data_notice() -> str:
    """Set (``DATA_NOTICE``) on deployments that run on synthetic data, empty otherwise."""
    return settings.FUEL_PLANNER.get("DATA_NOTICE", "")


def data_attribution() -> str:
    if notice := data_notice():
        return f"{PLACES_ATTRIBUTION} {notice}"
    return f"{PLACES_ATTRIBUTION} Fuel prices: supplied OPIS dataset."


def _q(value: Decimal, places: str) -> float:
    return float(Decimal(value).quantize(Decimal(places), rounding=ROUND_HALF_UP))


def gallons(value: Decimal) -> float:
    return _q(value, "0.01")


def money(value: Decimal) -> float:
    return _q(value, "0.01")


def miles(value: float) -> float:
    return round(float(value), 1)


def _location(loc) -> dict:
    return {
        "input": loc.query,
        "label": loc.label,
        "lat": round(loc.lat, 6),
        "lon": round(loc.lon, 6),
        "resolved_by": loc.resolved_by,
        "precision": loc.precision,
    }


def _simplified(coords, tolerance_miles: float):
    """Simplify a route, remembering the result: simplification costs tens of milliseconds, and
    a cached route should not pay for it on every request."""
    digest = hashlib.sha256(coords.tobytes()).hexdigest()[:32]
    key = f"fuelplanner:simplified:{digest}:{tolerance_miles}"
    simple = cache.get(key)
    if simple is None:
        simple = simplify_route(coords, tolerance_miles)
        cache.set(key, simple, settings.FUEL_PLANNER["ROUTE_CACHE_SECONDS"])
    return simple


def _geometry(trip: TripPlan, mode: str):
    if mode == "none":
        return None, 0
    coords = trip.route.coordinates
    if mode == "simplified":
        coords = _simplified(coords, settings.FUEL_PLANNER["GEOMETRY_TOLERANCE_MILES"])
    rounded = [[round(float(lon), 5), round(float(lat), 5)] for lon, lat in coords]
    return {"type": "LineString", "coordinates": rounded}, len(rounded)


def assumptions(trip: TripPlan) -> list[str]:
    cfg = settings.FUEL_PLANNER
    tank = Decimal(cfg["MAX_RANGE_MILES"]) / Decimal(cfg["MPG"])
    start_gal = tank * Decimal(trip.initial_fuel_fraction)
    items = [
        *([f"DATA NOTICE: {data_notice()}"] if data_notice() else []),
        f"The vehicle starts with {start_gal.normalize():f} gallons "
        f"({trip.initial_fuel_fraction:.0%} of a {tank.normalize():f}-gallon tank); that fuel is "
        "not counted as a purchase, so trips within the starting range cost $0.00.",
        "Fuel is bought only as needed to minimise cost; the vehicle may reach a stop or the "
        "destination with an empty tank, and never exceeds the "
        f"{cfg['MAX_RANGE_MILES']:g}-mile range between refuelling points.",
        (
            "Station prices are SYNTHETIC demo values, not real fuel prices."
            if data_notice()
            else "Each station's price is the lowest price listed for it in the supplied dataset, "
            "treated as the current USD price per gallon."
        ),
        "Station coordinates are not in the dataset: they are the centroid of the station's city "
        '(location_precision "city"), so a station can be a few miles from its marker.',
        f"Only stations within {trip.max_detour_miles:g} miles (straight line) of the route are "
        "considered. A detour is driven out and back, and the fuel it uses is paid for.",
    ]
    for label, loc in (("start", trip.start), ("finish", trip.finish)):
        if loc.precision in {"city", "zip_centroid"}:
            items.append(
                f"The {label} location was resolved to the {loc.precision.replace('_', ' ')} "
                f"of '{loc.label}', not to an exact address."
            )
    return items


def build_payload(trip: TripPlan, *, geometry_mode: str, started: float, calls: dict) -> dict:
    """``started`` is the ``time.perf_counter()`` value when the request began; the reported
    ``elapsed_ms`` therefore includes building this very payload (geometry and all)."""
    cfg = settings.FUEL_PLANNER
    ledger = trip.ledger
    geometry, geometry_points = _geometry(trip, geometry_mode)

    stops = []
    for acc in ledger.stops:
        cand, st = acc.candidate, acc.candidate.station
        stops.append(
            {
                "sequence": acc.sequence,
                "station": {
                    "opis_id": st.opis_id,
                    "name": st.name,
                    "address": st.address,
                    "city": st.city,
                    "state": st.state,
                    "lat": round(st.latitude, 6),
                    "lon": round(st.longitude, 6),
                    "location_precision": st.location_precision,
                    "approximate_location": st.location_precision != "exact",
                },
                "price_per_gallon": _q(acc.price_per_gallon, "0.001"),
                "route_mile": miles(cand.route_mile),
                "off_route_miles": miles(cand.off_route_miles),
                "detour_miles": miles(2 * cand.off_route_miles),
                "distance_from_previous_miles": miles(acc.distance_from_previous_miles),
                "fuel_on_arrival_gallons": gallons(acc.fuel_on_arrival_gallons),
                "gallons_purchased": gallons(acc.gallons_purchased),
                "fuel_on_departure_gallons": gallons(acc.fuel_on_departure_gallons),
                "cost": money(acc.cost),
            }
        )

    legs = [
        {
            "sequence": leg.sequence,
            "from": leg.from_label,
            "to": leg.to_label,
            "distance_miles": miles(leg.distance_miles),
            "fuel_used_gallons": gallons(leg.fuel_used_gallons),
        }
        for leg in ledger.legs
    ]

    purchased = ledger.fuel_purchased_gallons
    tank = Decimal(cfg["MAX_RANGE_MILES"]) / Decimal(cfg["MPG"])
    summary = {
        "fuel_stops": len(stops),
        "route_distance_miles": miles(ledger.route_distance_miles),
        "driving_distance_miles": miles(ledger.driving_distance_miles),
        "detour_miles": miles(ledger.driving_distance_miles - ledger.route_distance_miles),
        "fuel_used_gallons": gallons(ledger.fuel_used_gallons),
        "initial_fuel_gallons": gallons(ledger.initial_fuel_gallons),
        "fuel_purchased_gallons": gallons(purchased),
        "fuel_remaining_at_destination_gallons": gallons(ledger.fuel_remaining_gallons),
        "total_fuel_cost": money(ledger.total_cost),
        "average_price_per_gallon": _q(ledger.total_cost / purchased, "0.001")
        if purchased
        else None,
        "currency": "USD",
    }

    route_meta = trip.route
    payload = {
        "start": _location(trip.start),
        "finish": _location(trip.finish),
        "route": {
            "distance_miles": miles(trip.path.length),
            "duration_hours": round(route_meta.duration_hours, 2),
            "geometry_mode": geometry_mode,
            "geometry_points": geometry_points,
            "geometry": geometry,
        },
        "vehicle": {
            "max_range_miles": cfg["MAX_RANGE_MILES"],
            "miles_per_gallon": cfg["MPG"],
            "tank_gallons": gallons(tank),
            "initial_fuel_gallons": gallons(ledger.initial_fuel_gallons),
        },
        "fuel_stops": stops,
        "legs": legs,
        "summary": summary,
        "assumptions": assumptions(trip),
        "meta": {
            "routing_provider": route_meta.provider,
            "routing_cache": trip.route_cache,
            "external_calls": {
                "routing": calls.get("routing", 0),
                "geocoding": calls.get("geocoding", 0),
            },
            "candidate_stations": len(trip.candidates),
            "max_detour_miles": trip.max_detour_miles,
            "elapsed_ms": 0,  # filled in below, once everything else is built
            "attribution": f"{route_meta.attribution} {data_attribution()}",
        },
    }
    if data_notice():
        payload["meta"]["data_notice"] = data_notice()
    payload["meta"]["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    return payload


def build_geojson(payload: dict) -> dict:
    """The same result as a GeoJSON FeatureCollection, ready for geojson.io, Leaflet or Mapbox."""
    features = []
    route = payload["route"]
    if route["geometry"] is not None:
        features.append(
            {
                "type": "Feature",
                "geometry": route["geometry"],
                "properties": {
                    "kind": "route",
                    "distance_miles": route["distance_miles"],
                    "duration_hours": route["duration_hours"],
                    **{k: payload["summary"][k] for k in ("fuel_stops", "total_fuel_cost")},
                },
            }
        )
    for kind in ("start", "finish"):
        place = payload[kind]
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [place["lon"], place["lat"]]},
                "properties": {
                    "kind": kind,
                    "label": place["label"],
                    "precision": place["precision"],
                },
            }
        )
    for stop in payload["fuel_stops"]:
        st = stop["station"]
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [st["lon"], st["lat"]]},
                "properties": {
                    "kind": "fuel_stop",
                    "sequence": stop["sequence"],
                    "name": st["name"],
                    "city": f"{st['city']}, {st['state']}",
                    "price_per_gallon": stop["price_per_gallon"],
                    "gallons_purchased": stop["gallons_purchased"],
                    "cost": stop["cost"],
                    "route_mile": stop["route_mile"],
                    "approximate_location": st["approximate_location"],
                },
            }
        )
    return {
        "type": "FeatureCollection",
        "features": features,
        "summary": payload["summary"],
        "attribution": payload["meta"]["attribution"],
    }
