"""Synthetic demo data, the Render deployment files, and production-safe settings."""

import csv
import io
import json
import re
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework.test import APIClient

from fuelplanner.models import Station
from fuelplanner.services.gazetteer import Gazetteer
from fuelplanner.services.stations import reset_station_index

from .helpers import OSRM_URL, error, post
from .test_configuration import run_settings

ROOT = Path(django_settings.BASE_DIR)
DEMO_CSV = ROOT / "data" / "demo" / "synthetic_stations.csv"
REAL_GAZETTEER = ROOT / "data" / "us_places.csv.gz"
OSRM_FIXTURES = ROOT / "tests" / "fixtures" / "osrm"
NOTICE = "PUBLIC DEMO: all fuel stations and prices are SYNTHETIC."

JOURNEYS = [  # (recorded route, start, finish)
    ("chicago_dallas", "Chicago, IL", "Dallas, TX"),
    ("new_york_los_angeles", "New York, NY", "Los Angeles, CA"),
    ("seattle_san_diego", "Seattle, WA", "San Diego, CA"),
    ("miami_new_york", "Miami, FL", "New York, NY"),
    ("atlanta_denver", "Atlanta, GA", "Denver, CO"),
    ("minneapolis_houston", "Minneapolis, MN", "Houston, TX"),
    ("boston_chicago", "Boston, MA", "Chicago, IL"),
]


def demo_rows():
    with open(DEMO_CSV, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture
def real_places(settings):
    settings.FUEL_PLANNER = {**settings.FUEL_PLANNER, "GAZETTEER_PATH": REAL_GAZETTEER}


def load_demo():
    out = io.StringIO()
    call_command("load_demo_data", stdout=out)
    reset_station_index()
    return out.getvalue()


# --- the dataset file ----------------------------------------


def test_demo_dataset_is_obviously_synthetic():
    rows = demo_rows()
    assert 80 <= len(rows) <= 400
    assert all(r["Truckstop Name"].startswith("DEMO FUEL STOP #") for r in rows)
    assert all(r["Address"] == "DEMO HWY, EXIT 0" for r in rows)
    assert all(int(r["OPIS Truckstop ID"]) > 900_000 for r in rows)  # real ids are far lower
    prices = [float(r["Retail Price"]) for r in rows]
    assert min(prices) > 2.5 and max(prices) < 4.6
    assert len({r["OPIS Truckstop ID"] for r in rows}) == len(rows)
    assert len({r["State"] for r in rows}) >= 25  # nationwide, not one corridor


def test_every_demo_station_can_be_placed_on_the_map():
    from fuelplanner.importer import clean_and_geocode, read_rows

    stations, report = clean_and_geocode(read_rows(DEMO_CSV), Gazetteer.from_file(REAL_GAZETTEER))
    assert report.matched_stations == len(demo_rows()) and not report.unmatched
    assert not report.rejected_rows


def test_the_demo_file_is_tracked_not_ignored_and_the_private_csv_is_ignored():
    import subprocess

    def ignored(path):
        return subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode == 0

    assert not ignored("data/demo/synthetic_stations.csv")
    assert ignored("data/fuel-prices-for-be-assessment.csv")
    assert ignored("db.sqlite3") and ignored(".env")


def test_the_demo_file_shares_no_row_with_the_private_dataset():
    private = Path(django_settings.FUEL_PLANNER["FUEL_CSV_PATH"])
    if not private.exists():
        pytest.skip("private CSV not present (nothing to compare against)")
    with open(private, encoding="utf-8-sig", newline="") as handle:
        real_ids = {r["OPIS Truckstop ID"].strip() for r in csv.DictReader(handle)}
    assert not real_ids & {r["OPIS Truckstop ID"] for r in demo_rows()}


# --- the load_demo_data command ----------------------------------------


@pytest.mark.django_db
def test_load_demo_data_creates_stations_and_is_repeatable(real_places):
    output = load_demo()
    count = Station.objects.count()
    assert count == len(demo_rows()) and "SYNTHETIC" in output
    load_demo()
    load_demo()
    assert Station.objects.count() == count  # no duplicates however often it runs
    assert not Station.objects.filter(opis_id__lte=900_000).exists()


@pytest.mark.django_db
def test_load_demo_data_never_reads_the_private_csv_path(real_places, settings, tmp_path):
    settings.FUEL_PLANNER = {**settings.FUEL_PLANNER, "FUEL_CSV_PATH": tmp_path / "private.csv"}
    (tmp_path / "private.csv").write_text("this must never be read", encoding="utf-8")
    load_demo()
    assert Station.objects.count() == len(demo_rows())


@pytest.mark.django_db
def test_load_demo_data_does_not_overwrite_the_local_import_report(real_places):
    report = ROOT / "data" / "import_report.json"
    before = report.read_bytes() if report.exists() else None
    load_demo()
    assert (report.read_bytes() if report.exists() else None) == before


@pytest.mark.django_db
def test_load_demo_data_refuses_to_replace_real_stations_without_force(real_places):
    from .helpers import station_object

    station_object(4242, 32.0, -97.0, "3.1").save()
    with pytest.raises(CommandError, match="real"):
        call_command("load_demo_data", stdout=io.StringIO())
    assert Station.objects.filter(opis_id=4242).exists()  # untouched
    call_command("load_demo_data", force=True, stdout=io.StringIO())
    assert not Station.objects.filter(opis_id=4242).exists()


@pytest.mark.django_db
def test_load_demo_data_rejects_a_file_that_is_not_clearly_synthetic(monkeypatch, tmp_path):
    bogus = tmp_path / "x.csv"
    bogus.write_text(
        "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
        "4242,PILOT TRAVEL CENTER,I-1,Dallas,TX,1,3.10\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("fuelplanner.management.commands.load_demo_data.DEMO_CSV", bogus)
    with pytest.raises(CommandError, match="not clearly synthetic"):
        call_command("load_demo_data", stdout=io.StringIO())


# --- the demo supports useful trips ----------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(("route", "start", "finish"), JOURNEYS)
def test_demo_data_supports_cross_country_trips(client, http, real_places, route, start, finish):
    load_demo()
    http.get(OSRM_URL, json=json.loads((OSRM_FIXTURES / f"{route}.json").read_text()))
    response = post(client, start, finish)
    assert response.status_code == 200, response.content
    data = response.json()
    assert data["summary"]["fuel_stops"] >= 1 and data["summary"]["total_fuel_cost"] > 0
    assert all(s["station"]["name"].startswith("DEMO FUEL STOP") for s in data["fuel_stops"])
    assert all(leg["distance_miles"] <= 500.05 for leg in data["legs"])


# --- labelling synthetic data ----------------------------------------


@pytest.mark.django_db
def test_a_data_notice_labels_every_response_and_drops_the_opis_claim(client, http, settings):
    settings.FUEL_PLANNER = {**settings.FUEL_PLANNER, "DATA_NOTICE": NOTICE}
    from .helpers import DALLAS, HOUSTON, mock_route

    mock_route(http, DALLAS, HOUSTON, distance=240.0)
    data = post(client, "Dallas, TX", "Houston, TX").json()
    assert data["meta"]["data_notice"] == NOTICE
    assert NOTICE in data["meta"]["attribution"] and "OPIS" not in data["meta"]["attribution"]
    assert data["assumptions"][0] == f"DATA NOTICE: {NOTICE}"
    assert any("SYNTHETIC demo values" in a for a in data["assumptions"])
    assert not any("supplied dataset" in a for a in data["assumptions"])
    geojson = post(client, "Dallas, TX", "Houston, TX", query="?format=geojson").json()
    assert NOTICE in geojson["attribution"]


@pytest.mark.django_db
def test_without_a_notice_responses_are_unchanged(client, http):
    from .helpers import DALLAS, HOUSTON, mock_route

    mock_route(http, DALLAS, HOUSTON, distance=240.0)
    data = post(client, "Dallas, TX", "Houston, TX").json()
    assert "data_notice" not in data["meta"] and "OPIS" in data["meta"]["attribution"]
    assert not data["assumptions"][0].startswith("DATA NOTICE")
    assert "data_notice" not in APIClient().get("/api/v1/health/").json()


@pytest.mark.django_db
def test_health_shows_the_notice(settings):
    settings.FUEL_PLANNER = {**settings.FUEL_PLANNER, "DATA_NOTICE": NOTICE}
    assert APIClient().get("/api/v1/health/").json()["data_notice"] == NOTICE


# --- production settings on Render ----------------------------------------

PROD = {"DJANGO_SECRET_KEY": "render-secret-" + "k" * 40, "DJANGO_ALLOWED_HOSTS": ".onrender.com"}


def settings_value(expression, **env):
    result = run_settings(
        f"import json, config.settings as s; print(json.dumps({expression}))", **{**PROD, **env}
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_render_hostname_is_trusted_automatically():
    out = settings_value(
        "[s.ALLOWED_HOSTS, s.CSRF_TRUSTED_ORIGINS]",
        RENDER_EXTERNAL_HOSTNAME="demo-abc.onrender.com",
    )
    assert "demo-abc.onrender.com" in out[0] and ".onrender.com" in out[0]
    assert out[1] == ["https://demo-abc.onrender.com"]


def test_csrf_origins_can_be_configured_and_are_not_duplicated():
    out = settings_value(
        "s.CSRF_TRUSTED_ORIGINS",
        DJANGO_CSRF_TRUSTED_ORIGINS="https://a.example.com, https://demo.onrender.com",
        RENDER_EXTERNAL_HOSTNAME="demo.onrender.com",
    )
    assert out == ["https://a.example.com", "https://demo.onrender.com"]


def test_no_render_hostname_means_no_extra_trust():
    assert settings_value("[s.ALLOWED_HOSTS, s.CSRF_TRUSTED_ORIGINS]") == [[".onrender.com"], []]


def test_the_render_environment_passes_the_django_deploy_checklist():
    env = {
        **PROD,
        "DJANGO_BEHIND_HTTPS_PROXY": "true",
        "DJANGO_SECURE_SSL_REDIRECT": "true",
        "DJANGO_SECURE_HSTS_SECONDS": "31536000",
        "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS": "true",
        "DJANGO_SECURE_HSTS_PRELOAD": "true",
    }
    result = run_settings(
        "import django, os; os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings';"
        "django.setup(); from django.core.management import call_command;"
        "call_command('check', deploy=True, fail_level='WARNING')",
        **env,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.django_db
def test_https_redirect_does_not_break_platform_health_checks(settings):
    settings.SECURE_SSL_REDIRECT = True
    client = APIClient()
    assert client.get("/api/v1/health/").status_code in (200, 503)  # answered over plain HTTP
    redirected = client.post("/api/v1/fuel-route/", {}, format="json")
    assert redirected.status_code == 301 and redirected["Location"].startswith("https://")


def test_production_responses_never_include_a_traceback_or_browsable_html():
    out = settings_value("s.REST_FRAMEWORK['DEFAULT_RENDERER_CLASSES']")
    assert out == ["rest_framework.renderers.JSONRenderer"]
    assert settings_value("s.DEBUG") is False


# --- the deployment files ----------------------------------------


def render_yaml() -> str:
    return (ROOT / "render.yaml").read_text(encoding="utf-8")


def test_render_blueprint_runs_the_free_demo_on_synthetic_data_only():
    text = render_yaml()
    assert re.search(r"^\s+plan: free$", text, re.M)
    assert "load_demo_data" in text and "gunicorn config.wsgi:application" in text
    assert "migrate --noinput" in text and "--workers 1" in text
    assert "healthCheckPath: /api/v1/health/" in text
    for private in ("FUEL_CSV_PATH", "fuel-prices-for-be-assessment", "load_fuel_data", "OPIS"):
        assert private not in text, private


def test_render_blueprint_configures_a_secure_production_environment():
    text = render_yaml()
    assert re.search(r'key: DJANGO_DEBUG\s+value: "false"', text)
    assert re.search(r"key: DJANGO_SECRET_KEY\s+generateValue: true", text)  # never in the repo
    assert not re.search(r"DJANGO_SECRET_KEY\s+value:", text)
    for key in ("DJANGO_ALLOWED_HOSTS", "DJANGO_BEHIND_HTTPS_PROXY", "DJANGO_SECURE_SSL_REDIRECT",
                "DJANGO_SECURE_HSTS_SECONDS", "DATA_NOTICE"):  # fmt: skip
        assert f"key: {key}" in text, key
    assert re.search(r'key: DATA_NOTICE\s+value: ".*SYNTHETIC', text)


def test_python_version_and_gunicorn_are_pinned_for_render():
    assert (ROOT / ".python-version").read_text().strip() == "3.13"
    assert re.search(r"^gunicorn==\d+\.\d+\.\d+$", (ROOT / "requirements.txt").read_text(), re.M)


def test_the_wsgi_application_gunicorn_will_serve_imports():
    import importlib

    module = importlib.import_module("config.wsgi")
    assert callable(module.application)


def test_error_format_still_holds_for_unknown_routes_in_production(client):
    response = client.get("/api/v1/nope")
    assert response.status_code == 404 and error(response)["code"] == "not_found"
