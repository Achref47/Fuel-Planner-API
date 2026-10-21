import os

os.environ.setdefault("DJANGO_DEBUG", "true")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_process_state(settings):
    """Every test starts with empty caches and the small synthetic gazetteer.

    The in-process caches (Django cache, gazetteer, station index, routing provider) would
    otherwise leak state between tests; the real files are only used by tests that opt in.
    """
    from django.core.cache import cache

    from fuelplanner.services.gazetteer import reset_gazetteer_cache
    from fuelplanner.services.routing import reset_routing_provider
    from fuelplanner.services.stations import reset_station_index

    real_gazetteer = settings.FUEL_PLANNER["GAZETTEER_PATH"]
    settings.FUEL_PLANNER = {
        **settings.FUEL_PLANNER,
        "GAZETTEER_PATH": FIXTURES / "sample_places.csv",
        "OSRM_MIN_INTERVAL_SECONDS": 0.0,
    }
    settings.REAL_GAZETTEER_PATH = real_gazetteer
    for reset in (cache.clear, reset_gazetteer_cache, reset_routing_provider, reset_station_index):
        reset()
    yield
    for reset in (cache.clear, reset_gazetteer_cache, reset_routing_provider, reset_station_index):
        reset()
