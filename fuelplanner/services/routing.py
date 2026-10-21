"""Driving routes from a replaceable provider (OSRM by default), cached and rate limited.

One uncached journey costs exactly **one** routing request (full geometry in a single call).
Identical journeys are served from the cache without touching the provider. Concurrent
requests for the same journey are collapsed into a single upstream call.

The public OSRM demo server (``router.project-osrm.org``) is best effort with a limit of one
request per second and no uptime guarantee; this module therefore throttles outgoing calls,
identifies itself with a User-Agent, and maps every failure to a specific error. Point
``OSRM_BASE_URL`` at a self-hosted instance (or set ``ROUTING_PROVIDER`` to another provider
class) for real traffic.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np
import requests
from django.conf import settings
from django.core.cache import cache
from django.utils.module_loading import import_string

from ..errors import (
    RouteNotFoundError,
    RoutingProviderError,
    RoutingProviderTimeoutError,
    RoutingProviderUnavailableError,
)

logger = logging.getLogger(__name__)

METERS_PER_MILE = 1609.344
SECONDS_PER_HOUR = 3600.0

LatLon = tuple[float, float]


@dataclass(frozen=True)
class RouteResult:
    distance_miles: float
    duration_hours: float
    coordinates: np.ndarray  # shape (n, 2), [lon, lat] along the road
    provider: str
    attribution: str
    start_snap_miles: float | None = None  # how far the start was moved onto the road network
    end_snap_miles: float | None = None


@dataclass(frozen=True)
class RouteLookup:
    route: RouteResult
    cache_status: str  # "hit" or "miss"


class RoutingProvider(ABC):
    """Anything that can return one full driving route between two points."""

    name: str = "provider"
    attribution: str = ""

    @abstractmethod
    def route(self, start: LatLon, end: LatLon) -> RouteResult:
        """Route between two ``(lat, lon)`` points; raise a ``PlannerError`` on failure."""


class RequestThrottle:
    """Keeps at least ``min_interval`` seconds between outgoing requests (thread safe)."""

    def __init__(self, min_interval: float, clock=time.monotonic, sleep=time.sleep):
        self.min_interval = min_interval
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()
        self._last: float | None = None

    def wait(self) -> float:
        """Block until a request may be sent; return the seconds spent waiting."""
        with self._lock:
            waited = 0.0
            if self._last is not None and self.min_interval > 0:
                remaining = self.min_interval - (self._clock() - self._last)
                if remaining > 0:
                    self._sleep(remaining)
                    waited = remaining
            self._last = self._clock()
            return waited


_throttles: dict[str, RequestThrottle] = {}
_throttles_lock = threading.Lock()


def _throttle_for(base_url: str, min_interval: float) -> RequestThrottle:
    with _throttles_lock:
        throttle = _throttles.get(base_url)
        if throttle is None or throttle.min_interval != min_interval:
            throttle = _throttles[base_url] = RequestThrottle(min_interval)
        return throttle


class OSRMProvider(RoutingProvider):
    """Client for the OSRM ``route`` service (public demo or self-hosted)."""

    name = "osrm"
    attribution = (
        "Routing by OSRM (project-osrm.org). Map data © OpenStreetMap contributors (ODbL)."
    )

    def __init__(self, config: dict | None = None, session: requests.Session | None = None):
        cfg = config or settings.FUEL_PLANNER
        self.base_url = cfg["OSRM_BASE_URL"].rstrip("/")
        self.profile = cfg["OSRM_PROFILE"]
        self.timeout = cfg["OSRM_TIMEOUT_SECONDS"]
        self.user_agent = cfg["HTTP_USER_AGENT"]
        self.throttle = _throttle_for(self.base_url, cfg["OSRM_MIN_INTERVAL_SECONDS"])
        self.session = session or requests.Session()

    def route(self, start: LatLon, end: LatLon) -> RouteResult:
        coordinates = f"{start[1]:.6f},{start[0]:.6f};{end[1]:.6f},{end[0]:.6f}"
        url = f"{self.base_url}/route/v1/{self.profile}/{coordinates}"
        params = {"overview": "full", "geometries": "geojson", "steps": "false"}

        self.throttle.wait()
        started = time.perf_counter()
        try:
            response = self.session.get(
                url, params=params, headers={"User-Agent": self.user_agent}, timeout=self.timeout
            )
        except requests.Timeout as exc:
            logger.warning("routing request timed out after %ss", self.timeout)
            raise RoutingProviderTimeoutError(
                f"The routing service did not answer within {self.timeout:g} seconds. "
                "Try again shortly.",
                {"provider": self.name, "timeout_seconds": self.timeout},
            ) from exc
        except requests.RequestException as exc:
            logger.warning("routing service unreachable: %s", type(exc).__name__)
            raise RoutingProviderUnavailableError(
                "The routing service is unreachable. Try again shortly.",
                {"provider": self.name, "reason": type(exc).__name__},
            ) from exc
        elapsed_ms = (time.perf_counter() - started) * 1000

        result = self._parse(response)
        logger.info(
            "routing ok provider=%s status=%s %.0f ms distance=%.1f mi points=%d",
            self.name,
            response.status_code,
            elapsed_ms,
            result.distance_miles,
            len(result.coordinates),
        )
        return result

    def _parse(self, response: requests.Response) -> RouteResult:
        status = response.status_code
        if status in (429, 502, 503, 504):
            retry_after = response.headers.get("Retry-After")
            logger.warning("routing service busy: HTTP %s", status)
            details = {"provider": self.name, "status": status}
            if retry_after:
                details["retry_after_seconds"] = retry_after
            message = (
                "The routing service is rate limiting requests. Try again shortly."
                if status == 429
                else "The routing service is temporarily unavailable. Try again shortly."
            )
            raise RoutingProviderUnavailableError(message, details)

        try:
            payload = response.json()
            code = payload["code"]
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("routing service returned an unreadable response (HTTP %s)", status)
            raise RoutingProviderError(
                f"The routing service returned an unreadable response (HTTP {status}).",
                {"provider": self.name, "status": status},
            ) from exc

        if code in ("NoRoute", "NoSegment"):
            message = (
                "No driving route exists between these locations."
                if code == "NoRoute"
                else "One of the locations could not be matched to a road. Try a nearby town."
            )
            raise RouteNotFoundError(message, {"provider": self.name, "provider_code": code})
        if code != "Ok" or status != 200:
            logger.warning("routing service error code=%s HTTP %s", code, status)
            raise RoutingProviderError(
                f"The routing service reported an error ({code}).",
                {"provider": self.name, "provider_code": code, "status": status},
            )

        try:
            route = payload["routes"][0]
            coords = np.asarray(route["geometry"]["coordinates"], dtype=float)
            distance = float(route["distance"]) / METERS_PER_MILE
            duration = float(route["duration"]) / SECONDS_PER_HOUR
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RoutingProviderError(
                "The routing service returned a route in an unexpected format.",
                {"provider": self.name},
            ) from exc
        if coords.ndim != 2 or coords.shape[1] != 2 or len(coords) < 2:
            raise RoutingProviderError(
                "The routing service returned a route without usable geometry.",
                {"provider": self.name},
            )
        if not (np.isfinite(coords).all() and np.isfinite(distance) and distance >= 0):
            raise RoutingProviderError(
                "The routing service returned invalid numbers.", {"provider": self.name}
            )

        snaps = [
            _miles(w.get("distance")) for w in payload.get("waypoints", []) if isinstance(w, dict)
        ]
        return RouteResult(
            distance_miles=distance,
            duration_hours=duration,
            coordinates=coords,
            provider=self.name,
            attribution=self.attribution,
            start_snap_miles=snaps[0] if len(snaps) > 0 else None,
            end_snap_miles=snaps[-1] if len(snaps) > 1 else None,
        )


def _miles(meters) -> float | None:
    try:
        return float(meters) / METERS_PER_MILE
    except (TypeError, ValueError):
        return None


# --- provider selection ----------------------------------------------------------------------

_providers: dict[tuple, RoutingProvider] = {}
_providers_lock = threading.Lock()


def get_routing_provider() -> RoutingProvider:
    """The configured provider: ``ROUTING_PROVIDER`` is ``"osrm"`` or a dotted class path."""
    cfg = settings.FUEL_PLANNER
    name = cfg["ROUTING_PROVIDER"]
    key = (name, cfg["OSRM_BASE_URL"], cfg["OSRM_PROFILE"], cfg["OSRM_TIMEOUT_SECONDS"])
    with _providers_lock:
        provider = _providers.get(key)
        if provider is None:
            factory = OSRMProvider if name == "osrm" else import_string(name)
            provider = _providers[key] = factory(cfg)
        return provider


def reset_routing_provider() -> None:
    with _providers_lock:
        _providers.clear()
    with _throttles_lock:
        _throttles.clear()


# --- cached lookup ---------------------------------------------------------------------------

_STRIPES = [threading.Lock() for _ in range(64)]


def _cache_key(provider: RoutingProvider, start: LatLon, end: LatLon) -> str:
    raw = (
        f"{provider.name}|{getattr(provider, 'profile', '')}|"
        f"{start[0]:.5f},{start[1]:.5f}|{end[0]:.5f},{end[1]:.5f}"
    )
    return "fuelplanner:route:" + hashlib.sha256(raw.encode()).hexdigest()


def get_route(start: LatLon, end: LatLon) -> RouteLookup:
    """Route between two ``(lat, lon)`` points: from the cache, else one provider call.

    Failures are never cached. Coordinates are rounded to 5 decimals (~1 m) for the cache key.
    """
    provider = get_routing_provider()
    key = _cache_key(provider, start, end)
    cached = cache.get(key)
    if cached is not None:
        logger.info("route cache hit %s", key[-8:])
        return RouteLookup(cached, "hit")

    with _STRIPES[int(key[-4:], 16) % len(_STRIPES)]:  # collapse concurrent identical misses
        cached = cache.get(key)
        if cached is not None:
            return RouteLookup(cached, "hit")
        route = provider.route(start, end)
        cache.set(key, route, settings.FUEL_PLANNER["ROUTE_CACHE_SECONDS"])
        logger.info("route cache miss %s (stored)", key[-8:])
        return RouteLookup(route, "miss")
