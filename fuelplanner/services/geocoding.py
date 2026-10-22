"""Street-address geocoding through the U.S. Census Bureau Geocoder.

Only used for inputs that are real street addresses (``"1600 Pennsylvania Ave NW, ..."``).
Cities, ZIP codes and coordinates are resolved offline, so a typical request makes no
geocoding call at all. The service needs no API key. Results (including "no match") are
cached so the same address is never looked up twice.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

from ..errors import GeocodingProviderError
from . import callcount

logger = logging.getLogger(__name__)

BENCHMARK = "Public_AR_Current"
NEGATIVE_CACHE_SECONDS = 24 * 3600


@dataclass(frozen=True)
class GeocodeResult:
    lat: float
    lon: float
    matched_address: str


def _cache_key(address: str) -> str:
    digest = hashlib.sha256(" ".join(address.lower().split()).encode()).hexdigest()
    return f"fuelplanner:geocode:{digest}"


def geocode_address(address: str, *, session: requests.Session | None = None):
    """Return a ``GeocodeResult`` or ``None`` when the Census finds no match.

    Raises ``GeocodingProviderError`` if the service times out, fails or returns garbage.
    """
    cfg = settings.FUEL_PLANNER
    key = _cache_key(address)
    cached = cache.get(key)
    if cached is not None:
        return None if cached == "no-match" else GeocodeResult(**cached)

    http = session or requests
    callcount.record("geocoding")
    short = key[-8:]  # identify the lookup in logs without recording the address itself
    try:
        response = http.get(
            cfg["CENSUS_GEOCODER_URL"],
            params={"address": address, "benchmark": BENCHMARK, "format": "json"},
            headers={"User-Agent": cfg["HTTP_USER_AGENT"]},
            timeout=cfg["CENSUS_TIMEOUT_SECONDS"],
        )
    except requests.Timeout as exc:
        logger.warning("census geocode %s timed out", short)
        raise GeocodingProviderError(
            "The address geocoder timed out. Try again, or give the location as 'City, ST'.",
            {"provider": "census", "reason": "timeout"},
        ) from exc
    except requests.RequestException as exc:
        logger.warning("census geocode %s failed: %s", short, type(exc).__name__)
        raise GeocodingProviderError(
            "The address geocoder is unreachable. Try again, or give the location as 'City, ST'.",
            {"provider": "census", "reason": "connection_error"},
        ) from exc

    if response.status_code != 200:
        logger.warning("census geocode %s returned HTTP %s", short, response.status_code)
        raise GeocodingProviderError(
            f"The address geocoder returned HTTP {response.status_code}.",
            {"provider": "census", "reason": "http_error", "status": response.status_code},
        )
    try:
        matches = response.json()["result"]["addressMatches"]
    except (ValueError, KeyError, TypeError) as exc:
        raise GeocodingProviderError(
            "The address geocoder returned an unreadable response.",
            {"provider": "census", "reason": "bad_response"},
        ) from exc

    if not matches:
        cache.set(key, "no-match", min(cfg["GEOCODE_CACHE_SECONDS"], NEGATIVE_CACHE_SECONDS))
        logger.info("census geocode %s: no match", short)
        return None
    try:
        best = matches[0]
        result = GeocodeResult(
            lat=float(best["coordinates"]["y"]),
            lon=float(best["coordinates"]["x"]),
            matched_address=str(best.get("matchedAddress", address)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GeocodingProviderError(
            "The address geocoder returned an unreadable match.",
            {"provider": "census", "reason": "bad_response"},
        ) from exc
    cache.set(key, result.__dict__, cfg["GEOCODE_CACHE_SECONDS"])
    logger.info("census geocode %s: matched", short)
    return result
