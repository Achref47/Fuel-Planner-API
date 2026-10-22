"""Simple refuelling strategies a driver might actually use (tests only).

The optimal plan must never cost more than any of these. Each returns the total cost in USD,
or ``None`` if the strategy gets stuck (not every heuristic can finish every trip).
"""

from __future__ import annotations

from fuelplanner.services.optimizer import FuelOption

EPS = 1e-9


def _leg(here, option):
    """Distance from the current spot (``here`` = None for the start, else an option)."""
    if here is None:
        return option.route_mile + option.off_route_miles
    return here.off_route_miles + (option.route_mile - here.route_mile) + option.off_route_miles


def _to_end(here, length):
    return length if here is None else here.off_route_miles + (length - here.route_mile)


def _drive(options, length, cap, mpg, initial, choose, fill_all):
    ordered = sorted(options, key=lambda o: o.route_mile)
    here, fuel, cost, visited = None, initial, 0.0, 0
    while True:
        end = _to_end(here, length)
        if end <= fuel + EPS:
            return cost
        ahead = [
            o
            for o in ordered
            if (here is None or o.route_mile > here.route_mile)
            and _leg(here, o) <= fuel + EPS
            and o is not here
        ]
        if not ahead:
            return None
        nxt = choose(ahead)
        fuel -= _leg(here, nxt)
        here = nxt
        visited += 1
        if visited > 10 * len(ordered) + 10:
            return None
        per_mile = nxt.price / mpg
        remaining = _to_end(here, length)
        # Fill the tank, except at the last stop where only what is still needed is bought.
        amount = cap - fuel if fill_all or remaining > cap else max(remaining - fuel, 0.0)
        cost += amount * per_mile
        fuel += amount


def farthest_then_fill(options, length, cap, mpg, initial):
    """Drive as far as the fuel allows, then fill the tank."""
    return _drive(
        options, length, cap, mpg, initial, lambda a: max(a, key=lambda o: o.route_mile), False
    )


def cheapest_in_range_then_fill(options, length, cap, mpg, initial):
    """Stop at the cheapest station within reach (farthest on ties), then fill the tank."""
    return _drive(
        options,
        length,
        cap,
        mpg,
        initial,
        lambda a: min(a, key=lambda o: (o.price, -o.route_mile)),
        False,
    )


def farthest_buy_just_enough(options, length, cap, mpg, initial):
    """Hop to the farthest reachable station, buying only the fuel needed for the next hop."""
    ordered = sorted(options, key=lambda o: o.route_mile)
    here, fuel, cost, hops = None, initial, 0.0, 0
    while True:
        end = _to_end(here, length)
        if end <= fuel + EPS:
            return cost
        # Nothing can be bought at the start, so the first hop is limited to the fuel on board.
        limit = fuel if here is None else cap
        if here is not None and end <= cap + EPS:
            return cost + (end - fuel) * (here.price / mpg)
        hops_possible = [
            o
            for o in ordered
            if (here is None or o.route_mile > here.route_mile) and _leg(here, o) <= limit + EPS
        ]
        if not hops_possible:
            return None
        target = max(hops_possible, key=lambda o: o.route_mile)
        need = _leg(here, target)
        if need > fuel + EPS:
            cost += (need - fuel) * (here.price / mpg)
            fuel = need
        fuel -= need
        here = target
        hops += 1
        if hops > 10 * len(ordered) + 10:
            return None


STRATEGIES = {
    "farthest_then_fill": farthest_then_fill,
    "cheapest_in_range_then_fill": cheapest_in_range_then_fill,
    "farthest_buy_just_enough": farthest_buy_just_enough,
}


def option_list(stations) -> list[FuelOption]:
    return [FuelOption(s["mile"], s["off"], s["price"]) for s in stations]
