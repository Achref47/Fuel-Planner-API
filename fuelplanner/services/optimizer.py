"""Cost-minimising fuel-stop planner (pure Python, no Django).

Model
-----
* The vehicle drives a route of ``route_length`` miles. A tank holds ``max_range`` miles of
  fuel (500 mi = 50 gal at 10 mpg). It starts with ``initial_fuel_miles`` of fuel and may
  arrive anywhere - including the destination - with an empty tank.
* A candidate station sits at ``route_mile`` along the route, ``off_route_miles`` away from
  it, and sells fuel at ``price`` USD/gallon. Fuel can be bought in any amount.
* Visiting a station means leaving the route, driving ``off_route_miles`` to it and the same
  distance back, so the distance between consecutive stops ``i -> j`` is
  ``off_i + (mile_j - mile_i) + off_j``; from the start it is ``mile_j + off_j``; to the
  destination it is ``off_i + (length - mile_i)``. (A conservative straight-in/straight-out
  model of a detour.) Stations are visited in route order.
* Fuel carried is burned at ``1 / mpg`` gallons per mile, detours included.

Algorithm (dynamic programme over stations, O(n * m * log m) for n stations with m of them
within range of each other)
----------------------------------------------------------------------------------------
Two facts reduce the search space:

1. *Stopping without buying is never useful.* Skipping a station never lengthens the trip
   (detours obey ``D(i,k) <= D(i,j) + D(j,k)``), so every stop in an optimal plan buys fuel.
2. *Between two consecutive buying stops ``i -> j`` an optimal plan either fills the tank at
   ``i`` or buys just enough to reach ``j`` empty.* Otherwise purchases could be shifted
   between the two stations without raising cost (towards the cheaper one) until one of
   those two conditions holds.

So the fuel on arrival at a stop ``j`` can only be 0 (previous stop bought "just enough"),
``max_range - D(i, j)`` (previous stop ``i`` filled up), or ``initial - D(start, j)``.
The DP keeps, per station, the cheapest known way to arrive in each of those states, and
relaxes the two transitions from every state. Ties prefer fewer stops.

The module-level claim is tested, not assumed: ``tests/test_optimizer.py`` compares the
result with an exhaustive search over every subset of stations (with an independent
fuel-purchase solver) on thousands of random instances.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from ..errors import NoFeasiblePlanError

EPS = 1e-9
COST_TOLERANCE = 1e-9


@dataclass(frozen=True)
class FuelOption:
    """A candidate station as seen by the optimiser."""

    route_mile: float
    off_route_miles: float
    price: float  # USD per gallon


@dataclass(frozen=True)
class PlannedStop:
    option_index: int  # index into the ``options`` list passed to ``plan_fuel_stops``
    distance_from_previous_miles: float  # driven since the previous stop (or the start)
    fuel_on_arrival_miles: float
    purchase_miles: float  # fuel bought here, expressed as miles of range


@dataclass(frozen=True)
class OptimalPlan:
    stops: tuple[PlannedStop, ...]
    final_leg_miles: float  # last stop (or the start) -> destination
    cost: float  # USD, floating point (exact money is computed in accounting.py)
    initial_fuel_miles: float


class _State:
    """Cheapest known way of arriving at a station in a given fuel state."""

    __slots__ = ("buy", "cost", "fuel", "parent", "station", "stops")

    def __init__(self, station, fuel, cost, stops, parent, buy):
        self.station = station  # sorted-station index, -1 for the start
        self.fuel = fuel  # fuel on arrival, in miles
        self.cost = cost  # money spent before arriving, USD
        self.stops = stops  # stops made so far, including this one
        self.parent = parent
        self.buy = buy  # miles of fuel bought at the parent to arrive here


def _better(cost_a: float, stops_a: int, cost_b: float, stops_b: int) -> bool:
    """True if (cost_a, stops_a) is strictly preferable to (cost_b, stops_b)."""
    if cost_a < cost_b - COST_TOLERANCE:
        return True
    return abs(cost_a - cost_b) <= COST_TOLERANCE and stops_a < stops_b


def plan_fuel_stops(
    options: list[FuelOption],
    route_length: float,
    *,
    max_range: float = 500.0,
    mpg: float = 10.0,
    initial_fuel_miles: float | None = None,
) -> OptimalPlan:
    """Return the cheapest feasible set of fuel stops, or raise ``NoFeasiblePlanError``."""
    if max_range <= 0 or mpg <= 0 or route_length < 0:
        raise ValueError("max_range, mpg must be positive and route_length non-negative")
    start_fuel = max_range if initial_fuel_miles is None else float(initial_fuel_miles)
    if not -EPS <= start_fuel <= max_range + EPS:
        raise ValueError("initial_fuel_miles must be between 0 and max_range")
    start_fuel = min(max(start_fuel, 0.0), max_range)

    if route_length <= start_fuel + EPS:
        return OptimalPlan((), route_length, 0.0, start_fuel)

    # Work on stations sorted by route mile; remember original positions.
    order = sorted(range(len(options)), key=lambda k: (options[k].route_mile, k))
    opts = [options[k] for k in order]
    n = len(opts)
    mile = [o.route_mile for o in opts]
    off = [o.off_route_miles for o in opts]
    per_mile = [o.price / mpg for o in opts]  # USD per mile of fuel

    def dist(i: int, j: int) -> float:
        return off[i] + (mile[j] - mile[i]) + off[j]

    def dist_from_start(j: int) -> float:
        return mile[j] + off[j]

    def dist_to_end(i: int) -> float:
        return off[i] + (route_length - mile[i])

    states: list[list[_State]] = [[] for _ in range(n)]

    def offer(j: int, fuel: float, cost: float, parent: _State, buy: float) -> None:
        fuel = max(fuel, 0.0)
        for existing in states[j]:
            if abs(existing.fuel - fuel) <= EPS:
                if _better(cost, parent.stops + 1, existing.cost, existing.stops):
                    existing.cost, existing.stops = cost, parent.stops + 1
                    existing.parent, existing.buy = parent, buy
                    existing.fuel = fuel
                return
        states[j].append(_State(j, fuel, cost, parent.stops + 1, parent, buy))

    origin = _State(-1, start_fuel, 0.0, 0, None, 0.0)
    for j in range(n):
        d0 = dist_from_start(j)
        if d0 <= start_fuel + EPS:
            offer(j, start_fuel - d0, 0.0, origin, 0.0)

    best_final: tuple[float, int, _State, float] | None = None

    for i in range(n):
        arrivals = sorted(states[i], key=lambda s: s.fuel)
        if not arrivals:
            continue
        c = per_mile[i]
        # prefix_best[k]: arrival state among arrivals[:k+1] minimising  cost - fuel * c,
        # i.e. the cheapest way to *be at i holding an empty tank* once fuel bought here
        # (at price c) is valued; this is what both transitions need.
        prefix_val: list[float] = []
        prefix_state: list[_State] = []
        for s in arrivals:
            val = s.cost - s.fuel * c
            if prefix_state and not _better(val, s.stops, prefix_val[-1], prefix_state[-1].stops):
                prefix_val.append(prefix_val[-1])
                prefix_state.append(prefix_state[-1])
            else:
                prefix_val.append(val)
                prefix_state.append(s)
        fuels = [s.fuel for s in arrivals]

        # Option 1: fill the tank here, then drive to any station within range.
        fill_state = prefix_state[-1]
        fill_cost = prefix_val[-1] + max_range * c
        fill_buy = max_range - fill_state.fuel

        for j in range(i + 1, n):
            if mile[j] - mile[i] > max_range + EPS:
                break
            d = dist(i, j)
            if d > max_range + EPS:
                continue
            offer(j, max_range - d, fill_cost, fill_state, fill_buy)
            # Option 2: buy just enough to arrive at j with an empty tank.
            k = bisect_right(fuels, d + EPS) - 1
            if k >= 0:
                src = prefix_state[k]
                offer(j, 0.0, prefix_val[k] + d * c, src, max(d - src.fuel, 0.0))

        # Finishing from i: buy just enough to reach the destination.
        d_end = dist_to_end(i)
        if d_end <= max_range + EPS:
            k = bisect_right(fuels, d_end + EPS) - 1
            if k >= 0:
                src = prefix_state[k]
                total = prefix_val[k] + d_end * c
                if best_final is None or _better(total, src.stops, best_final[0], best_final[1]):
                    best_final = (total, src.stops, src, max(d_end - src.fuel, 0.0))

    if best_final is None:
        raise _explain_infeasible(
            mile, off, route_length, max_range, start_fuel, dist, dist_from_start, dist_to_end
        )

    total, _, last_state, last_buy = best_final
    chain: list[tuple[_State, float]] = []
    state, buy = last_state, last_buy
    while state.station != -1:
        chain.append((state, buy))
        buy = state.buy
        state = state.parent
    chain.reverse()

    stops: list[PlannedStop] = []
    previous = -1
    for st, purchase in chain:
        d = dist_from_start(st.station) if previous == -1 else dist(previous, st.station)
        stops.append(PlannedStop(order[st.station], d, st.fuel, purchase))
        previous = st.station
    final_leg = dist_to_end(previous)
    return OptimalPlan(tuple(stops), final_leg, total, start_fuel)


def _explain_infeasible(
    mile, off, route_length, max_range, start_fuel, dist, dist_from_start, dist_to_end
) -> NoFeasiblePlanError:
    """Locate the smallest shortfall that makes the destination unreachable."""
    n = len(mile)
    reachable = [False] * n
    for j in range(n):
        if dist_from_start(j) <= start_fuel + EPS:
            reachable[j] = True
            continue
        reachable[j] = any(reachable[i] and dist(i, j) <= max_range + EPS for i in range(j))

    # (shortfall, from_mile, to_mile, distance, to_destination)
    best: tuple[float, float, float, float, bool] | None = None

    def consider(shortfall, from_mile, to_mile, distance, to_destination):
        nonlocal best
        if best is None or shortfall < best[0]:
            best = (shortfall, from_mile, to_mile, distance, to_destination)

    sources = [(-1, 0.0, start_fuel)] + [(i, mile[i], max_range) for i in range(n) if reachable[i]]
    for i, from_mile, reach in sources:
        if i == -1:
            targets = [(j, dist_from_start(j)) for j in range(n) if not reachable[j]]
            end_d = route_length
        else:
            targets = [(j, dist(i, j)) for j in range(i + 1, n) if not reachable[j]]
            end_d = dist_to_end(i)
        for j, d in targets:
            consider(d - reach, from_mile, mile[j], d, False)
        consider(end_d - reach, from_mile, route_length, end_d, True)

    shortfall, from_mile, to_mile, distance, to_destination = best  # type: ignore[misc]
    target = "the destination" if to_destination else f"the next station (route mile {to_mile:.0f})"
    drivable = distance - shortfall  # what the vehicle can cover from that point
    message = (
        f"No feasible fuel plan: the vehicle cannot get past route mile {from_mile:.0f}. "
        f"The closest next fuel option is {target}, {distance:.0f} miles away, but it can drive "
        f"at most {drivable:.0f} miles from there ({max_range:.0f}-mile range)."
    )
    return NoFeasiblePlanError(
        message,
        {
            "last_reachable_route_mile": round(from_mile, 1),
            "next_option_route_mile": round(to_mile, 1),
            "next_option_is_destination": to_destination,
            "distance_to_next_option_miles": round(distance, 1),
            "shortfall_miles": round(shortfall, 1),
            "max_range_miles": max_range,
            "route_length_miles": round(route_length, 1),
            "candidate_stations": n,
        },
    )
