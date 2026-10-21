from decimal import Decimal
from pathlib import Path

import pytest

from fuelplanner.importer import DatasetError, clean_and_geocode, read_rows
from fuelplanner.services.gazetteer import Gazetteer

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def result():
    rows = read_rows(FIXTURES / "sample_fuel.csv")
    gazetteer = Gazetteer.from_file(FIXTURES / "sample_places.csv")
    stations, report = clean_and_geocode(rows, gazetteer)
    return {s.opis_id: s for s in stations}, report


def test_report_counts(result):
    _, report = result
    assert report.csv_rows == 15
    assert report.canadian_rows == 1
    assert dict(report.rejected_rows) == {"bad_price": 2, "unknown_state": 1, "missing_city": 1}
    assert report.exact_duplicate_rows == 1
    assert report.us_stations == 8
    assert report.matched_stations == 7
    assert report.unmatched == [{"opis_id": 8, "city": "Atlantis", "state": "TX"}]
    assert report.as_dict()["matched_percent"] == 87.5


def test_duplicate_ids_merge_to_lowest_price_with_trimmed_city(result):
    stations, report = result
    chicago = stations[1]
    assert chicago.price == Decimal("3.25000000")
    assert chicago.city == "Chicago"
    assert chicago.source_rows == 2
    assert report.stations_with_price_changes == 1
    assert report.stations_with_name_variants == 1
    assert stations[3].source_rows == 2 and report.stations_with_price_changes == 1  # same price


def test_locations_come_from_the_gazetteer_never_invented(result):
    stations, _ = result
    assert (stations[1].latitude, stations[1].longitude) == pytest.approx((41.8369, -87.6847))
    assert stations[2].city == "Saint Louis"  # name variants still match
    assert stations[2].location_source == "place"
    assert stations[6].latitude > 34  # Hot Springs via alias
    assert 8 not in stations  # unmatched stations are excluded, not given fake coordinates


def test_invalid_and_foreign_rows_are_excluded(result):
    stations, _ = result
    assert not {9, 10, 11, 12, 13} & set(stations)


def test_missing_csv_explains_where_to_put_the_dataset(tmp_path):
    with pytest.raises(DatasetError) as excinfo:
        read_rows(tmp_path / "missing.csv")
    message = str(excinfo.value)
    assert "not found" in message
    assert "data/fuel-prices-for-be-assessment.csv" in message
    assert "FUEL_CSV_PATH" in message


def test_wrong_columns_are_reported(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    with pytest.raises(DatasetError, match="missing expected column"):
        read_rows(bad)
