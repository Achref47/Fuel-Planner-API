from pathlib import Path

import pytest

from fuelplanner.services.gazetteer import (
    Gazetteer,
    GazetteerNotFoundError,
    build_place_records,
    read_pipe_table,
    write_gazetteer,
)

FIXTURES = Path(__file__).parent / "fixtures"


def census(state, name, lat, lon, land="1000", funcstat="A"):
    return {
        "USPS": state,
        "NAME": name,
        "INTPTLAT": str(lat),
        "INTPTLONG": str(lon),
        "ALAND": land,
        "FUNCSTAT": funcstat,
    }


def gnis(state_numeric, name, lat, lon):
    return {
        "state_numeric": state_numeric,
        "feature_name": name,
        "prim_lat_dec": str(lat),
        "prim_long_dec": str(lon),
    }


def build(places=(), cousubs=(), gnis_rows=(), zctas=(), fips=None):
    records = build_place_records(places, cousubs, gnis_rows, zctas, fips or {"48": "TX"})
    return Gazetteer(records)


def test_census_suffix_is_stripped_and_exact_name_kept():
    g = build(
        places=[census("TX", "Dallas city", 32.7, -96.8), census("NV", "Carson City", 39.1, -119.7)]
    )
    assert g.find_city("Dallas", "TX").lat == 32.7
    # "Carson City" has no type suffix; stripping yields "Carson", so the full name is indexed too.
    assert g.find_city("Carson City", "NV") is not None


def test_incorporated_place_beats_cdp_and_larger_land_wins():
    g = build(
        places=[
            census("TX", "Springfield CDP", 1.0, 1.0, land="900000", funcstat="S"),
            census("TX", "Springfield city", 2.0, 2.0, land="10", funcstat="A"),
            census("TX", "Springfield town", 3.0, 3.0, land="500", funcstat="A"),
        ]
    )
    assert g.find_city("Springfield", "TX").lat == 3.0


def test_source_priority_place_over_cousub_over_gnis():
    g = build(
        places=[census("TX", "Alpha city", 1.0, 1.0)],
        cousubs=[census("TX", "Alpha town", 2.0, 2.0), census("TX", "Beta town", 5.0, 5.0)],
        gnis_rows=[
            gnis("48", "Alpha", 3.0, 3.0),
            gnis("48", "Beta", 6.0, 6.0),
            gnis("48", "Gamma", 7.0, 7.0),
        ],
    )
    assert (g.find_city("Alpha", "TX").lat, g.find_city("Alpha", "TX").source) == (1.0, "place")
    assert g.find_city("Beta", "TX").source == "cousub"
    assert g.find_city("Gamma", "TX").source == "gnis"


def test_hyphenated_census_names_match_first_part():
    g = build(
        places=[census("GA", "Athens-Clarke County unified government (balance)", 33.9, -83.4)]
    )
    assert g.find_city("Athens", "GA") is not None


def test_gnis_zero_coordinates_and_unknown_states_are_skipped():
    g = build(gnis_rows=[gnis("48", "Nowhere", 0, 0), gnis("99", "Elsewhere", 1.0, 1.0)])
    assert g.find_city("Nowhere", "TX") is None
    assert g.find_city("Elsewhere", "TX") is None


def test_bad_census_rows_are_ignored():
    g = build(places=[census("TX", "Broken city", "x", "y")])
    assert g.find_city("Broken", "TX") is None


def test_zip_lookup_pads_and_truncates():
    g = build(zctas=[{"GEOID": "02134", "INTPTLAT": "42.35", "INTPTLONG": "-71.13"}])
    assert g.find_zip("02134-1234").lat == 42.35
    assert g.find_zip("2134").lat == 42.35
    assert g.find_zip("99999") is None


def test_aliases_resolve_known_misspellings():
    g = Gazetteer.from_file(FIXTURES / "sample_places.csv")
    assert g.find_city("Hot Springs National Park", "AR").name == "Hot Springs"
    assert g.find_city("S Coffeyville", "OK") is not None  # via normalisation (S -> alias)


def test_roundtrip_through_gzip_file(tmp_path):
    original = build(
        places=[census("TX", "Dallas city", 32.79, -96.77)],
        zctas=[{"GEOID": "75201", "INTPTLAT": "32.78", "INTPTLONG": "-96.8"}],
    )
    path = tmp_path / "places.csv.gz"
    count = write_gazetteer(
        build_place_records(
            [census("TX", "Dallas city", 32.79, -96.77)],
            [],
            [],
            [{"GEOID": "75201", "INTPTLAT": "32.78", "INTPTLONG": "-96.8"}],
        ),
        path,
    )
    loaded = Gazetteer.from_file(path)
    assert count == len(loaded) == len(original)
    assert loaded.find_city("Dallas", "TX").lon == pytest.approx(-96.77)
    assert loaded.find_zip("75201") is not None


def test_missing_gazetteer_gives_helpful_error(tmp_path):
    with pytest.raises(GazetteerNotFoundError, match="build_gazetteer"):
        Gazetteer.from_file(tmp_path / "nope.csv.gz")


def test_read_pipe_table_strips_padded_headers_and_values():
    data = b"USPS|GEOID|INTPTLONG  \nAL|0100100| -85.5 \n"
    assert read_pipe_table(data) == [{"USPS": "AL", "GEOID": "0100100", "INTPTLONG": "-85.5"}]
