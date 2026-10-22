"""Messy-input behaviour of the CSV importer, the import command and the shipped place table."""

import codecs
import io
import json
from decimal import Decimal
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.core.management import call_command
from django.core.management.base import CommandError

from fuelplanner.importer import DatasetError, clean_and_geocode, read_rows
from fuelplanner.models import Station
from fuelplanner.services.gazetteer import (
    Gazetteer,
    GazetteerNotFoundError,
    build_place_records,
    iter_gazetteer,
    write_gazetteer,
)
from fuelplanner.services.geometry import haversine_miles

FIXTURES = Path(__file__).parent / "fixtures"
HEADER = "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price"
SHIPPED_GAZETTEER = Path(django_settings.BASE_DIR) / "data" / "us_places.csv.gz"


@pytest.fixture
def gazetteer():
    return Gazetteer.from_file(FIXTURES / "sample_places.csv")


def write_csv(tmp_path, body, *, encoding="utf-8", newline="\n", header=HEADER):
    path = tmp_path / "fuel.csv"
    text = (header + "\n" + body).replace("\n", newline)
    path.write_bytes(text.encode(encoding))
    return path


def load(tmp_path, body, gazetteer, **kwargs):
    return clean_and_geocode(read_rows(write_csv(tmp_path, body, **kwargs)), gazetteer)


# --- file formats ------------------------------------------------------------------------------


def test_excel_style_file_with_bom_crlf_quotes_and_padding(tmp_path, gazetteer):
    crlf = chr(13) + chr(10)
    body = '1,"PILOT, INC ""#1""","I-80, EXIT 5",Chicago   ,il,100," 3.250 "' + crlf
    path = tmp_path / "excel.csv"
    path.write_bytes(codecs.BOM_UTF8 + (HEADER + crlf + body).encode("utf-8"))  # real BOM
    rows = read_rows(path)
    assert list(rows[0])[0] == "OPIS Truckstop ID"  # the BOM did not leak into the first header
    assert rows[0]["Truckstop Name"] == 'PILOT, INC "#1"'
    assert rows[0]["Retail Price"] == "3.250" and rows[0]["City"] == "Chicago"
    stations, report = clean_and_geocode(rows, gazetteer)
    assert (stations[0].state, stations[0].price) == ("IL", Decimal("3.250"))
    assert report.matched_stations == 1


def test_rows_with_the_wrong_number_of_fields_are_rejected_not_guessed(tmp_path, gazetteer):
    lines = [
        "1,GOOD,I-35,Dallas,TX,300,3.00",
        "2,EXTRA FIELD,I-35,Dallas,TX,300,3,50",  # an unquoted decimal comma: 8 fields
        "3,TOO SHORT,I-35,Dallas,TX",
        "4,ALSO GOOD,I-35,Houston,TX,310,3.10",
    ]
    body = "".join(line + chr(10) for line in lines)
    stations, report = load(tmp_path, body, gazetteer)
    assert sorted(s.opis_id for s in stations) == [1, 4]
    assert dict(report.rejected_rows) == {"malformed_row": 2}
    assert report.us_stations == 2


def test_columns_may_be_reordered_and_extra_columns_are_ignored(tmp_path, gazetteer):
    header = "Retail Price,State,City,Notes,Address,Truckstop Name,OPIS Truckstop ID,Rack ID"
    path = write_csv(tmp_path, "3.10,TX,Dallas,ignore me,I-35,LOVES #2,2,300\n", header=header)
    stations, _ = clean_and_geocode(read_rows(path), gazetteer)
    assert [(s.opis_id, s.city, s.price) for s in stations] == [(2, "Dallas", Decimal("3.10"))]


def test_a_windows_code_page_file_is_read_leniently(tmp_path, gazetteer):
    path = write_csv(tmp_path, "1,CAFÉ TRUCK STOP,I-35,Dallas,TX,300,3.00\n", encoding="cp1252")
    rows = read_rows(path)
    assert rows[0]["Truckstop Name"] == "CAFÉ TRUCK STOP"
    assert clean_and_geocode(rows, gazetteer)[1].matched_stations == 1


def test_header_only_file_is_valid_but_empty(tmp_path, gazetteer):
    stations, report = load(tmp_path, "", gazetteer)
    assert stations == [] and report.csv_rows == 0
    assert report.as_dict()["matched_percent"] == 0.0


def test_empty_file_is_reported_as_missing_columns(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_bytes(b"")
    with pytest.raises(DatasetError, match="missing expected column"):
        read_rows(path)


# --- bad values ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "price",
    ["NaN", "Infinity", "-3.5", "0", "0.49", "20.01", "1e3", "", "abc", "$3.50", "--"],
)
def test_implausible_or_malformed_prices_are_rejected_not_repaired(tmp_path, gazetteer, price):
    stations, report = load(tmp_path, f"1,X,I-35,Dallas,TX,300,{price}\n", gazetteer)
    assert stations == []
    assert dict(report.rejected_rows) == {"bad_price": 1}


@pytest.mark.parametrize(("price", "expected"), [("0.5", "0.5"), ("20", "20"), ("3.5E0", "3.5")])
def test_price_bounds_are_inclusive_and_scientific_notation_is_understood(
    tmp_path, gazetteer, price, expected
):
    stations, _ = load(tmp_path, f"1,X,I-35,Dallas,TX,300,{price}\n", gazetteer)
    assert stations[0].price == Decimal(expected)


@pytest.mark.parametrize("station_id", ["abc", "", "12.5", "-", "1e3"])
def test_malformed_station_ids_are_rejected(tmp_path, gazetteer, station_id):
    _, report = load(tmp_path, f"{station_id},X,I-35,Dallas,TX,300,3.00\n", gazetteer)
    assert dict(report.rejected_rows) == {"bad_station_id": 1}


def test_station_ids_with_padding_or_leading_zeros_are_normalised(tmp_path, gazetteer):
    stations, _ = load(
        tmp_path, "007,X,I-35,Dallas,TX,300,3.00\n 8 ,Y,I-35,Houston,TX,300,3.00\n", gazetteer
    )
    assert sorted(s.opis_id for s in stations) == [7, 8]


def test_one_id_with_two_locations_is_flagged_and_uses_the_first(tmp_path, gazetteer):
    body = "5,A,I-35,Dallas,TX,300,3.00\n5,A,I-10,Houston,TX,300,2.90\n"
    stations, report = load(tmp_path, body, gazetteer)
    assert report.stations_with_conflicting_locations == 1
    assert (stations[0].city, stations[0].price) == ("Dallas", Decimal("2.90"))
    assert report.as_dict()["stations_with_conflicting_locations"] == 1


def test_lowest_price_wins_regardless_of_row_order(tmp_path, gazetteer):
    forward = (
        "5,A,I-35,Dallas,TX,300,3.00\n5,A,I-35,Dallas,TX,300,2.80\n5,A,I-35,Dallas,TX,300,3.40\n"
    )
    backward = "".join(reversed(forward.splitlines(keepends=True)))
    a, _ = load(tmp_path, forward, gazetteer)
    b, _ = load(tmp_path, backward, gazetteer)
    assert a[0].price == b[0].price == Decimal("2.80") and a[0].source_rows == 3


def test_importing_is_deterministic(tmp_path, gazetteer):
    path = FIXTURES / "sample_fuel.csv"
    first = clean_and_geocode(read_rows(path), gazetteer)
    second = clean_and_geocode(read_rows(path), gazetteer)
    assert first[0] == second[0] and first[1].as_dict() == second[1].as_dict()


# --- the import command --------------------------------------------------------------------------


def run_import(tmp_path, **kwargs):
    out = io.StringIO()
    options = {
        "csv": str(FIXTURES / "sample_fuel.csv"),
        "gazetteer": str(FIXTURES / "sample_places.csv"),
        "report": str(tmp_path / "report.json"),
        "stdout": out,
    }
    options.update(kwargs)
    call_command("load_fuel_data", **options)
    return out.getvalue()


@pytest.mark.django_db
def test_import_fails_clearly_when_the_place_table_is_missing(tmp_path):
    with pytest.raises(CommandError, match="build_gazetteer"):
        run_import(tmp_path, gazetteer=str(tmp_path / "missing.csv.gz"))
    assert Station.objects.count() == 0


@pytest.mark.django_db
def test_import_with_no_locatable_stations_keeps_existing_data(tmp_path):
    run_import(tmp_path)
    before = Station.objects.count()
    unknown = write_csv(tmp_path, "1,X,I-1,Atlantis,TX,300,3.00\n2,Y,I-1,Lemuria,TX,300,3.00\n")
    with pytest.raises(CommandError, match="No stations could be located"):
        run_import(tmp_path, csv=str(unknown))
    assert Station.objects.count() == before  # a failed import never wipes the table
    assert json.loads((tmp_path / "report.json").read_text())["matched_stations"] == 0


@pytest.mark.django_db
def test_relative_report_paths_are_placed_under_the_project_root(tmp_path, settings):
    settings.BASE_DIR = tmp_path
    run_import(tmp_path, report="out/report.json")
    assert (tmp_path / "out" / "report.json").exists()


@pytest.mark.django_db
def test_import_summary_is_human_readable(tmp_path):
    output = run_import(tmp_path)
    for fragment in (
        "CSV rows read",
        "Canadian rows excluded",
        "Located via gazetteer",
        "Imported 7",
    ):
        assert fragment in output


# --- gazetteer files ---------------------------------------------------------------------------


def test_malformed_source_rows_are_skipped_not_fatal():
    places = [
        {"USPS": "TX", "NAME": "Okay city", "INTPTLAT": "30", "INTPTLONG": "-97", "ALAND": "1"}
    ]
    gnis = [
        {"state_numeric": "48", "feature_name": "NoCoords"},
        {
            "state_numeric": "48",
            "feature_name": "Garbage",
            "prim_lat_dec": "x",
            "prim_long_dec": "y",
        },
        {
            "state_numeric": "48",
            "feature_name": "Fine",
            "prim_lat_dec": "31",
            "prim_long_dec": "-98",
        },
    ]
    zctas = [{"GEOID": "1"}, {"GEOID": "75201", "INTPTLAT": "", "INTPTLONG": ""},
             {"GEOID": "2134", "INTPTLAT": "42.3", "INTPTLONG": "-71.1"}]  # fmt: skip
    gazetteer = Gazetteer(build_place_records(places, [], gnis, zctas, {"48": "TX"}))
    assert gazetteer.find_city("Okay", "TX") and gazetteer.find_city("Fine", "TX")
    assert (
        gazetteer.find_city("NoCoords", "TX") is None
        and gazetteer.find_city("Garbage", "TX") is None
    )
    assert gazetteer.find_zip("02134") is not None and gazetteer.find_zip("75201") is None


def test_a_file_with_the_wrong_columns_is_rejected_with_a_rebuild_hint(tmp_path):
    bad = tmp_path / "wrong.csv"
    bad.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    with pytest.raises(GazetteerNotFoundError, match="not a valid gazetteer file"):
        Gazetteer.from_file(bad)


def test_gazetteer_roundtrips_as_plain_csv_and_iterates_places(tmp_path):
    dallas = {"USPS": "TX", "NAME": "Dallas city", "INTPTLAT": "32.79", "INTPTLONG": "-96.77"}
    zcta = {"GEOID": "75201", "INTPTLAT": "32.78", "INTPTLONG": "-96.8"}
    records = build_place_records([{**dallas, "ALAND": "5"}], [], [], [zcta], {})
    path = tmp_path / "places.csv"
    assert write_gazetteer(records, path) == len(records) == 3  # two name variants + a ZIP
    assert {p.kind for p in iter_gazetteer(path)} == {"place", "zip"}
    assert len(Gazetteer.from_file(path)) == 3
    assert path.read_bytes()[:2] != bytes([0x1F, 0x8B])  # plain .csv is not gzip-compressed


def test_an_empty_gazetteer_has_no_nearest_place():
    assert Gazetteer().nearest_place_miles(40.0, -100.0) is None


def test_nearest_place_distance_is_in_miles():
    gazetteer = Gazetteer.from_file(FIXTURES / "sample_places.csv")
    assert gazetteer.nearest_place_miles(41.8369, -87.6847) == pytest.approx(0, abs=0.01)
    one_degree_north = gazetteer.nearest_place_miles(42.8369, -87.6847)
    assert one_degree_north == pytest.approx(69.1, abs=1.0)


# --- the place table shipped in the repository ----------------------------------------------------

LANDMARKS = {  # city -> (lat, lon) of the real city centre; the table stores Census internal points
    ("Chicago", "IL"): (41.8781, -87.6298),
    ("New York", "NY"): (40.7128, -74.0060),
    ("Los Angeles", "CA"): (34.0522, -118.2437),
    ("Dallas", "TX"): (32.7767, -96.7970),
    ("Houston", "TX"): (29.7604, -95.3698),
    ("Seattle", "WA"): (47.6062, -122.3321),
    ("Miami", "FL"): (25.7617, -80.1918),
    ("Denver", "CO"): (39.7392, -104.9903),
    ("Atlanta", "GA"): (33.7490, -84.3880),
    ("Phoenix", "AZ"): (33.4484, -112.0740),
    ("Boston", "MA"): (42.3601, -71.0589),
    ("Salt Lake City", "UT"): (40.7608, -111.8910),
    ("Omaha", "NE"): (41.2565, -95.9345),
    ("Nashville", "TN"): (36.1627, -86.7816),
    ("Portland", "OR"): (45.5152, -122.6784),
}
ZIP_LANDMARKS = {
    "90210": (34.0901, -118.4065),
    "10001": (40.7506, -73.9970),
    "60601": (41.8858, -87.6181),
    "98101": (47.6105, -122.3350),
    "33101": (25.7700, -80.1900),
}


@pytest.fixture(scope="module")
def shipped():
    if not SHIPPED_GAZETTEER.exists():
        pytest.skip("shipped gazetteer not present")
    return Gazetteer.from_file(SHIPPED_GAZETTEER)


def test_shipped_gazetteer_has_the_expected_scale_and_columns(shipped):
    assert len(shipped) > 250_000
    rows = list(iter_gazetteer(SHIPPED_GAZETTEER))
    assert sum(r.kind == "zip" for r in rows) > 30_000
    assert {r.source for r in rows} == {"place", "cousub", "gnis", "zcta"}
    assert len({(r.kind, r.key) for r in rows}) == len(rows)  # keys are unique
    places = [r for r in rows if r.kind == "place"]
    assert all(17 <= r.lat <= 72 for r in places)  # 50 states, DC and Puerto Rico
    assert all(-180 <= r.lon <= -64 or 170 <= r.lon <= 180 for r in places)  # Aleutians cross 180
    zips = [r for r in rows if r.kind == "zip"]
    # ZIP codes also cover Guam, American Samoa and the Northern Marianas; the resolver rejects
    # those as outside the supported area.
    assert sum(not (17 <= r.lat <= 72) for r in zips) < 20


@pytest.mark.parametrize(("city", "state"), list(LANDMARKS))
def test_major_cities_resolve_to_within_fifteen_miles_of_their_centre(shipped, city, state):
    place = shipped.find_city(city, state)
    assert place is not None
    lat, lon = LANDMARKS[(city, state)]
    assert haversine_miles(place.lat, place.lon, lat, lon) < 15


@pytest.mark.parametrize("zip_code", list(ZIP_LANDMARKS))
def test_zip_centroids_are_close_to_the_real_location(shipped, zip_code):
    place = shipped.find_zip(zip_code)
    lat, lon = ZIP_LANDMARKS[zip_code]
    assert haversine_miles(place.lat, place.lon, lat, lon) < 6


def test_shipped_gazetteer_covers_the_known_spelling_variants(shipped):
    assert shipped.find_city("Saint Louis", "MO") is shipped.find_city("St. Louis", "MO")
    assert shipped.find_city("Fort Worth", "TX") is shipped.find_city("Ft Worth", "TX")
    assert shipped.find_city("Mount Pleasant", "SC") is shipped.find_city("Mt. Pleasant", "SC")
    assert shipped.find_city("Hot Springs National Park", "AR") is not None
    assert shipped.find_city("Carson City", "NV") is not None
    assert shipped.find_city("Washington", "DC") is not None
    assert shipped.nearest_place_miles(30.0, -70.0) > 200  # open Atlantic is far from any place
