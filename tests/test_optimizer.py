import math
import random
import time

import pytest

from fuelplanner.errors import NoFeasiblePlanError
from fuelplanner.services.optimizer import FuelOption, plan_fuel_stops

from .brute_force import brute_force_cost, integer_sequence_cost, lp_sequence_cost

CAP = 500.0
MPG = 10.0


def opt(mile, price, off=0.0):
    return FuelOption(float(mile), float(off), float(price))


def plan(options, length, f0=None, cap=CAP):
    return plan_fuel_stops(options, float(length), max_range=cap, mpg=MPG, initial_fuel_miles=f0)


def mile_marks(result, options):
    return [options[s.option_index].route_mile for s in result.stops]


# --- the worked example from the design document ---------------------------------------


def test_worked_example_skips_the_unreachable_cheapest_and_buys_just_enough():
    options = [
        opt(150, 3.80), opt(420, 3.40), opt(480, 3.10),
        opt(700, 3.90), opt(850, 2.95), opt(980, 3.60),
    ]  # fmt: skip
    result = plan(options, 1100)
    assert mile_marks(result, options) == [480, 850]
    first, second = result.stops
    assert first.fuel_on_arrival_miles == pytest.approx(20)
    assert first.purchase_miles == pytest.approx(350)  # 35 gal, exactly enough to reach 850
    assert second.fuel_on_arrival_miles == pytest.approx(0)
    assert second.purchase_miles == pytest.approx(250)  # 25 gal for the last 250 miles
    assert result.cost == pytest.approx(35 * 3.10 + 25 * 2.95)  # $182.25
    assert result.final_leg_miles == pytest.approx(250)


# --- basic situations -----------------------------------------------------------------


def test_short_route_needs_no_stops_even_with_cheap_stations_available():
    result = plan([opt(100, 1.0)], 450)
    assert result.stops == () and result.cost == 0
    assert result.final_leg_miles == 450


def test_route_exactly_at_range_needs_no_stops():
    assert plan([opt(250, 3.0)], 500).stops == ()


def test_one_stop_when_the_route_is_longer_than_a_tank():
    options = [opt(300, 3.00)]
    result = plan(options, 700)
    assert mile_marks(result, options) == [300]
    assert result.stops[0].fuel_on_arrival_miles == pytest.approx(200)
    assert result.stops[0].purchase_miles == pytest.approx(200)  # 20 gal
    assert result.cost == pytest.approx(60.0)


def test_several_stops_on_a_long_route_with_uniform_prices():
    options = [opt(m, 3.0) for m in range(100, 2000, 100)]
    result = plan(options, 2000)
    # Fuel needed is fixed by the distance: (2000 - 500) / 10 gal at $3.
    assert result.cost == pytest.approx(150 * 3.0)
    assert len(result.stops) >= 3
    assert all(s.distance_from_previous_miles <= CAP + 1e-9 for s in result.stops)
    assert result.final_leg_miles <= CAP + 1e-9


def test_cheaper_station_farther_ahead_is_preferred_over_a_nearer_expensive_one():
    options = [opt(100, 4.50), opt(400, 3.00)]
    result = plan(options, 800)
    assert mile_marks(result, options) == [400]
    assert result.cost == pytest.approx(30 * 3.00)  # arrive with 100 mi, buy 300 mi


def test_buys_only_enough_to_reach_a_cheaper_station_when_it_is_in_range():
    options = [opt(200, 3.50), opt(450, 3.00)]
    result = plan(options, 950, f0=250)  # 450 is out of reach from the start
    assert mile_marks(result, options) == [200, 450]
    assert result.stops[0].fuel_on_arrival_miles == pytest.approx(50)
    assert result.stops[0].purchase_miles == pytest.approx(200)  # just enough: 250 mi to go
    assert result.stops[1].fuel_on_arrival_miles == pytest.approx(0)
    assert result.cost == pytest.approx(20 * 3.50 + 50 * 3.00)


def test_a_full_tank_start_skips_an_early_expensive_stop():
    options = [opt(200, 3.50), opt(450, 3.00)]
    result = plan(options, 950)
    assert mile_marks(result, options) == [450]
    assert result.cost == pytest.approx(45 * 3.00)


def test_fills_up_at_a_cheap_station_when_nothing_cheaper_is_reachable():
    options = [opt(300, 2.50), opt(750, 3.50)]
    result = plan(options, 1200)
    first = result.stops[0]
    assert first.fuel_on_arrival_miles == pytest.approx(200)
    assert first.purchase_miles == pytest.approx(300)  # filled to the 500-mile limit
    assert first.fuel_on_arrival_miles + first.purchase_miles == pytest.approx(CAP)


def test_cheap_but_unreachable_station_is_not_used_directly():
    options = [opt(450, 4.00), opt(700, 1.00)]
    result = plan(options, 1000)
    assert mile_marks(result, options) == [450, 700]  # must hop through the pricey one
    assert result.stops[0].fuel_on_arrival_miles == pytest.approx(50)
    assert result.stops[0].purchase_miles == pytest.approx(200)  # only to reach 700 empty
    assert result.cost == pytest.approx(20 * 4.00 + 30 * 1.00)


def test_station_beyond_first_tank_range_is_unreachable_from_the_start():
    with pytest.raises(NoFeasiblePlanError):
        plan([opt(501, 1.0)], 900)


def test_excessive_detour_makes_a_cheaper_station_not_worth_it():
    near = opt(300, 3.20)
    far_cheap = opt(250, 3.00, off=40)
    result = plan([far_cheap, near], 700)
    assert result.stops[0].option_index == 1  # the on-route station, not the 40-mile detour
    assert result.cost == pytest.approx(20 * 3.20)


def test_detour_that_pays_off_is_taken():
    cheap_detour = opt(300, 2.00, off=10)
    on_route = opt(300, 3.50)
    result = plan([cheap_detour, on_route], 700)
    assert result.stops[0].option_index == 0
    # arrive with 500 - 310 = 190 mi, then 10 + 400 = 410 mi to the end -> buy 220 mi
    assert result.cost == pytest.approx(22 * 2.00)
    assert result.final_leg_miles == pytest.approx(410)


def test_detour_distance_counts_against_range():
    # Only reachable if the 30-mile detour (both ways) fits: 470 + 30 > 500.
    with pytest.raises(NoFeasiblePlanError):
        plan([opt(480, 1.0, off=30)], 900)
    assert plan([opt(470, 1.0, off=30)], 900).stops  # exactly 500 miles to arrive


def test_ties_prefer_fewer_stops():
    options = [opt(300, 3.0), opt(350, 3.0)]
    result = plan(options, 800)
    assert len(result.stops) == 1
    assert result.cost == pytest.approx(30 * 3.0)


def test_unsorted_options_report_original_indices():
    options = [opt(850, 2.95), opt(150, 3.80), opt(480, 3.10), opt(420, 3.40)]
    result = plan(options, 1100)
    assert [s.option_index for s in result.stops] == [2, 0]


# --- initial fuel -----------------------------------------------------------------------


def test_starting_with_a_partial_tank():
    options = [opt(100, 3.0)]
    result = plan(options, 600, f0=200)
    assert mile_marks(result, options) == [100]
    assert result.stops[0].fuel_on_arrival_miles == pytest.approx(100)
    assert result.stops[0].purchase_miles == pytest.approx(400)  # 500 more miles needed total...
    assert result.cost == pytest.approx(40 * 3.0)


def test_starting_empty_requires_a_station_at_the_origin():
    assert plan([opt(0, 3.0)], 300, f0=0).cost == pytest.approx(30 * 3.0)
    with pytest.raises(NoFeasiblePlanError):
        plan([opt(5, 3.0)], 300, f0=0)


def test_invalid_initial_fuel_is_rejected():
    with pytest.raises(ValueError):
        plan([], 100, f0=600)
    with pytest.raises(ValueError):
        plan([], 100, f0=-5)


# --- infeasible routes ---------------------------------------------------------------------


def test_no_stations_on_a_long_route_reports_the_gap():
    with pytest.raises(NoFeasiblePlanError) as excinfo:
        plan([], 900)
    err = excinfo.value
    assert err.code == "no_feasible_fuel_plan" and err.http_status == 422
    assert err.details["next_option_is_destination"] is True
    assert err.details["shortfall_miles"] == pytest.approx(400)
    assert err.details["candidate_stations"] == 0


def test_gap_between_stations_larger_than_range_is_located():
    options = [opt(100, 3.0), opt(400, 3.0), opt(1000, 3.0)]
    with pytest.raises(NoFeasiblePlanError) as excinfo:
        plan(options, 1200)
    d = excinfo.value.details
    assert d["last_reachable_route_mile"] == 400
    assert d["next_option_route_mile"] == 1000
    assert d["distance_to_next_option_miles"] == pytest.approx(600)
    assert d["shortfall_miles"] == pytest.approx(100)
    assert "route mile 400" in excinfo.value.message


def test_infeasible_when_final_station_is_too_far_from_the_destination():
    with pytest.raises(NoFeasiblePlanError) as excinfo:
        plan([opt(400, 3.0)], 1000)
    assert excinfo.value.details["next_option_is_destination"] is True


# --- correctness against exhaustive search ------------------------------------------------


def random_instance(rng):
    cap = rng.randint(10, 30)
    length = rng.randint(cap // 2, 3 * cap)
    n = rng.randint(0, 6)
    stations = [
        {
            "mile": rng.randint(0, length),
            "off": rng.choice([0, 0, 0, 1, 2, 3, cap // 5]),
            "price": rng.choice([2.0, 2.5, 3.0, 3.5, 4.0, 4.5]) + rng.choice([0, 0, 0.1, 0.25]),
        }
        for _ in range(n)
    ]
    f0 = rng.choice([cap, cap, rng.randint(0, cap)])
    return cap, length, stations, f0


def validate_plan(result, stations, length, cap, f0):
    """Re-simulate a plan; return its recomputed cost. Independent of the optimiser's logic."""
    fuel, cost, previous = f0, 0.0, None
    for stop in result.stops:
        st = stations[stop.option_index]
        d = (
            st["mile"] + st["off"]
            if previous is None
            else (previous["off"] + st["mile"] - previous["mile"] + st["off"])
        )
        fuel -= d
        assert fuel >= -1e-9, "ran out of fuel"
        assert d <= cap + 1e-9
        assert stop.fuel_on_arrival_miles == pytest.approx(fuel, abs=1e-9)
        assert stop.purchase_miles >= -1e-9
        fuel += stop.purchase_miles
        assert fuel <= cap + 1e-9, "tank overfilled"
        cost += stop.purchase_miles * st["price"] / MPG
        previous = st
    last = 0 if previous is None else previous["off"] + length - previous["mile"]
    end = length if previous is None else last
    fuel -= end
    assert fuel >= -1e-9, "ran out of fuel before the destination"
    return cost


@pytest.mark.parametrize("seed", range(8))
def test_matches_exhaustive_search_on_random_instances(seed):
    rng = random.Random(seed)
    feasible = infeasible = 0
    for case in range(120):
        cap, length, stations, f0 = random_instance(rng)
        options = [FuelOption(s["mile"], s["off"], s["price"]) for s in stations]
        expected = brute_force_cost(stations, length, cap, MPG, f0)
        label = f"seed={seed} case={case} cap={cap} L={length} f0={f0} stations={stations}"
        if math.isinf(expected):
            infeasible += 1
            with pytest.raises(NoFeasiblePlanError):
                plan_fuel_stops(options, length, max_range=cap, mpg=MPG, initial_fuel_miles=f0)
            continue
        feasible += 1
        result = plan_fuel_stops(options, length, max_range=cap, mpg=MPG, initial_fuel_miles=f0)
        assert result.cost == pytest.approx(expected, abs=1e-7), label
        assert validate_plan(result, stations, length, cap, f0) == pytest.approx(
            result.cost, abs=1e-7
        ), label
    assert feasible > 40 and infeasible > 3  # the generator exercises both outcomes


def test_exhaustive_reference_agrees_with_a_continuous_linear_programme():
    """The brute force assumes whole-mile fuel levels; the LP shows that loses nothing."""
    rng = random.Random(99)
    compared = 0
    for _ in range(150):
        cap, length, stations, f0 = random_instance(rng)
        stations = sorted(stations, key=lambda s: s["mile"])
        subset = [s for s in stations if rng.random() < 0.7]
        integer = integer_sequence_cost(subset, length, cap, MPG, f0)
        continuous = lp_sequence_cost(subset, length, cap, MPG, f0)
        if math.isinf(integer) or math.isinf(continuous):
            assert math.isinf(integer) and math.isinf(continuous)
            continue
        assert integer == pytest.approx(continuous, abs=1e-6)
        compared += 1
    assert compared > 50


def test_plan_invariants_on_larger_random_routes_and_cost_lower_bound():
    rng = random.Random(7)
    for _ in range(40):
        length = rng.randint(600, 3000)
        stations = [
            {
                "mile": rng.uniform(0, length),
                "off": rng.choice([0, 0.5, 1, 2, 4]),
                "price": round(rng.uniform(2.7, 4.5), 3),
            }
            for _ in range(rng.randint(10, 60))
        ]
        options = [FuelOption(s["mile"], s["off"], s["price"]) for s in stations]
        try:
            result = plan(options, length)
        except NoFeasiblePlanError:
            continue
        recomputed = validate_plan(result, stations, length, CAP, CAP)
        assert recomputed == pytest.approx(result.cost, rel=1e-9, abs=1e-9)
        # Cannot beat buying all the missing fuel at the cheapest price on offer.
        cheapest = min(s["price"] for s in stations)
        assert result.cost >= (length - CAP) / MPG * cheapest - 1e-6


def test_optimiser_is_fast_with_hundreds_of_candidates():
    rng = random.Random(3)
    options = [
        opt(m, round(rng.uniform(2.7, 4.5), 3), off=rng.choice([0, 1, 2, 5]))
        for m in sorted(
            rng.uniform(
                0,
                3000,
            )
            for _ in range(300)
        )
    ]
    start = time.perf_counter()
    result = plan(options, 3000)
    elapsed = time.perf_counter() - start
    assert len(result.stops) >= 4
    assert elapsed < 0.5
