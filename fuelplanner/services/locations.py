"""Turn user input into coordinates, mostly without any network call.

Accepted forms (all within the contiguous United States):

* ``{"lat": 41.88, "lon": -87.63}`` or the string ``"41.88,-87.63"``
* a 5-digit ZIP code (``"60601"`` or ``"60601-1234"``)       -> ZIP centroid, offline
* ``"City, ST"`` / ``"City, State"`` / ``"City ST"``           -> city centroid, offline
* a street address with a house number                          -> Census Geocoder (one call)

City and ZIP centroids are approximate by nature; the response says which kind of
location was used (``precision``) so clients can tell.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass

from ..errors import LocationDataUnavailableError, LocationNotFoundError, LocationOutsideUSAError
from .gazetteer import GazetteerNotFoundError, Place, get_gazetteer
from .geocoding import geocode_address
from .states import split_trailing_state, state_code

logger = logging.getLogger(__name__)

# Contiguous United States bounding box: lat_min, lat_max, lon_min, lon_max.
CONUS_BOUNDS = (24.3, 49.7, -125.1, -66.7)
# A coordinate in the box must also be near a known U.S. place; this rejects open ocean and
# most of Canada / Mexico / the Gulf. (A coarse check - not a border survey.)
MAX_DISTANCE_TO_PLACE_MILES = 75.0

_COORD_RE = re.compile(r"^([-+]?\d{1,3}(?:\.\d+)?)\s*[,;\s]\s*([-+]?\d{1,3}(?:\.\d+)?)$")
_ZIP_RE = re.compile(r"^(\d{5})(?:-\d{4})?$")
_TRAILING_ZIP_RE = re.compile(r"\s*\b\d{5}(?:-\d{4})?$")
_HOUSE_NUMBER_RE = re.compile(r"^\d+[A-Za-z]?\s+\S")
_COUNTRY_NAMES = {"usa", "us", "u.s.", "u.s.a.", "united states", "united states of america"}

HINT = "Use 'City, ST', a 5-digit ZIP code, 'lat,lon' or a full street address."


@dataclass(frozen=True)
class ResolvedLocation:
    query: str
    label: str
    lat: float
    lon: float
    resolved_by: str  # coordinates | gazetteer_city | gazetteer_zip | census_geocoder
    precision: str  # exact | city | zip_centroid | address


def resolve_location(value) -> ResolvedLocation:
    """Resolve one user-supplied location, raising a ``PlannerError`` subclass on failure."""
    if isinstance(value, Mapping):
        return _from_mapping(value)
    if not isinstance(value, str):
        raise LocationNotFoundError(
            "A location must be a text string or an object with 'lat' and 'lon'. " + HINT
        )
    text = " ".join(value.split())
    if not text:
        raise LocationNotFoundError("The location is empty. " + HINT)

    if match := _COORD_RE.match(text):
        return _from_coordinates(float(match[1]), float(match[2]), text)
    if match := _ZIP_RE.match(text):
        return _from_zip(match[1], text)
    return _from_text(text)


# --- coordinates ---------------------------------------------------------------------------


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _from_mapping(value: Mapping) -> ResolvedLocation:
    lat = next((_number(value[k]) for k in ("lat", "latitude") if k in value), None)
    lon = next((_number(value[k]) for k in ("lon", "lng", "longitude") if k in value), None)
    if lat is None or lon is None:
        raise LocationNotFoundError("A coordinate object needs numeric 'lat' and 'lon' values.")
    return _from_coordinates(lat, lon, f"{lat},{lon}")


def _from_coordinates(lat: float, lon: float, query: str) -> ResolvedLocation:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise LocationNotFoundError(
            f"({lat}, {lon}) is not a valid coordinate (latitude -90..90, longitude -180..180)."
        )
    ensure_in_us(lat, lon, check_places=True)
    return ResolvedLocation(query, f"{lat:.5f}, {lon:.5f}", lat, lon, "coordinates", "exact")


def ensure_in_us(lat: float, lon: float, *, check_places: bool) -> None:
    lat_min, lat_max, lon_min, lon_max = CONUS_BOUNDS
    outside = LocationOutsideUSAError(
        "Only locations in the contiguous United States are supported "
        f"(({lat:.4f}, {lon:.4f}) is outside it).",
        {"lat": round(lat, 5), "lon": round(lon, 5)},
    )
    if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
        raise outside
    if check_places:
        try:
            distance = _gazetteer().nearest_place_miles(lat, lon)
        except LocationDataUnavailableError:
            return  # bounding box only
        if distance is not None and distance > MAX_DISTANCE_TO_PLACE_MILES:
            raise outside


# --- offline lookups ---------------------------------------------------------------------


def _gazetteer():
    try:
        return get_gazetteer()
    except GazetteerNotFoundError as exc:
        logger.error("gazetteer unavailable: %s", exc)
        raise LocationDataUnavailableError(
            "City and ZIP lookups are unavailable because the place table is missing. "
            "Rebuild it with `python manage.py build_gazetteer`, or use coordinates / a street "
            "address."
        ) from exc


def _pretty(city: str) -> str:
    return city.title() if city.islower() or city.isupper() else city


def _from_zip(zip_code: str, query: str) -> ResolvedLocation:
    place = _gazetteer().find_zip(zip_code)
    if place is None:
        raise LocationNotFoundError(f"ZIP code {zip_code} was not found.", {"input": query})
    ensure_in_us(place.lat, place.lon, check_places=False)
    return ResolvedLocation(
        query, f"ZIP {zip_code}", place.lat, place.lon, "gazetteer_zip", "zip_centroid"
    )


def _from_city(city: str, code: str, query: str) -> ResolvedLocation:
    place: Place | None = _gazetteer().find_city(city, code)
    if place is None:
        raise LocationNotFoundError(
            f"No place named '{city}' was found in {code}. " + HINT, {"input": query}
        )
    ensure_in_us(place.lat, place.lon, check_places=False)
    return ResolvedLocation(
        query, f"{_pretty(city)}, {code}", place.lat, place.lon, "gazetteer_city", "city"
    )


# --- free text ---------------------------------------------------------------------------


def _from_text(text: str) -> ResolvedLocation:
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if parts and parts[-1].lower() in _COUNTRY_NAMES:
        parts.pop()
    if not parts:
        raise LocationNotFoundError("The location is empty. " + HINT)

    if len(parts) >= 2 and _HOUSE_NUMBER_RE.match(parts[0]):
        return _from_address(text)

    if len(parts) >= 2:
        city, state_text = parts[-2], _TRAILING_ZIP_RE.sub("", parts[-1])
        if code := state_code(state_text):
            return _from_city(city, code, text)
    elif split := split_trailing_state(_TRAILING_ZIP_RE.sub("", parts[0])):
        return _from_city(split[0], split[1], text)

    raise LocationNotFoundError(
        f"Could not understand the location '{text}'. " + HINT, {"input": text}
    )


def _from_address(text: str) -> ResolvedLocation:
    result = geocode_address(text)
    if result is None:
        raise LocationNotFoundError(
            f"The address '{text}' was not found by the geocoder. Check it, or use 'City, ST'.",
            {"input": text},
        )
    ensure_in_us(result.lat, result.lon, check_places=False)
    return ResolvedLocation(
        text, result.matched_address, result.lat, result.lon, "census_geocoder", "address"
    )
