"""Settings, entry points and small leftovers: behaviour a deployment depends on."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent


def run_settings(code: str, **env) -> subprocess.CompletedProcess:
    """Import the production settings in a clean subprocess with exactly this environment.

    Empty strings are used (rather than deleting variables) so that a developer's own ``.env``
    file - which python-dotenv never lets override existing variables - cannot interfere.
    """
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("DJANGO_", "FUEL_", "OSRM_"))}
    clean.update({"DJANGO_SECRET_KEY": "", "DJANGO_DEBUG": "false"})
    clean.update({k: str(v) for k, v in env.items()})
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=BASE_DIR,
        env=clean,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_production_refuses_to_start_without_a_secret_key():
    result = run_settings("import config.settings")
    assert result.returncode != 0
    assert "DJANGO_SECRET_KEY is not set" in result.stderr


def test_production_starts_with_a_secret_key_and_has_safe_defaults():
    result = run_settings(
        "import json, config.settings as s;"
        "print(json.dumps({'debug': s.DEBUG, 'key': s.SECRET_KEY,"
        " 'renderers': s.REST_FRAMEWORK['DEFAULT_RENDERER_CLASSES']}))",
        DJANGO_SECRET_KEY="a-real-secret",
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["debug"] is False and out["key"] == "a-real-secret"
    assert out["renderers"] == ["rest_framework.renderers.JSONRenderer"]  # no browsable API


def test_debug_mode_falls_back_to_a_development_key():
    result = run_settings(
        "import config.settings as s; print(s.SECRET_KEY, s.DEBUG)", DJANGO_DEBUG="true"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split()[0].startswith("insecure-development-key")
    assert result.stdout.split()[-1] == "True"


def test_environment_variables_configure_the_planner():
    result = run_settings(
        "import json, config.settings as s; c = s.FUEL_PLANNER;"
        "print(json.dumps({'csv': str(c['FUEL_CSV_PATH']), 'osrm': c['OSRM_BASE_URL'],"
        " 'timeout': c['OSRM_TIMEOUT_SECONDS'], 'hosts': s.ALLOWED_HOSTS,"
        " 'abs': str(c['GAZETTEER_PATH'])}))",
        DJANGO_SECRET_KEY="k",
        FUEL_CSV_PATH="somewhere/prices.csv",
        GAZETTEER_PATH=str(BASE_DIR / "elsewhere" / "places.csv.gz"),
        OSRM_BASE_URL="http://osrm.internal:5000/",
        OSRM_TIMEOUT_SECONDS="3.5",
        DJANGO_ALLOWED_HOSTS="api.example.com, localhost",
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert Path(out["csv"]) == BASE_DIR / "somewhere" / "prices.csv"  # relative -> project root
    assert Path(out["abs"]) == BASE_DIR / "elsewhere" / "places.csv.gz"  # absolute is kept
    assert out["osrm"] == "http://osrm.internal:5000"  # trailing slash removed
    assert out["timeout"] == 3.5
    assert out["hosts"] == ["api.example.com", "localhost"]


def test_vehicle_and_planning_defaults_match_the_assignment(settings):
    cfg = settings.FUEL_PLANNER
    assert cfg["MAX_RANGE_MILES"] == 500.0 and cfg["MPG"] == 10.0
    assert cfg["RESAMPLE_STEP_MILES"] <= cfg["MIN_ALLOWED_DETOUR_MILES"]  # corridors resolve


def test_the_test_settings_ignore_the_local_environment():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import config.settings_test as s; c = s.FUEL_PLANNER;"
            "print(c['OSRM_BASE_URL'], c['OSRM_MIN_INTERVAL_SECONDS'], s.DEBUG)",
        ],
        cwd=BASE_DIR,
        env={
            **os.environ,
            "OSRM_BASE_URL": "http://hostile.invalid",
            "DJANGO_DEBUG": "false",
            "OSRM_MIN_INTERVAL_SECONDS": "30",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["https://router.project-osrm.org", "0.0", "True"]


@pytest.mark.django_db
def test_wsgi_entry_point_serves_requests():
    from wsgiref.util import setup_testing_defaults

    from config.wsgi import application

    environ: dict = {}
    setup_testing_defaults(environ)
    environ.update(PATH_INFO="/api/v1/health/", REQUEST_METHOD="GET", HTTP_HOST="localhost")
    seen: dict = {}

    def start_response(status, headers, exc_info=None):
        seen["status"] = status
        seen["headers"] = dict(headers)

    body = b"".join(application(environ, start_response))
    assert seen["status"].startswith("503")  # no stations loaded in the test database
    assert seen["headers"]["Content-Type"].startswith("application/json")
    assert json.loads(body)["status"] == "unavailable"


@pytest.mark.django_db
def test_station_string_representation():
    from decimal import Decimal

    from fuelplanner.models import Station

    station = Station(name="PILOT #1", city="Tomah", state="WI", price=Decimal("3.299"))
    assert str(station) == "PILOT #1 (Tomah, WI) $3.299"


DEPLOY_SECRET = "deploy-check-secret-" + "x" * 50  # long and random-looking enough for W009


def test_a_production_configuration_passes_the_django_deploy_checklist():
    result = run_settings(
        "import django, os; os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings';"
        "django.setup(); from django.core.management import call_command;"
        "call_command('check', deploy=True, fail_level='WARNING')",
        DJANGO_SECRET_KEY=DEPLOY_SECRET,
        DJANGO_SECURE_SSL_REDIRECT="true",
        DJANGO_SECURE_HSTS_SECONDS="31536000",
        DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS="true",
        DJANGO_SECURE_HSTS_PRELOAD="true",
        DJANGO_BEHIND_HTTPS_PROXY="true",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no issues" in (result.stdout + result.stderr)


def test_https_settings_default_off_for_local_development():
    result = run_settings(
        "import config.settings as s;"
        "print(s.SECURE_SSL_REDIRECT, s.SECURE_HSTS_SECONDS,"
        " hasattr(s, 'SECURE_PROXY_SSL_HEADER'))",
        DJANGO_SECRET_KEY=DEPLOY_SECRET,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["False", "0", "False"]


@pytest.mark.django_db
def test_responses_carry_anti_framing_and_nosniff_headers(client):
    response = client.get("/api/v1/health/")
    assert response["X-Frame-Options"] == "DENY"
    assert response["X-Content-Type-Options"] == "nosniff"
