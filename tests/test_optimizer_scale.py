"""The optimiser against an independent exact solver on realistic-size instances."""

import math
import random
import time

import pytest

from fuelplanner.errors import NoFeasiblePlanError
from fuelplanner.services.optimizer import FuelOption, plan_fuel_stops

from .instances import CAP_MILES, MPG, chain_instance, to_options
from .reference_dp import reference_cost


def grid(stations, length, initial, scale):
    """Convert miles to whole units (1 unit = 1/scale mile) for the reference solver."""
    units = [
        {"mile": round(s["mile"] * scale), "off": round(s["off"] * scale), "price": s["price"]}
        for s in stations
    ]
    return units, round(length * scale), round(CAP_MILES * scale), round(initial * scale)


def check(stations, length, initial, scale, label):
    units, length_u, cap_u, initial_u = grid(stations, length, initial, scale)
    expected = reference_cost(units, length_u, cap_u, MPG, initial_u, mile_per_unit=1 / scale)
    try:
        result = plan_fuel_stops(
            to_options(stations), length, max_range=CAP_MILES, mpg=MPG, initial_fuel_miles=initial
        )
    except NoFeasiblePlanError:
        assert math.isinf(expected), (
            f"optimiser says infeasible, reference found {expected}: {label}"
        )
        return "infeasible"
    assert not math.isinf(expected), f"optimiser found a plan the reference cannot: {label}"
    assert result.cost == pytest.approx(expected, rel=1e-9, abs=1e-7), label
    return "feasible"


@pytest.mark.parametrize("seed", range(6))
def test_matches_reference_on_realistic_instances_whole_miles(seed):
    rng = random.Random(1000 + seed)
    outcomes = [
        check(*chain_instance(rng, tenth_mile_grid=False), 1, f"seed={seed} case={i}")
        for i in range(25)
    ]
    assert outcomes.count("feasible") > 10 and outcomes.count("infeasible") >= 1


@pytest.mark.parametrize("seed", range(4))
def test_matches_reference_with_fractional_miles_and_detours(seed):
    """Distances on a 0.1-mile grid: every leg, detour and purchase is non-integral."""
    rng = random.Random(2000 + seed)
    outcomes = [check(*chain_instance(rng), 10, f"seed={seed} case={i}") for i in range(10)]
    assert outcomes.count("feasible") > 4


def test_exact_range_boundaries_are_inclusive():
    """A leg of exactly the tank range is allowed; a hair more is not."""
    for gap, feasible in ((500.0, True), (499.9, True), (500.1, False)):
        stations = [
            {"mile": 0.0, "off": 0.0, "price": 3.0},
            {"mile": gap, "off": 0.0, "price": 3.0},
        ]
        # Empty tank at the start forces the first fill at mile 0; then the 2nd leg is `gap`.
        length = gap + 500.0
        outcome = check(stations, length, 0.0, 10, f"gap={gap}")
        assert (outcome == "feasible") is feasible


def test_exact_range_boundary_with_detours_counted_on_both_sides():
    # 495 miles of road plus 2.5 + 2.5 miles of detour = exactly 500.
    stations = [
        {"mile": 0.0, "off": 2.5, "price": 3.0},
        {"mile": 495.0, "off": 2.5, "price": 3.0},
    ]
    check(stations, 995.0, 0.0, 10, "boundary with detours")
    # (start is 2.5 mi from the first station, so start with at least that much fuel)
    check(stations, 995.0, 2.5, 10, "boundary with detours and a tiny start tank")


def test_reference_solver_agrees_with_the_tiny_exhaustive_search():
    """Sanity check of the checkers themselves: both reference methods agree on small cases."""
    from .brute_force import brute_force_cost

    rng = random.Random(77)
    for _ in range(120):
        cap = rng.randint(10, 30)
        length = rng.randint(cap // 2, 3 * cap)
        stations = [
            {
                "mile": rng.randint(0, length),
                "off": rng.choice([0, 0, 1, 2, 3]),
                "price": rng.choice([2.0, 2.5, 3.0, 3.5, 4.0]),
            }
            for _ in range(rng.randint(0, 6))
        ]
        f0 = rng.randint(0, cap)
        exhaustive = brute_force_cost(stations, length, cap, MPG, f0)
        layered = reference_cost(stations, length, cap, MPG, f0)
        assert (math.isinf(exhaustive) and math.isinf(layered)) or layered == pytest.approx(
            exhaustive, abs=1e-9
        )


def test_the_instance_generator_produces_a_healthy_mix():
    rng = random.Random(5)
    feasible = stops = 0
    for _ in range(80):
        stations, length, initial = chain_instance(rng)
        try:
            result = plan_fuel_stops(
                to_options(stations),
                length,
                max_range=CAP_MILES,
                mpg=MPG,
                initial_fuel_miles=initial,
            )
        except NoFeasiblePlanError:
            continue
        feasible += 1
        stops += len(result.stops)
    assert feasible > 40 and stops / feasible > 1.5  # genuinely multi-stop plans


def test_optimiser_scales_to_a_dense_national_corridor():
    rng = random.Random(11)
    options = [
        FuelOption(m, rng.choice([0.0, 1.0, 3.0, 5.0]), round(rng.uniform(2.7, 4.6), 3))
        for m in sorted(rng.uniform(0, 6000) for _ in range(600))
    ]
    started = time.perf_counter()
    result = plan_fuel_stops(options, 6000.0, max_range=CAP_MILES, mpg=MPG)
    elapsed = time.perf_counter() - started
    assert len(result.stops) >= 9 and elapsed < 1.0
