"""Properties any correct cost-minimising planner must satisfy (metamorphic tests).

Each test starts from a random *feasible* instance and changes it in a way whose effect on
the optimal cost is known in advance. These catch mistakes that example-based tests miss.
"""

import random
from dataclasses import replace

import pytest

from fuelplanner.errors import NoFeasiblePlanError
from fuelplanner.services.optimizer import FuelOption, plan_fuel_stops

from .instances import CAP_MILES, MPG, chain_instance, to_options

TOL = 1e-7


def solve(options, length, initial=CAP_MILES, cap=CAP_MILES):
    return plan_fuel_stops(options, length, max_range=cap, mpg=MPG, initial_fuel_miles=initial)


def feasible_instances(seed, count=40):
    """Yield ``(options, length, initial_fuel, plan)`` for random solvable trips."""
    rng = random.Random(seed)
    made = 0
    while made < count:
        stations, length, initial = chain_instance(rng, infeasible_bias=0.0)
        options = to_options(stations)
        try:
            plan = solve(options, length, initial)
        except NoFeasiblePlanError:
            continue
        if plan.stops:  # interesting cases only
            made += 1
            yield options, length, initial, plan, rng


def test_adding_a_station_never_increases_the_cost():
    for options, length, initial, plan, rng in feasible_instances(1):
        extra = FuelOption(rng.uniform(0, length), rng.choice([0, 1, 4]), rng.uniform(2.6, 4.6))
        assert solve(options + [extra], length, initial).cost <= plan.cost + TOL


def test_removing_a_station_never_decreases_the_cost_and_unused_ones_change_nothing():
    for options, length, initial, plan, _ in feasible_instances(2):
        used = {s.option_index for s in plan.stops}
        for index in range(len(options)):
            remaining = options[:index] + options[index + 1 :]
            try:
                cost = solve(remaining, length, initial).cost
            except NoFeasiblePlanError:
                assert index in used  # only a station the plan relies on can make it infeasible
                continue
            assert cost >= plan.cost - TOL
            if index not in used:
                assert cost == pytest.approx(plan.cost, abs=TOL)


def test_more_starting_fuel_never_increases_the_cost():
    for options, length, initial, plan, rng in feasible_instances(3):
        more = min(CAP_MILES, initial + rng.uniform(0, 150))
        assert solve(options, length, more).cost <= plan.cost + TOL


def test_a_bigger_tank_never_increases_the_cost_or_breaks_feasibility():
    for options, length, initial, plan, _ in feasible_instances(4):
        bigger = solve(options, length, initial, cap=CAP_MILES + 100)
        assert bigger.cost <= plan.cost + TOL


def test_a_cheaper_station_never_increases_the_cost():
    for options, length, initial, plan, rng in feasible_instances(5):
        index = rng.randrange(len(options))
        cheaper = list(options)
        cheaper[index] = replace(options[index], price=options[index].price * rng.uniform(0.5, 1.0))
        assert solve(cheaper, length, initial).cost <= plan.cost + TOL


def test_a_dearer_station_never_decreases_the_cost():
    for options, length, initial, plan, rng in feasible_instances(6):
        index = rng.randrange(len(options))
        dearer = list(options)
        dearer[index] = replace(options[index], price=options[index].price * rng.uniform(1.0, 2.0))
        assert solve(dearer, length, initial).cost >= plan.cost - TOL


def test_a_station_closer_to_the_route_never_increases_the_cost():
    for options, length, initial, plan, rng in feasible_instances(7):
        index = rng.randrange(len(options))
        closer = list(options)
        closer[index] = replace(options[index], off_route_miles=options[index].off_route_miles / 2)
        assert solve(closer, length, initial).cost <= plan.cost + TOL


def test_prices_scale_linearly():
    for options, length, initial, plan, rng in feasible_instances(8):
        factor = rng.uniform(0.3, 3.0)
        scaled = [replace(o, price=o.price * factor) for o in options]
        assert solve(scaled, length, initial).cost == pytest.approx(plan.cost * factor, rel=1e-9)


def test_the_order_of_the_input_list_does_not_matter():
    for options, length, initial, plan, rng in feasible_instances(9):
        shuffled = list(options)
        rng.shuffle(shuffled)
        assert solve(shuffled, length, initial).cost == pytest.approx(plan.cost, abs=TOL)


def test_uniform_prices_and_no_detours_cost_exactly_the_missing_fuel():
    """With one price everywhere the only question is how much fuel is needed: p*(L-f0)/mpg."""
    rng = random.Random(10)
    checked = 0
    for _ in range(60):
        stations, length, initial = chain_instance(rng, infeasible_bias=0.0)
        price = round(rng.uniform(2.7, 4.6), 3)
        options = [FuelOption(s["mile"], 0.0, price) for s in stations]
        try:
            result = solve(options, length, initial)
        except NoFeasiblePlanError:
            continue
        needed = max(0.0, length - initial) / MPG
        assert result.cost == pytest.approx(price * needed, rel=1e-9, abs=1e-9)
        checked += 1
    assert checked > 30


def test_detours_can_only_add_to_the_uniform_price_cost():
    rng = random.Random(11)
    for _ in range(40):
        stations, length, initial = chain_instance(rng, infeasible_bias=0.0)
        price = 3.5
        options = [FuelOption(s["mile"], s["off"], price) for s in stations]
        try:
            result = solve(options, length, initial)
        except NoFeasiblePlanError:
            continue
        floor = price * max(0.0, length - initial) / MPG
        assert result.cost >= floor - TOL
        detour_miles = sum(
            options[s.option_index].off_route_miles * 2 for s in result.stops
        )  # out and back, for each stop made
        assert result.cost <= floor + price * detour_miles / MPG + TOL


def test_a_plan_never_buys_more_than_the_trip_needs_plus_nothing_left_over():
    """No fuel is left at the destination (buying it would only waste money)."""
    for _options, _length, initial, plan, _ in feasible_instances(12):
        total_bought = sum(s.purchase_miles for s in plan.stops)
        driven = sum(s.distance_from_previous_miles for s in plan.stops) + plan.final_leg_miles
        assert initial + total_bought - driven == pytest.approx(0.0, abs=1e-6)


def test_no_stop_is_pointless():
    """Every stop buys fuel (a stop that buys nothing is a wasted detour)."""
    for _, _, _, plan, _ in feasible_instances(13):
        assert all(s.purchase_miles > 1e-9 for s in plan.stops)


def test_every_stop_obeys_the_two_buying_rules():
    """At every stop the plan either fills the tank or leaves exactly enough to reach the
    next stop (or the destination) - never an arbitrary amount in between."""
    for _, _, _, plan, _ in feasible_instances(14):
        legs = [s.distance_from_previous_miles for s in plan.stops[1:]] + [plan.final_leg_miles]
        for stop, next_leg in zip(plan.stops, legs, strict=True):
            after = stop.fuel_on_arrival_miles + stop.purchase_miles
            assert after == pytest.approx(CAP_MILES, abs=1e-6) or after == pytest.approx(
                next_leg, abs=1e-6
            )


def test_optimum_is_never_worse_than_common_driver_strategies():
    from .baselines import STRATEGIES

    rng = random.Random(21)
    compared = {name: 0 for name in STRATEGIES}
    strictly_better = {name: 0 for name in STRATEGIES}
    for options, length, initial, plan, _ in feasible_instances(21, count=80):
        for name, strategy in STRATEGIES.items():
            cost = strategy(options, length, CAP_MILES, MPG, initial)
            if cost is None:  # the heuristic got stuck; the optimum still found a plan
                continue
            compared[name] += 1
            assert plan.cost <= cost + TOL, f"{name}: optimum {plan.cost} > heuristic {cost}"
            strictly_better[name] += plan.cost < cost - 0.01
    del rng
    for name in STRATEGIES:
        assert compared[name] >= 20, name
        # The comparison must be meaningful: the optimum usually beats a naive strategy.
        assert strictly_better[name] >= compared[name] * 0.3, name
