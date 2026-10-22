"""Random but realistic planning instances shared by the optimiser tests."""

from __future__ import annotations

import random

from fuelplanner.services.optimizer import FuelOption

CAP_MILES = 500.0
MPG = 10.0


def chain_instance(rng: random.Random, *, tenth_mile_grid: bool = True, infeasible_bias=0.2):
    """A route of 250-3,000 miles with stations at irregular spacing along it.

    Returns ``(stations, length, initial_fuel)`` in *miles*; stations are dicts with
    ``mile``, ``off`` and ``price``. Positions sit on a 0.1-mile grid (or whole miles) so the
    exact reference solver can work in integer units. With probability ``infeasible_bias``
    a 500+ mile window is emptied, making the trip impossible.
    """
    step = 0.1 if tenth_mile_grid else 1.0
    # Detours must lie on the same grid as everything else (whole miles in whole-mile mode).
    offsets = [0.0, 0.0, 1.0, 2.0, 5.0] if step == 1.0 else [0.0, 0.0, 0.3, 1.0, 2.5, 5.0]

    def random_offset():
        return round(rng.uniform(0, 8) / step) * step

    length = round(rng.uniform(250, 3000) / step) * step
    stations = []
    position = rng.uniform(0, 200)
    mean_gap = rng.choice([30, 60, 120, 250])
    while position < length:
        stations.append(
            {
                "mile": round(position / step) * step,
                "off": rng.choice(offsets)
                if step == 1.0
                else rng.choice(offsets + [random_offset()]),
                "price": round(rng.uniform(2.7, 4.6), 3),
            }
        )
        position += rng.uniform(1, 2 * mean_gap)
    if rng.random() < infeasible_bias and length > 600:
        start = rng.uniform(0, length - 520)
        stations = [s for s in stations if not start <= s["mile"] <= start + 520]
    initial = rng.choice([CAP_MILES, CAP_MILES, round(rng.uniform(0, CAP_MILES) / step) * step])
    for s in stations:  # tidy float noise from the rounding above
        s["mile"], s["off"] = round(min(s["mile"], length), 1), round(s["off"], 1)
    return stations, round(length, 1), round(initial, 1)


def to_options(stations):
    return [FuelOption(s["mile"], s["off"], s["price"]) for s in stations]
