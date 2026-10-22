"""Load the data the first request would otherwise have to load.

Reading the 265k-row place table and the station table takes about a second. Doing it once at
start-up (``runserver`` calls this) or from the health endpoint keeps the first real request as
fast as every later one.
"""

from __future__ import annotations

import logging
import time

from django.db import DatabaseError

from ..errors import FuelDataUnavailableError
from .gazetteer import GazetteerNotFoundError, get_gazetteer
from .stations import load_station_index

logger = logging.getLogger(__name__)


def warm_up() -> dict:
    """Load the place table, its spatial index and the stations; never raises."""
    started = time.perf_counter()
    places = stations = 0
    try:
        gazetteer = get_gazetteer()
        places = len(gazetteer)
        gazetteer.nearest_place_miles(39.0, -98.0)  # builds the coordinate-check index
    except GazetteerNotFoundError as exc:
        logger.warning("warm-up: place table unavailable: %s", exc)
    try:
        stations = len(load_station_index())
    except (FuelDataUnavailableError, DatabaseError) as exc:
        logger.warning("warm-up: stations unavailable: %s", exc)
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    logger.info("warm-up: %d places, %d stations in %d ms", places, stations, elapsed_ms)
    return {"gazetteer_places": places, "stations_loaded": stations, "warmup_ms": elapsed_ms}
