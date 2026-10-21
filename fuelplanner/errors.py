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
