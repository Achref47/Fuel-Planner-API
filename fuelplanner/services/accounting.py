"""Turn an optimiser plan into an auditable fuel/cost ledger.

The optimiser works in floating-point miles to choose stops. This module re-derives
everything from first principles - driving the route leg by leg, burning fuel, buying fuel -
and verifies that the plan is physically valid (never out of fuel, never over capacity) and
that fuel and money reconcile, before any number is shown to a client. Money uses ``Decimal``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from ..errors import PlanInvariantError
from .optimizer import EPS, OptimalPlan
from .stations import Candidate

CENT = Decimal("0.01")
# Float noise tolerance for physical checks (miles / gallons).
TOLERANCE = 1e-6


@dataclass(frozen=True)
class StopAccount:
    sequence: int  # 1-based order along the route
    candidate: Candidate
    distance_from_previous_miles: float
    fuel_on_arrival_gallons: Decimal
    gallons_purchased: Decimal
    fuel_on_departure_gallons: Decimal
    price_per_gallon: Decimal
    cost: Decimal  # gallons_purchased * price, rounded to cents


@dataclass(frozen=True)
class LegAccount:
    sequence: int  # 1-based; the last leg ends at the destination
    from_label: str  # "start" or "stop N"
    to_label: str  # "stop N" or "finish"
    distance_miles: float
    fuel_used_gallons: Decimal


@dataclass(frozen=True)
class FuelLedger:
    stops: tuple[StopAccount, ...]
    legs: tuple[LegAccount, ...]
    route_distance_miles: float
    driving_distance_miles: float  # route + all detours
    initial_fuel_gallons: Decimal
    fuel_used_gallons: Decimal
    fuel_purchased_gallons: Decimal
    fuel_remaining_gallons: Decimal  # on arrival at the destination
    total_cost: Decimal  # sum of the per-stop (cent-rounded) costs


def _gallons(miles: float, mpg: float) -> Decimal:
    return Decimal(miles) / Decimal(mpg)


def build_ledger(
    plan: OptimalPlan,
    candidates: list[Candidate],
    *,
    route_length: float,
    max_range: float = 500.0,
    mpg: float = 10.0,
) -> FuelLedger:
    """Simulate the plan and return the ledger; raise ``PlanInvariantError`` if it is invalid.

    ``candidates`` must be the same list (same order) that was fed to the optimiser.
    """
    fuel = plan.initial_fuel_miles  # miles of range currently in the tank
    stops: list[StopAccount] = []
    legs: list[LegAccount] = []
    previous_label = "start"
    driving = 0.0
    purchased_miles = 0.0

    def violation(message: str) -> PlanInvariantError:
        return PlanInvariantError(f"Internal consistency check failed: {message}")

    # Every leg is at most one tank: the vehicle never carries more than ``max_range`` miles of
    # fuel (checked here and after each purchase) and never runs dry (checked after each leg).
    if not -TOLERANCE <= fuel <= max_range + TOLERANCE:
        raise violation(f"starting fuel {fuel:.3f} mi is outside the 0-{max_range} mi tank")

    for number, planned in enumerate(plan.stops, start=1):
        candidate = candidates[planned.option_index]
        leg = planned.distance_from_previous_miles
        fuel -= leg
        if fuel < -TOLERANCE:
            raise violation(f"ran out of fuel before stop {number} ({fuel:.6f} mi)")
        fuel = max(fuel, 0.0)
        if abs(fuel - planned.fuel_on_arrival_miles) > 1e-4:
            raise violation(
                f"arrival fuel at stop {number} disagrees with the optimiser "
                f"({fuel:.6f} vs {planned.fuel_on_arrival_miles:.6f} mi)"
            )
        if planned.purchase_miles < -TOLERANCE:
            raise violation(f"negative purchase at stop {number}")
        arrival = fuel
        fuel += max(planned.purchase_miles, 0.0)
        if fuel > max_range + TOLERANCE:
            raise violation(f"tank overfilled at stop {number} ({fuel:.6f} > {max_range} mi)")

        price = candidate.station.price
        gallons = _gallons(max(planned.purchase_miles, 0.0), mpg)
        stops.append(
            StopAccount(
                sequence=number,
                candidate=candidate,
                distance_from_previous_miles=leg,
                fuel_on_arrival_gallons=_gallons(arrival, mpg),
                gallons_purchased=gallons,
                fuel_on_departure_gallons=_gallons(fuel, mpg),
                price_per_gallon=price,
                cost=(gallons * price).quantize(CENT, rounding=ROUND_HALF_UP),
            )
        )
        label = f"stop {number}"
        legs.append(LegAccount(number, previous_label, label, leg, _gallons(leg, mpg)))
        previous_label = label
        driving += leg
        purchased_miles += max(planned.purchase_miles, 0.0)

    final_leg = plan.final_leg_miles
    fuel -= final_leg
    if fuel < -TOLERANCE:
        raise violation(f"ran out of fuel before the destination ({fuel:.6f} mi)")
    fuel = max(fuel, 0.0)
    legs.append(
        LegAccount(len(legs) + 1, previous_label, "finish", final_leg, _gallons(final_leg, mpg))
    )
    driving += final_leg

    initial = _gallons(plan.initial_fuel_miles, mpg)
    used = _gallons(driving, mpg)
    bought = _gallons(purchased_miles, mpg)
    remaining = _gallons(fuel, mpg)
    if abs(float(initial + bought - used - remaining)) > TOLERANCE:  # pragma: no cover
        # Defensive: the simulation above makes this an identity, so it can only fire if the
        # bookkeeping itself is broken.
        raise violation("fuel does not reconcile (initial + purchased - used != remaining)")
    if driving + EPS < route_length - TOLERANCE:
        raise violation("driving distance is shorter than the route")

    total = sum((s.cost for s in stops), Decimal("0.00"))
    return FuelLedger(
        stops=tuple(stops),
        legs=tuple(legs),
        route_distance_miles=route_length,
        driving_distance_miles=driving,
        initial_fuel_gallons=initial,
        fuel_used_gallons=used,
        fuel_purchased_gallons=bought,
        fuel_remaining_gallons=remaining,
        total_cost=total,
    )
