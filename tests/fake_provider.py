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
