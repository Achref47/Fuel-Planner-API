"""Exhaustive reference solver used to validate the optimiser (tests only).

It deliberately shares no logic with ``fuelplanner.services.optimizer``:

* it tries *every* subset of stations as the ordered list of stops (so it does not rely on
  "skip zero-purchase stops" or "fill or buy-just-enough" arguments), and
* for each fixed list of stops it finds the cheapest fuel purchases by a plain DP over
  integer fuel levels (valid because all test data are integers; ``lp_sequence_cost``
  below checks that claim against a continuous linear programme).
"""

from __future__ import annotations

import itertools
import math

import numpy as np
from scipy.optimize import linprog


def leg_distances(sequence, route_length):
    """Distances origin->s1, s1->s2, ..., sm->destination for stations in ``sequence``."""
    legs = []
    previous = None
    for st in sequence:
        if previous is None:
            legs.append(st["mile"] + st["off"])
        else:
            legs.append(previous["off"] + (st["mile"] - previous["mile"]) + st["off"])
        previous = st
    if previous is None:
        legs.append(route_length)
    else:
        legs.append(previous["off"] + route_length - previous["mile"])
    return legs


def integer_sequence_cost(sequence, route_length, cap, mpg, f0):
    """Cheapest cost of driving a fixed stop sequence, fuel levels in whole miles."""
    legs = leg_distances(sequence, route_length)
    inf = math.inf
    cost = [inf] * (cap + 1)
    cost[f0] = 0.0
    for k, d in enumerate(legs):
        # drive the leg
        moved = [inf] * (cap + 1)
        for level in range(d, cap + 1):
            moved[level - d] = cost[level]
        if k == len(legs) - 1:
            return min(moved)
        # buy fuel at stop k: any non-negative amount up to the capacity
        per_mile = sequence[k]["price"] / mpg
        for level in range(1, cap + 1):
            moved[level] = min(moved[level], moved[level - 1] + per_mile)
        cost = moved
    return inf  # unreachable


def brute_force_cost(stations, route_length, cap, mpg, f0):
    """Minimum cost over all ordered subsets of stations (inf if infeasible)."""
    ordered = sorted(stations, key=lambda s: s["mile"])
    best = math.inf
    for size in range(len(ordered) + 1):
        for subset in itertools.combinations(ordered, size):
            best = min(best, integer_sequence_cost(list(subset), route_length, cap, mpg, f0))
    return best


def lp_sequence_cost(sequence, route_length, cap, mpg, f0):
    """Cost of a fixed stop sequence from a continuous LP (fuel amounts need not be whole)."""
    legs = leg_distances(sequence, route_length)
    m = len(sequence)
    if m == 0:
        return 0.0 if legs[0] <= f0 else math.inf
    prices = np.array([s["price"] / mpg for s in sequence])
    a_ub, b_ub = [], []
    for k in range(m + 1):  # arrive at stop k+1 (or the destination) with fuel >= 0
        row = np.zeros(m)
        row[:k] = -1.0  # purchases at stops 1..k
        a_ub.append(row)
        b_ub.append(f0 - sum(legs[: k + 1]))
    for k in range(1, m + 1):  # leave stop k with fuel <= cap
        row = np.zeros(m)
        row[:k] = 1.0
        a_ub.append(row)
        b_ub.append(cap - f0 + sum(legs[:k]) - 0.0)
    result = linprog(
        prices, A_ub=np.array(a_ub), b_ub=np.array(b_ub), bounds=(0, None), method="highs"
    )
    return float(result.fun) if result.status == 0 else math.inf
