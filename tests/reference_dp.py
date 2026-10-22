"""A second, independent exact solver for realistic-size instances (tests only).

``brute_force.py`` proves the optimiser on tiny instances by trying every subset of stops.
This module covers realistic sizes (dozens of stations, a 500-mile tank) with a different
exact method: a dynamic programme over (station, whole units of fuel), where one *unit* is
a fixed small distance (e.g. 0.1 mile). Fuel may be bought one unit at a time at any
station, any station may be skipped, and nothing about "fill up or buy just enough" is
assumed - that structure, which the optimiser relies on, is verified rather than baked in.

Everything is expressed in whole units, so the answer is exact for inputs on that grid.
"""

from __future__ import annotations

import numpy as np


def _shift_down(costs: np.ndarray, units: int) -> np.ndarray:
    """Drive ``units``: the fuel level drops by that much (levels below zero are impossible)."""
    shifted = np.full_like(costs, np.inf)
    if units < len(costs):
        shifted[: len(costs) - units] = costs[units:]
    return shifted


def _buy(costs: np.ndarray, unit_price: float) -> np.ndarray:
    """Allow buying any number of units here: cost[f] = min over g<=f of cost[g] + (f-g)*price."""
    levels = np.arange(len(costs)) * unit_price
    return levels + np.minimum.accumulate(costs - levels)


def reference_cost(
    stations: list[dict],
    route_length: int,
    cap: int,
    mpg: float,
    initial_fuel: int,
    mile_per_unit: float = 1.0,
) -> float:
    """Cheapest total cost (USD), or ``inf`` if the destination cannot be reached.

    ``stations`` hold integer ``mile`` / ``off`` (in units) and a float ``price`` (USD/gal);
    ``route_length``, ``cap`` and ``initial_fuel`` are in units too.
    """
    ordered = sorted(stations, key=lambda s: s["mile"])
    n = len(ordered)
    arrived = [np.full(cap + 1, np.inf) for _ in range(n)]

    start = np.full(cap + 1, np.inf)
    start[initial_fuel] = 0.0
    best = float(_shift_down(start, route_length).min())
    for j, s in enumerate(ordered):
        arrived[j] = np.minimum(arrived[j], _shift_down(start, s["mile"] + s["off"]))

    for i, s in enumerate(ordered):
        if not np.isfinite(arrived[i]).any():
            continue
        unit_price = s["price"] / mpg * mile_per_unit
        can_leave = _buy(arrived[i], unit_price)
        best = min(best, float(_shift_down(can_leave, s["off"] + route_length - s["mile"]).min()))
        for j in range(i + 1, n):
            t = ordered[j]
            distance = s["off"] + (t["mile"] - s["mile"]) + t["off"]
            if distance <= cap:
                arrived[j] = np.minimum(arrived[j], _shift_down(can_leave, distance))
    return best
