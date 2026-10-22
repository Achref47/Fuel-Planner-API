"""Application errors with a stable machine-readable ``code`` and HTTP status.

The API layer turns these into the structured JSON error format; services raise them
without knowing anything about HTTP.
"""

from __future__ import annotations


class PlannerError(Exception):
    code = "planner_error"
    http_status = 500

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class FuelDataUnavailableError(PlannerError):
    code = "fuel_data_unavailable"
    http_status = 503


class NoFeasiblePlanError(PlannerError):
    """The destination cannot be reached with the available stations and vehicle range."""

    code = "no_feasible_fuel_plan"
    http_status = 422


class PlanInvariantError(PlannerError):
    """A computed plan failed its own consistency checks (a bug, never user error)."""

    code = "plan_invariant_violation"
    http_status = 500


class LocationNotFoundError(PlannerError):
    code = "location_not_found"
    http_status = 422


class LocationOutsideUSAError(PlannerError):
    code = "location_outside_usa"
    http_status = 422


class LocationDataUnavailableError(PlannerError):
    """The offline place table is missing, so city / ZIP lookups cannot be served."""

    code = "location_data_unavailable"
    http_status = 503


class RouteNotFoundError(PlannerError):
    code = "route_not_found"
    http_status = 422


class RoutingProviderError(PlannerError):
    """The routing service answered, but not with a usable route."""

    code = "routing_provider_error"
    http_status = 502


class RoutingProviderUnavailableError(PlannerError):
    """Routing service is down, rate limiting us, or unreachable."""

    code = "routing_provider_unavailable"
    http_status = 503


class RoutingProviderTimeoutError(PlannerError):
    code = "routing_provider_timeout"
    http_status = 504


class GeocodingProviderError(PlannerError):
    """The street-address geocoder failed (timeout, outage or bad response)."""

    code = "geocoding_provider_error"
    http_status = 502


class SameLocationError(PlannerError):
    code = "same_location"
    http_status = 400
