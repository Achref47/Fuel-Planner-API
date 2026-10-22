import io
import json
import zipfile
from pathlib import Path

import pytest
import responses
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError

from fuelplanner.models import Station
from fuelplanner.services.gazetteer import Gazetteer

FIXTURES = Path(__file__).parent / "fixtures"


def run_import(tmp_path, **extra):
    out = io.StringIO()
    call_command(
        "load_fuel_data",
        csv=str(extra.pop("csv", FIXTURES / "sample_fuel.csv")),
        gazetteer=str(FIXTURES / "sample_places.csv"),
        report=str(tmp_path / "report.json"),
        stdout=out,
        **extra,
    )
    return out.getvalue()


@pytest.mark.django_db
def test_import_loads_stations_and_writes_report(tmp_path):
    output = run_import(tmp_path)
    assert "Imported 7 stations" in output
    assert Station.objects.count() == 7
    chicago = Station.objects.get(opis_id=1)
    assert chicago.price == pytest.approx(3.25)
    assert chicago.location_precision == "city"
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["matched_stations"] == 7
    assert report["unmatched_stations"][0]["city"] == "Atlantis"


@pytest.mark.django_db
def test_import_is_repeatable_and_replaces_rows(tmp_path):
    run_import(tmp_path)
    run_import(tmp_path)
    assert Station.objects.count() == 7


@pytest.mark.django_db
def test_dry_run_does_not_touch_the_database(tmp_path):
    output = run_import(tmp_path, dry_run=True)
    assert "Dry run" in output
    assert Station.objects.count() == 0


@pytest.mark.django_db
def test_missing_dataset_gives_clear_command_error(tmp_path):
    with pytest.raises(CommandError, match="not distributed with this repository"):
        run_import(tmp_path, csv=tmp_path / "absent.csv")


def census_zip(name, rows, header):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, header + "\n" + "\n".join("|".join(r) for r in rows) + "\n")
    return buffer.getvalue()


@responses.activate
def test_build_gazetteer_downloads_and_condenses(tmp_path):
    census_header = "USPS|GEOID|NAME|FUNCSTAT|ALAND|INTPTLAT|INTPTLONG"
    base = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2026_Gazetteer/"
    place = census_zip(
        "p.txt", [["TX", "4819000", "Dallas city", "A", "999", "32.79", "-96.77"]], census_header
    )
    cousub = census_zip(
        "c.txt", [["TX", "4800100", "Elm town", "A", "10", "31.0", "-97.0"]], census_header
    )
    zcta = census_zip(
        "z.txt",
        [["", "75201", "", "", "1", "32.78", "-96.80"]],
        "USPS|GEOID|NAME|FUNCSTAT|ALAND|INTPTLAT|INTPTLONG",
    )
    gnis_header = "feature_name|state_numeric|prim_lat_dec|prim_long_dec"
    gnis = census_zip(
        "Text/PopulatedPlaces_National.txt", [["Rural Spot", "48", "30.5", "-98.5"]], gnis_header
    )
    responses.get(base + "2026_Gaz_place_national.zip", body=place)
    responses.get(base + "2026_Gaz_cousubs_national.zip", body=cousub)
    responses.get(base + "2026_Gaz_zcta_national.zip", body=zcta)
    responses.get(
        "https://prd-tnm.s3.amazonaws.com/StagedProducts/GeographicNames/Topical/"
        "PopulatedPlaces_National_Text.zip",
        body=gnis,
    )
    output = tmp_path / "places.csv.gz"
    call_command(
        "build_gazetteer", output=str(output), raw_dir=str(tmp_path / "raw"), stdout=io.StringIO()
    )

    gazetteer = Gazetteer.from_file(output)
    assert gazetteer.find_city("Dallas", "TX").source == "place"
    assert gazetteer.find_city("Elm", "TX").source == "cousub"
    assert gazetteer.find_city("Rural Spot", "TX").source == "gnis"
    assert gazetteer.find_zip("75201") is not None
    assert len(responses.calls) == 4

    # A second run uses the download cache: no further HTTP requests.
    call_command(
        "build_gazetteer", output=str(output), raw_dir=str(tmp_path / "raw"), stdout=io.StringIO()
    )
    assert len(responses.calls) == 4


@responses.activate
def test_build_gazetteer_reports_download_failure(tmp_path):
    responses.get(
        "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2026_Gazetteer/2026_Gaz_place_national.zip",
        status=503,
    )
    with pytest.raises(CommandError, match="Could not download"):
        call_command(
            "build_gazetteer",
            output=str(tmp_path / "x.csv.gz"),
            raw_dir=str(tmp_path / "raw"),
            stdout=io.StringIO(),
        )


# --- Integration with the real, locally supplied dataset (skipped when absent) ---------

REAL_CSV = Path(settings.FUEL_PLANNER["FUEL_CSV_PATH"])
REAL_GAZETTEER = Path(settings.FUEL_PLANNER["GAZETTEER_PATH"])


@pytest.mark.skipif(
    not (REAL_CSV.exists() and REAL_GAZETTEER.exists()),
    reason="assessment CSV or shipped gazetteer not available",
)
def test_real_dataset_is_located_with_high_coverage():
    from fuelplanner.importer import clean_and_geocode, read_rows

    stations, report = clean_and_geocode(read_rows(REAL_CSV), Gazetteer.from_file(REAL_GAZETTEER))
    assert report.us_stations > 6000
    assert report.matched_stations / report.us_stations >= 0.99
    assert all(24 < s.latitude < 50 or s.state in {"AK", "HI"} for s in stations)
    assert all(s.price > 0 for s in stations)


@pytest.mark.django_db
def test_imported_prices_keep_all_their_decimals_down_to_the_billed_cent(tmp_path):
    """Regression: prices were stored to 5 decimals, which can move a bill by a cent.

    3.41 gal at $3.00733333 = $10.2550... -> $10.26, but at $3.00733 it is $10.2549... -> $10.25.
    """
    from decimal import Decimal

    from fuelplanner.services.accounting import build_ledger
    from fuelplanner.services.optimizer import OptimalPlan, PlannedStop
    from fuelplanner.services.stations import Candidate, load_station_index

    csv_path = tmp_path / "prices.csv"
    csv_path.write_text(
        "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
        "1,EIGHT DECIMALS,I-35,Dallas,TX,300,3.00733333\n",
        encoding="utf-8",
    )
    run_import(tmp_path, csv=csv_path)
    station = load_station_index(refresh=True).stations[0]
    assert station.price == Decimal("3.00733333")

    plan = OptimalPlan((PlannedStop(0, 100.0, 400.0, 34.1),), 434.1, 0.0, 500.0)
    ledger = build_ledger(plan, [Candidate(station, 100.0, 0.0)], route_length=534.1)
    assert ledger.stops[0].cost == Decimal("10.26")
