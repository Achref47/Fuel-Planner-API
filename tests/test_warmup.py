"""Start-up behaviour: warming the caches and coping with a database that is not ready."""

import io
from decimal import Decimal
from unittest import mock

import pytest
from django.core.management.commands.runserver import Command as DjangoRunserver
from django.db import OperationalError

from fuelplanner.errors import FuelDataUnavailableError
from fuelplanner.management.commands.runserver import Command as WarmRunserver
from fuelplanner.models import Station
from fuelplanner.services.stations import load_station_index, reset_station_index
from fuelplanner.services.warmup import warm_up

from .helpers import DALLAS, HOUSTON, error, mock_route, post, station_object

pytestmark = pytest.mark.django_db


def test_warm_up_loads_places_and_stations_and_reports_timings():
    Station.objects.bulk_create([station_object(1, 32.0, -97.0, "3.1")])
    loaded = warm_up()
    assert loaded["stations_loaded"] == 1 and loaded["gazetteer_places"] > 5
    assert loaded["warmup_ms"] >= 0
    again = warm_up()  # idempotent and cheap the second time
    assert again["stations_loaded"] == 1


def test_warm_up_never_raises_even_when_nothing_is_available(settings, tmp_path, caplog):
    settings.FUEL_PLANNER = {**settings.FUEL_PLANNER, "GAZETTEER_PATH": tmp_path / "gone.csv.gz"}
    with mock.patch("fuelplanner.models.Station.objects") as manager:
        manager.all.side_effect = OperationalError("no such table: fuelplanner_station")
        with caplog.at_level("WARNING"):
            loaded = warm_up()
    assert loaded["gazetteer_places"] == 0 and loaded["stations_loaded"] == 0
    assert "place table unavailable" in caplog.text and "stations unavailable" in caplog.text


def test_runserver_warms_up_before_serving():
    out = io.StringIO()
    command = WarmRunserver(stdout=out)
    with mock.patch.object(DjangoRunserver, "inner_run") as serve:
        command.inner_run(None, addrport="8000")
    assert serve.call_count == 1
    assert "Warmed up:" in out.getvalue() and "No fuel stations loaded yet" in out.getvalue()


def test_runserver_does_not_nag_when_data_is_loaded():
    Station.objects.bulk_create([station_object(1, 32.0, -97.0, "3.1")])
    out = io.StringIO()
    with mock.patch.object(DjangoRunserver, "inner_run"):
        WarmRunserver(stdout=out).inner_run(None)
    assert "1 stations" in out.getvalue() and "No fuel stations" not in out.getvalue()


def test_an_empty_station_table_is_not_cached():
    """A server started before load_fuel_data must notice the data once it appears."""
    assert len(load_station_index()) == 0
    Station.objects.bulk_create([station_object(1, 32.0, -97.0, "3.1")])
    assert len(load_station_index()) == 1  # no restart or explicit refresh needed
    Station.objects.bulk_create([station_object(2, 33.0, -97.0, "3.2")])
    assert len(load_station_index()) == 1  # a populated index is cached (restart to refresh)
    reset_station_index()
    assert len(load_station_index()) == 2


def test_an_unmigrated_database_is_a_clear_503_and_wastes_no_routing_request(client, http):
    mock_route(http, DALLAS, HOUSTON, distance=900.0)
    with mock.patch("fuelplanner.models.Station.objects") as manager:
        manager.all.side_effect = OperationalError("no such table: fuelplanner_station")
        # Chicago -> Dallas is more than a tank away as the crow flies, so refuelling is certain.
        response = post(client, "Chicago, IL", "Dallas, TX")
    assert response.status_code == 503
    err = error(response)
    assert err["code"] == "fuel_data_unavailable"
    assert err["details"]["reason"] == "database_not_ready" and "migrate" in err["message"]
    assert len(http.calls) == 0  # failed fast, before calling the router


def test_missing_station_data_fails_fast_for_trips_that_certainly_need_fuel(client, http):
    mock_route(http)
    response = post(client)  # Chicago -> Dallas, ~800 miles in a straight line
    assert (response.status_code, error(response)["code"]) == (503, "fuel_data_unavailable")
    assert len(http.calls) == 0


def test_a_short_trip_still_works_without_station_data_and_does_call_the_router(client, http):
    mock_route(http, DALLAS, HOUSTON, distance=240.0)
    assert post(client, "Dallas, TX", "Houston, TX").status_code == 200
    assert len(http.calls) == 1


def test_the_loader_raises_a_planner_error_for_database_failures():
    with mock.patch("fuelplanner.models.Station.objects") as manager:
        manager.all.side_effect = OperationalError("disk I/O error")
        with pytest.raises(FuelDataUnavailableError):
            load_station_index()


def test_prices_are_loaded_as_exact_decimals():
    Station.objects.bulk_create([station_object(1, 32.0, -97.0, "3.00733333")])
    assert load_station_index().stations[0].price == Decimal("3.00733333")  # all 8 places kept
