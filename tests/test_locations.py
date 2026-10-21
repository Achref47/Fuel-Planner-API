import pytest
import requests
import responses

from fuelplanner.errors import (
    GeocodingProviderError,
    LocationDataUnavailableError,
    LocationNotFoundError,
    LocationOutsideUSAError,
)
from fuelplanner.services.locations import resolve_location
from fuelplanner.services.states import split_trailing_state, state_code

CENSUS = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"


def census_match(lat, lon, address="1600 PENNSYLVANIA AVE NW, WASHINGTON, DC, 20500"):
    return {
        "result": {
            "addressMatches": [{"matchedAddress": address, "coordinates": {"x": lon, "y": lat}}]
        }
    }


# --- coordinates ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        {"lat": 41.88, "lon": -87.63},
        {"latitude": "41.88", "longitude": "-87.63"},
        {"lat": 41.88, "lng": -87.63},
        "41.88,-87.63",
        "41.88, -87.63",
        " 41.88 -87.63 ",
    ],
)
def test_coordinates_in_many_notations(value):
    loc = resolve_location(value)
    assert (loc.lat, loc.lon) == pytest.approx((41.88, -87.63))
    assert loc.resolved_by == "coordinates" and loc.precision == "exact"
    assert loc.label == "41.88000, -87.63000"


@pytest.mark.parametrize(
    "value",
    [
        {"lat": 95, "lon": 10},
        {"lat": 10, "lon": 200},
        "95,10",
    ],
)
def test_impossible_coordinates_are_rejected(value):
    with pytest.raises(LocationNotFoundError, match="not a valid coordinate"):
        resolve_location(value)


@pytest.mark.parametrize(
    "value", [{"lat": 41.0}, {"lat": "x", "lon": 1}, {"lat": True, "lon": 1}, {}]
)
def test_incomplete_coordinate_objects_are_rejected(value):
    with pytest.raises(LocationNotFoundError, match="numeric"):
        resolve_location(value)


@pytest.mark.parametrize(
    ("lat", "lon"),
    [
        (48.8566, 2.3522),  # Paris
        (19.43, -99.13),  # Mexico City
        (60.0, -100.0),  # northern Canada
        (21.3, -157.8),  # Honolulu (not contiguous)
        (30.0, -70.0),  # Atlantic Ocean, inside the bounding box
        (26.0, -90.0),  # Gulf of Mexico
    ],
)
def test_coordinates_outside_the_contiguous_us_are_rejected(lat, lon):
    with pytest.raises(LocationOutsideUSAError) as excinfo:
        resolve_location({"lat": lat, "lon": lon})
    assert excinfo.value.code == "location_outside_usa" and excinfo.value.http_status == 422


def test_coordinates_near_a_known_place_are_accepted():
    # The fixture gazetteer only knows a handful of towns; this is ~10 miles from Dallas.
    assert resolve_location("32.9,-96.8").precision == "exact"


# --- ZIP codes and cities ----------------------------------------------------------------------


def test_zip_code_resolves_to_its_centroid():
    loc = resolve_location("60601")
    assert (loc.lat, loc.lon) == pytest.approx((41.885, -87.622))
    assert (loc.resolved_by, loc.precision, loc.label) == (
        "gazetteer_zip",
        "zip_centroid",
        "ZIP 60601",
    )
    assert resolve_location("60601-1234").lat == loc.lat


def test_unknown_zip_is_not_found():
    with pytest.raises(LocationNotFoundError, match="ZIP code 99999"):
        resolve_location("99999")


@pytest.mark.parametrize(
    "text",
    [
        "Chicago, IL",
        "chicago,il",
        "CHICAGO, IL",
        "Chicago, Illinois",
        "Chicago IL",
        "Chicago Illinois",
        "Chicago, IL 60601",
        "Chicago, IL, USA",
        "  Chicago ,  IL  ",
        "Chicago, il, United States",
    ],
)
def test_city_and_state_in_many_notations(text):
    loc = resolve_location(text)
    assert (loc.lat, loc.lon) == pytest.approx((41.8369, -87.6847))
    assert loc.resolved_by == "gazetteer_city" and loc.precision == "city"
    assert loc.label == "Chicago, IL"


def test_saint_and_alias_spellings_resolve():
    assert resolve_location("Saint Louis, MO").lat == resolve_location("St. Louis, MO").lat
    assert resolve_location("Hot Springs National Park, AR").lat == pytest.approx(34.48)
    assert resolve_location("Carson City, NV").label == "Carson City, NV"


@pytest.mark.parametrize(
    "text",
    [
        "Atlantis, TX",
        "Chicago, ZZ",
        "Paris, France",
        "Chicago",
        "???",
        "Springfield,",
        "Chicago, MO",
    ],
)
def test_unknown_or_ambiguous_text_is_not_found(text):
    with pytest.raises(LocationNotFoundError) as excinfo:
        resolve_location(text)
    assert "City, ST" in excinfo.value.message  # tells the user what to send


@pytest.mark.parametrize("value", ["", "   ", None, 42, ["Chicago", "IL"]])
def test_empty_or_wrong_types_are_rejected(value):
    with pytest.raises(LocationNotFoundError):
        resolve_location(value)


def test_non_contiguous_places_are_outside_coverage():
    with pytest.raises(LocationOutsideUSAError):
        resolve_location("Anchorage, AK")
    with pytest.raises(LocationOutsideUSAError):
        resolve_location("99501")


def test_city_lookup_needs_no_network():
    with responses.RequestsMock() as rsps:  # any HTTP call would raise
        resolve_location("Dallas, TX")
        resolve_location("60601")
        resolve_location("32.9,-96.8")
        assert len(rsps.calls) == 0


def test_missing_place_table_gives_actionable_error(settings, tmp_path):
    settings.FUEL_PLANNER = {**settings.FUEL_PLANNER, "GAZETTEER_PATH": tmp_path / "gone.csv.gz"}
    with pytest.raises(LocationDataUnavailableError, match="build_gazetteer") as excinfo:
        resolve_location("Dallas, TX")
    assert excinfo.value.http_status == 503
    # Coordinates still work without the place table (bounding-box check only).
    assert resolve_location("40.0,-100.0").resolved_by == "coordinates"


def test_state_helpers():
    assert state_code("tx") == "TX" and state_code("North Carolina") == "NC"
    assert state_code("D.C.") == "DC" and state_code("Texas ") == "TX"
    assert state_code("District of Columbia") == "DC"
    assert split_trailing_state("Fort Worth Texas") == ("Fort Worth", "TX")
    assert split_trailing_state("Raleigh North Carolina") == ("Raleigh", "NC")
    assert split_trailing_state("Texas") is None


# --- street addresses (Census Geocoder, mocked) --------------------------------------------------


@responses.activate
def test_street_address_uses_the_census_geocoder_once_and_caches():
    responses.get(CENSUS, json=census_match(38.8987, -77.0352))
    address = "1600 Pennsylvania Ave NW, Washington, DC 20500"
    first = resolve_location(address)
    second = resolve_location(address)
    assert (first.lat, first.lon) == pytest.approx((38.8987, -77.0352))
    assert first.resolved_by == "census_geocoder" and first.precision == "address"
    assert first.label.startswith("1600 PENNSYLVANIA")
    assert second == first
    assert len(responses.calls) == 1
    request = responses.calls[0].request
    assert "benchmark=Public_AR_Current" in request.url and "format=json" in request.url
    assert "spotter-fuel-route" in request.headers["User-Agent"]


@responses.activate
def test_cache_ignores_case_and_spacing_of_addresses():
    responses.get(CENSUS, json=census_match(38.9, -77.0))
    resolve_location("1600 Pennsylvania Ave NW, Washington, DC")
    resolve_location("1600  PENNSYLVANIA ave nw,  washington, dc")
    assert len(responses.calls) == 1


@responses.activate
def test_address_without_a_match_is_not_found_and_remembered():
    responses.get(CENSUS, json={"result": {"addressMatches": []}})
    for _ in range(2):
        with pytest.raises(LocationNotFoundError, match="not found by the geocoder"):
            resolve_location("99999 Nowhere Rd, Nowhere, TX")
    assert len(responses.calls) == 1


@responses.activate
def test_address_geocoded_outside_the_us_is_rejected():
    responses.get(CENSUS, json=census_match(48.85, 2.35))
    with pytest.raises(LocationOutsideUSAError):
        resolve_location("10 Rue de Rivoli, Paris, TX")


@responses.activate
@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"body": requests.Timeout("slow")}, "timeout"),
        ({"body": requests.ConnectionError("down")}, "connection_error"),
        ({"status": 500, "body": "oops"}, "http_error"),
        ({"status": 200, "body": "<html>not json</html>"}, "bad_response"),
        ({"json": {"unexpected": True}}, "bad_response"),
        ({"json": {"result": {"addressMatches": [{"coordinates": {}}]}}}, "bad_response"),
    ],
)
def test_geocoder_failures_become_clean_errors_and_are_not_cached(kwargs, reason):
    responses.get(CENSUS, **kwargs)
    for _ in range(2):
        with pytest.raises(GeocodingProviderError) as excinfo:
            resolve_location("1 Main St, Springfield, IL")
        assert excinfo.value.details["reason"] == reason
        assert excinfo.value.http_status == 502
    assert len(responses.calls) == 2  # failures are retried, never cached


@responses.activate
def test_address_is_not_written_to_the_logs(caplog):
    responses.get(CENSUS, json=census_match(38.9, -77.0))
    with caplog.at_level("DEBUG", logger="fuelplanner"):
        resolve_location("1600 Pennsylvania Ave NW, Washington, DC")
    assert "Pennsylvania" not in caplog.text
    assert "matched" in caplog.text
