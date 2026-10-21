import pytest

from fuelplanner.services.text import normalize_name, place_key, strip_place_suffix


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Chicago", "chicago"),
        ("  New Castle          ", "newcastle"),
        ("St. Louis", "stlouis"),
        ("Saint Louis", "stlouis"),
        ("Fort Wayne", "ftwayne"),
        ("Mount Jackson", "mtjackson"),
        ("Mc Graw", "mcgraw"),
        ("De Witt", "dewitt"),
        ("Cañon City", "canoncity"),
        ("Foo (balance)", "foo"),
        ("O'Fallon", "ofallon"),
    ],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


def test_saint_not_replaced_inside_words():
    assert normalize_name("Stuart") == "stuart"
    assert normalize_name("Mountain View") == "mountainview"
    assert normalize_name("Fortuna") == "fortuna"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Abbeville city", "Abbeville"),
        ("Abanda CDP", "Abanda"),
        ("Foo town", "Foo"),
        ("Bar (balance)", "Bar"),
        ("Augusta-Richmond County consolidated government (balance)", "Augusta-Richmond County"),
        ("Carson City", "Carson"),  # the exact-name variant is indexed separately
    ],
)
def test_strip_place_suffix(raw, expected):
    assert strip_place_suffix(raw) == expected


def test_place_key_combines_state_and_normalised_city():
    assert place_key(" tx ", "St. Louis") == "TX|stlouis"
