"""Settings for the automated test suite.

Deterministic and independent of any local ``.env`` file or shell environment, so the same
tests behave identically on a reviewer's machine, in CI, and here. Network access is never
needed: tests mock every external service.
"""

import os

# Set before the base settings are imported (python-dotenv never overrides existing values).
os.environ["DJANGO_DEBUG"] = "true"
os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key")

from .settings import *  # noqa: E402, F403
from .settings import FUEL_PLANNER as _BASE  # noqa: E402

FUEL_PLANNER = {
    **_BASE,
    "ROUTING_PROVIDER": "osrm",
    "OSRM_BASE_URL": "https://router.project-osrm.org",
    "OSRM_PROFILE": "driving",
    "OSRM_TIMEOUT_SECONDS": 15.0,
    "OSRM_MIN_INTERVAL_SECONDS": 0.0,
    "HTTP_USER_AGENT": "spotter-fuel-route-assessment/1.0 (automated tests)",
    "CENSUS_GEOCODER_URL": "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress",
    "ROUTE_CACHE_SECONDS": 24 * 3600,
    "GEOCODE_CACHE_SECONDS": 30 * 24 * 3600,
    "DATA_NOTICE": "",  # tests that need a notice set it themselves
}

CACHES = {"default": {**CACHES["default"], "LOCATION": "fuelplanner-tests"}}  # noqa: F405

REST_FRAMEWORK = {
    **REST_FRAMEWORK,  # noqa: F405
    "DEFAULT_THROTTLE_RATES": {"anon": "100000/min"},  # throttling has its own test
}

LOGGING["loggers"]["fuelplanner"]["level"] = "INFO"  # noqa: F405
