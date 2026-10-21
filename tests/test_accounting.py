import random
from dataclasses import replace
from decimal import Decimal

import pytest

from fuelplanner.errors import NoFeasiblePlanError, PlanInvariantError
from fuelplanner.services.accounting import build_ledger
from fuelplanner.services.optimizer import (
    FuelOption,
    OptimalPlan,
    PlannedStop,
    plan_fuel_stops,
)
from fuelplanner.services.stations import Candidate, StationRecord

MPG = 10.0
CAP = 500.0


def candidate(opis_id, mile, price, off=0.0):
    record = StationRecord(
        opis_id, f"Station {opis_id}", "I-1", "Town", "NE", Decimal(str(price)), 40.0, -95.0
    )
    return Candidate(record, float(mile), float(off))


def solve(candidates, length, f0=None):
    options = [
        FuelOption(c.route_mile, c.off_route_miles, float(c.station.price)) for c in candidates
    ]
    plan = plan_fuel_stops(options, length, max_range=CAP, mpg=MPG, initial_fuel_miles=f0)
    return plan, build_ledger(plan, candidates, route_length=length, max_range=CAP, mpg=MPG)


WORKED_EXAMPLE = [
    candidate(1, 150, "3.80"),
    candidate(2, 420, "3.40"),
    candidate(3, 480, "3.10"),
    candidate(4, 700, "3.90"),
    candidate(5, 850, "2.95"),
    candidate(6, 980, "3.60"),
]


def test_worked_example_ledger_matches_hand_calculation():
    _, ledger = solve(WORKED_EXAMPLE, 1100)
    first, second = ledger.stops
    assert [s.candidate.station.opis_id for s in ledger.stops] == [3, 5]
    assert first.gallons_purchased == pytest.approx(Decimal("35"))
    assert first.cost == Decimal("108.50")
    assert second.gallons_purchased == pytest.approx(Decimal("25"))
    assert second.cost == Decimal("73.75")
    assert ledger.total_cost == Decimal("182.25")
    assert [round(float(leg.fuel_used_gallons), 6) for leg in ledger.legs] == [48, 37, 25]
    assert [leg.distance_miles for leg in ledger.legs] == pytest.approx([480, 370, 250])
    assert (ledger.legs[0].from_label, ledger.legs[0].to_label) == ("start", "stop 1")
    assert (ledger.legs[-1].from_label, ledger.legs[-1].to_label) == ("stop 2", "finish")
    # Fuel: 50 gal at the start + 60 bought - 110 burned = 0 left.
    assert ledger.initial_fuel_gallons == 50
    assert ledger.fuel_purchased_gallons == pytest.approx(Decimal("60"))
    assert ledger.fuel_used_gallons == pytest.approx(Decimal("110"))
    assert float(ledger.fuel_remaining_gallons) == pytest.approx(0, abs=1e-9)
    assert first.fuel_on_arrival_gallons == pytest.approx(Decimal("2"))
    assert first.fuel_on_departure_gallons == pytest.approx(Decimal("37"))


def test_no_stop_ledger_is_free_and_has_a_single_leg():
    _, ledger = solve([candidate(1, 100, "3.0")], 450)
    assert ledger.stops == () and ledger.total_cost == Decimal("0.00")
    assert len(ledger.legs) == 1
    assert ledger.legs[0].distance_miles == 450
    assert ledger.fuel_used_gallons == pytest.approx(Decimal("45"))
    assert ledger.fuel_remaining_gallons == pytest.approx(Decimal("5"))


def test_detour_miles_are_driven_and_paid_for():
    on_route = candidate(1, 300, "3.50")
    detour = candidate(2, 300, "2.00", off=20)
    _, ledger = solve([on_route, detour], 700)
    (stop,) = ledger.stops
    assert stop.candidate.station.opis_id == 2
    assert ledger.driving_distance_miles == pytest.approx(700 + 40)
    assert ledger.driving_distance_miles - ledger.route_distance_miles == pytest.approx(40)
    assert ledger.legs[0].distance_miles == pytest.approx(320)


def test_money_is_exact_decimal_rounded_per_stop_and_total_is_the_sum_of_lines():
    cands = [candidate(1, 300, "3.00733333"), candidate(2, 700, "3.28733333")]
    _, ledger = solve(cands, 1100)
    assert all(s.cost == s.cost.quantize(Decimal("0.01")) for s in ledger.stops)
    assert ledger.total_cost == sum((s.cost for s in ledger.stops), Decimal("0"))
    for s in ledger.stops:
        assert s.cost == (s.gallons_purchased * s.price_per_gallon).quantize(Decimal("0.01"))


def test_start_with_partial_tank_reconciles():
    _, ledger = solve([candidate(1, 100, "3.0")], 600, f0=200)
    assert ledger.initial_fuel_gallons == pytest.approx(Decimal("20"))
    balance = (
        ledger.initial_fuel_gallons
        + ledger.fuel_purchased_gallons
        - ledger.fuel_used_gallons
        - ledger.fuel_remaining_gallons
    )
    assert abs(balance) < Decimal("1e-9")


# --- the ledger refuses to present an invalid plan ------------------------------------------


def valid_plan_and_candidates():
    plan, _ = solve(WORKED_EXAMPLE, 1100)
    return plan


def test_ledger_detects_running_out_of_fuel():
    plan = valid_plan_and_candidates()
    broken = replace(
        plan, stops=(replace(plan.stops[0], distance_from_previous_miles=520.0), *plan.stops[1:])
    )
    with pytest.raises(PlanInvariantError, match="ran out of fuel"):
        build_ledger(broken, WORKED_EXAMPLE, route_length=1100)


def test_ledger_detects_an_overfilled_tank():
    plan = valid_plan_and_candidates()
    broken = replace(plan, stops=(plan.stops[0], replace(plan.stops[1], purchase_miles=600.0)))
    with pytest.raises(PlanInvariantError, match="overfilled"):
        build_ledger(broken, WORKED_EXAMPLE, route_length=1100)


def test_ledger_detects_not_enough_fuel_for_the_final_leg():
    plan = valid_plan_and_candidates()
    broken = replace(plan, stops=(plan.stops[0], replace(plan.stops[1], purchase_miles=100.0)))
    with pytest.raises(PlanInvariantError, match="destination"):
        build_ledger(broken, WORKED_EXAMPLE, route_length=1100)


def test_ledger_detects_disagreement_with_the_optimiser():
    plan = valid_plan_and_candidates()
    bad_stop = replace(plan.stops[0], fuel_on_arrival_miles=300.0)
    broken = OptimalPlan((bad_stop, plan.stops[1]), plan.final_leg_miles, plan.cost, 500.0)
    with pytest.raises(PlanInvariantError, match="disagrees"):
        build_ledger(broken, WORKED_EXAMPLE, route_length=1100)


def test_ledger_detects_negative_purchases():
    stop = PlannedStop(0, 100.0, 400.0, -5.0)
    broken = OptimalPlan((stop,), 400.0, 0.0, 500.0)
    with pytest.raises(PlanInvariantError, match="negative purchase"):
        build_ledger(broken, [candidate(1, 100, "3.0")], route_length=500)


# --- properties over random journeys --------------------------------------------------------


def test_every_random_plan_is_feasible_and_reconciles():
    rng = random.Random(2024)
    checked = 0
    for _ in range(150):
        length = rng.uniform(100, 3000)
        cands = [
            candidate(
                i + 1,
                rng.uniform(0, length),
                f"{rng.uniform(2.7, 4.6):.3f}",
                off=rng.choice([0, 0.4, 1.5, 3, 5]),
            )
            for i in range(rng.randint(5, 50))
        ]
        f0 = rng.choice([None, rng.uniform(0, CAP)])
        try:
            plan, ledger = solve(cands, length, f0)
        except NoFeasiblePlanError:
            continue
        checked += 1
        assert all(leg.distance_miles <= CAP + 1e-6 for leg in ledger.legs)
        assert ledger.legs[0].distance_miles <= (CAP if f0 is None else f0) + 1e-6
        balance = (
            ledger.initial_fuel_gallons
            + ledger.fuel_purchased_gallons
            - ledger.fuel_used_gallons
            - ledger.fuel_remaining_gallons
        )
        assert abs(balance) < Decimal("1e-6")
        assert ledger.fuel_remaining_gallons >= 0
        assert sum(s.cost for s in ledger.stops) == ledger.total_cost
        assert float(ledger.total_cost) == pytest.approx(
            plan.cost, abs=0.005 * len(plan.stops) + 1e-6
        )
        assert [s.candidate.route_mile for s in ledger.stops] == sorted(
            s.candidate.route_mile for s in ledger.stops
        )
        assert ledger.driving_distance_miles >= length - 1e-6
    assert checked > 60
