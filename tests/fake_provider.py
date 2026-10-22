"""A stand-in routing provider used to prove the provider is swappable via configuration."""

import threading
import time

import numpy as np

from fuelplanner.services.routing import RouteResult, RoutingProvider


class FakeProvider(RoutingProvider):
    name = "fake"
    attribution = "Fake routing for tests."
    calls: list = []
    delay = 0.0
    _lock = threading.Lock()

    def __init__(self, config):
        self.config = config

    def route(self, start, end):
        with self._lock:
            type(self).calls.append((start, end))
        time.sleep(type(self).delay)
        coords = np.array([[start[1], start[0]], [end[1], end[0]]])
        return RouteResult(123.4, 2.5, coords, self.name, self.attribution)


class LineProvider(RoutingProvider):
    """A straight-line 'road' between the points, 18 % longer than the crow flies."""

    name = "line"
    attribution = "Straight-line routing for tests."
    detour_factor = 1.18
    points = 600
    delay = 0.0  # seconds each route() call takes (to provoke concurrent identical requests)
    calls: list = []
    _lock = threading.Lock()

    def __init__(self, config):
        self.config = config

    def route(self, start, end):
        from fuelplanner.services.geometry import haversine_miles

        with self._lock:
            type(self).calls.append((start, end))
        time.sleep(type(self).delay)
        lat = np.linspace(start[0], end[0], self.points)
        lon = np.linspace(start[1], end[1], self.points)
        distance = float(haversine_miles(start[0], start[1], end[0], end[1])) * self.detour_factor
        return RouteResult(
            distance, distance / 55.0, np.column_stack((lon, lat)), self.name, self.attribution
        )
