"""Helpers for building test journeys and stations along a route."""

import re
from decimal import Decimal

import numpy as np

from fuelplanner.models import Station
from fuelplanner.services.geometry import resample_route

MILES_PER_DEGREE = 69.093


def point_beside_route(path, mile, off=0.0):
    """(lat, lon) of the point ``mile`` miles along ``path``, ``off`` miles to its side."""
    lat = float(np.interp(mile, path.miles, path.lat))
    lon = float(np.interp(mile, path.miles, path.lon))
    if off:
        ahead_lat = float(np.interp(mile + 5, path.miles, path.lat))
        ahead_lon = float(np.interp(mile + 5, path.miles, path.lon))
        cos_lat = np.cos(np.radians(lat))
        dy = (ahead_lat - lat) * MILES_PER_DEGREE
        dx = (ahead_lon - lon) * MILES_PER_DEGREE * cos_lat
        norm = np.hypot(dx, dy)
        # Unit vector perpendicular to the direction of travel (miles east, miles north).
        px, py = -dy / norm, dx / norm
        lat += off * py / MILES_PER_DEGREE
        lon += off * px / (MILES_PER_DEGREE * cos_lat)
    return lat, lon


def station_object(opis_id, lat, lon, price, name=None, state="IL"):
    return Station(
        opis_id=opis_id,
        name=name or f"STATION {opis_id}",
        address="I-1, EXIT 1",
        city="Testville",
        state=state,
        price=Decimal(str(price)),
        latitude=lat,
        longitude=lon,
        location_source="place",
    )


def add_station(opis_id, coords, distance, mile, price, off=0.0, name=None):
    """Create a station ``mile`` miles along the mocked route, ``off`` miles to its side."""
    path = resample_route(coords, 0.5, total_miles=distance)
    lat, lon = point_beside_route(path, mile, off)
    station = station_object(opis_id, lat, lon, price, name)
    station.save()
    return station


# --- API journey helpers shared by the API-level test modules -----------------------------

OSRM_URL = re.compile(r"https://router\.project-osrm\.org/route/v1/driving/.*")
CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
URL = "/api/v1/fuel-route/"

# City centroids from tests/fixtures/sample_places.csv, as [lon, lat].
CHICAGO = (-87.6847, 41.8369)
DALLAS = (-96.7667, 32.7923)
HOUSTON = (-95.3935, 29.7872)


def line(a, b, n=400):
    return np.column_stack((np.linspace(a[0], b[0], n), np.linspace(a[1], b[1], n)))


def osrm_response(coords, distance_miles):
    return {
        "code": "Ok",
        "routes": [
            {
                "distance": distance_miles * 1609.344,
                "duration": distance_miles / 60 * 3600,
                "geometry": {"type": "LineString", "coordinates": coords.tolist()},
            }
        ],
        "waypoints": [{"distance": 100.0}, {"distance": 50.0}],
    }


def mock_route(http, a=CHICAGO, b=DALLAS, distance=970.0, n=400):
    """Make the mocked OSRM answer with a straight line ``a -> b`` of the given road distance."""
    coords = line(a, b, n)
    http.get(OSRM_URL, json=osrm_response(coords, distance))
    return coords


def seed_chicago_dallas(coords, distance=970.0):
    """Four stations; the cheapest feasible plan is B (388 mi) then D (776 mi)."""
    add_station(1, coords, distance, 194, "3.50")
    add_station(2, coords, distance, 388, "3.00")
    add_station(3, coords, distance, 582, "3.40")
    add_station(4, coords, distance, 776, "2.90")


def post(client, start="Chicago, IL", finish="Dallas, TX", options=None, query=""):
    body = {"start": start, "finish": finish}
    if options is not None:
        body["options"] = options
    return client.post(URL + query, body, format="json")


def error(response):
    """The ``error`` object of a response, after checking it has the documented shape."""
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    return body["error"]
