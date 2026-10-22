"""Django settings for the fuel route planner.

Everything environment-specific is read from environment variables (optionally
loaded from a local, git-ignored ``.env`` file). See ``.env.example``.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_path(name: str, default: str) -> Path:
    """A path from the environment, resolved relative to the project root."""
    path = Path(os.environ.get(name, default))
    return path if path.is_absolute() else BASE_DIR / path


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


DEBUG = env_bool("DJANGO_DEBUG", False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG:
        # Convenience for local development only; never used when DEBUG is off.
        SECRET_KEY = "insecure-development-key-do-not-use-in-production"
    else:
        from django.core.exceptions import ImproperlyConfigured

        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY is not set. Copy .env.example to .env and set it "
            "(or set DJANGO_DEBUG=true for local development)."
        )

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "rest_framework",
    "fuelplanner",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# HTTPS hardening for deployments (off by default because local development is plain HTTP).
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", False)
SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_SECURE_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", False)
SECURE_HSTS_PRELOAD = env_bool("DJANGO_SECURE_HSTS_PRELOAD", False)
if env_bool("DJANGO_BEHIND_HTTPS_PROXY", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# CSRF protection guards cookie-authenticated browser sessions. This API sets no cookies and has
# no authentication (DRF views are CSRF-exempt without SessionAuthentication), so the
# middleware would protect nothing; the deploy check for it is silenced deliberately.
SILENCED_SYSTEM_CHECKS = ["security.W003"]

ROOT_URLCONF = "config.urls"
APPEND_SLASH = (
    False  # the API routes accept both /x and /x/ themselves (a redirect would drop a POST body)
)
WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / os.environ.get("DATABASE_FILE", "db.sqlite3"),
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

# In-process cache for routes and address look-ups. A cached coast-to-coast route is ~0.5 MB,
# so the entry cap bounds memory; use Redis / Memcached / a file cache for a multi-process or
# larger deployment (change BACKEND here - the code only uses django.core.cache).
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "fuelplanner-cache",
        "OPTIONS": {"MAX_ENTRIES": int(os.environ.get("CACHE_MAX_ENTRIES", "200"))},
    }
}

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"]
    + (["rest_framework.renderers.BrowsableAPIRenderer"] if DEBUG else []),
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    # The API is public and stateless for this assessment: no sessions, no auth.
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
    "UNAUTHENTICATED_USER": None,
    # Responses are always JSON; `?format=geojson` is handled by the view, not content negotiation.
    "URL_FORMAT_OVERRIDE": None,
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"anon": os.environ.get("THROTTLE_RATE", "120/min")},
    "EXCEPTION_HANDLER": "fuelplanner.api.exceptions.api_exception_handler",
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "loggers": {
        "fuelplanner": {
            "handlers": ["console"],
            "level": os.environ.get("LOG_LEVEL", "INFO"),
            "propagate": False,
        },
    },
}

# --- Fuel planner configuration -------------------------------------------------

FUEL_PLANNER = {
    # Vehicle model fixed by the assignment.
    "MAX_RANGE_MILES": 500.0,
    "MPG": 10.0,
    # Planning defaults (overridable per request within the validated bounds).
    "DEFAULT_MAX_DETOUR_MILES": 5.0,
    "MIN_ALLOWED_DETOUR_MILES": 0.5,  # one resample step: smaller corridors are not reliable
    "MAX_ALLOWED_DETOUR_MILES": 25.0,
    # Tolerance used when simplifying the route geometry returned to clients.
    "GEOMETRY_TOLERANCE_MILES": 0.05,
    # Route resampling step used for station matching.
    "RESAMPLE_STEP_MILES": 0.5,
    # Dataset locations. The real CSV is supplied by the assessor and is not
    # redistributed in the repository (see README).
    "FUEL_CSV_PATH": env_path("FUEL_CSV_PATH", "data/fuel-prices-for-be-assessment.csv"),
    "GAZETTEER_PATH": env_path("GAZETTEER_PATH", "data/us_places.csv.gz"),
    # Routing provider (OSRM-compatible HTTP API). Replace with a self-hosted
    # instance for production use; the public demo server is best-effort only.
    "ROUTING_PROVIDER": os.environ.get("ROUTING_PROVIDER", "osrm"),
    "OSRM_BASE_URL": os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org").rstrip("/"),
    "OSRM_PROFILE": os.environ.get("OSRM_PROFILE", "driving"),
    "OSRM_TIMEOUT_SECONDS": float(os.environ.get("OSRM_TIMEOUT_SECONDS", "15")),
    "OSRM_MIN_INTERVAL_SECONDS": float(os.environ.get("OSRM_MIN_INTERVAL_SECONDS", "1.0")),
    "HTTP_USER_AGENT": os.environ.get(
        "HTTP_USER_AGENT", "spotter-fuel-route-assessment/1.0 (+https://github.com/)"
    ),
    # Caching.
    "ROUTE_CACHE_SECONDS": int(os.environ.get("ROUTE_CACHE_SECONDS", str(24 * 3600))),
    "GEOCODE_CACHE_SECONDS": int(os.environ.get("GEOCODE_CACHE_SECONDS", str(30 * 24 * 3600))),
    # Street-address lookups only (city/ZIP/coordinates are resolved offline).
    "CENSUS_GEOCODER_URL": os.environ.get(
        "CENSUS_GEOCODER_URL", "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
    ),
    "CENSUS_TIMEOUT_SECONDS": float(os.environ.get("CENSUS_TIMEOUT_SECONDS", "10")),
}
