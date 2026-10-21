from django.conf import settings


def test_vehicle_constants_match_assignment():
    cfg = settings.FUEL_PLANNER
    assert cfg["MAX_RANGE_MILES"] == 500.0
    assert cfg["MPG"] == 10.0


def test_no_api_keys_required():
    # The default routing provider is key-less (OSRM); nothing secret in settings.
    assert settings.FUEL_PLANNER["ROUTING_PROVIDER"] == "osrm"
    assert not any("KEY" in name and name != "SECRET_KEY" for name in settings.FUEL_PLANNER)
