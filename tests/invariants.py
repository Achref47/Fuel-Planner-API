"""Invariants every successful API response must satisfy (shared by several test modules)."""

import pytest

CAP = 500.0


def check_success(data, by_id, max_detour, fraction):
    s, stops, legs = data["summary"], data["fuel_stops"], data["legs"]
    length = data["route"]["distance_miles"]
    initial = data["vehicle"]["initial_fuel_gallons"]

    assert len(legs) == len(stops) + 1 and s["fuel_stops"] == len(stops)
    assert [st["sequence"] for st in stops] == list(range(1, len(stops) + 1))
    miles = [st["route_mile"] for st in stops]
    assert miles == sorted(miles) and all(-0.05 <= m <= length + 0.05 for m in miles)
    assert len({st["station"]["opis_id"] for st in stops}) == len(stops)  # no station twice

    previous_departure = initial
    for stop, leg in zip(stops, legs, strict=False):
        station = by_id[stop["station"]["opis_id"]]
        assert stop["price_per_gallon"] == pytest.approx(float(station.price), abs=0.0006)
        assert stop["station"]["lat"] == pytest.approx(station.latitude, abs=1e-6)
        assert stop["off_route_miles"] <= max_detour + 0.06
        assert stop["detour_miles"] == pytest.approx(2 * stop["off_route_miles"], abs=0.11)
        assert stop["gallons_purchased"] > 0
        assert 0 <= stop["fuel_on_arrival_gallons"] <= 50.01
        assert stop["fuel_on_departure_gallons"] <= 50.01
        assert stop["fuel_on_departure_gallons"] == pytest.approx(
            stop["fuel_on_arrival_gallons"] + stop["gallons_purchased"], abs=0.02
        )
        assert stop["cost"] == pytest.approx(
            stop["gallons_purchased"] * stop["price_per_gallon"], abs=0.06
        )
        assert stop["fuel_on_arrival_gallons"] == pytest.approx(
            previous_departure - leg["fuel_used_gallons"], abs=0.03
        )
        assert stop["distance_from_previous_miles"] == leg["distance_miles"]
        previous_departure = stop["fuel_on_departure_gallons"]

    assert all(0 <= leg["distance_miles"] <= CAP + 0.05 for leg in legs)
    assert legs[0]["distance_miles"] <= fraction * CAP + 0.06
    assert all(
        leg["fuel_used_gallons"] == pytest.approx(leg["distance_miles"] / 10, abs=0.011)
        for leg in legs
    )
    remaining = s["fuel_remaining_at_destination_gallons"]
    assert remaining == pytest.approx(previous_departure - legs[-1]["fuel_used_gallons"], abs=0.03)
    if stops:
        assert remaining == pytest.approx(0, abs=0.03)  # nothing bought that is not needed
    else:
        assert s["total_fuel_cost"] == 0 and remaining >= 0

    assert sum(st["cost"] for st in stops) == pytest.approx(s["total_fuel_cost"], abs=0.005)
    assert sum(st["gallons_purchased"] for st in stops) == pytest.approx(
        s["fuel_purchased_gallons"], abs=0.006 * len(stops) + 0.006
    )
    assert s["driving_distance_miles"] == pytest.approx(
        sum(leg["distance_miles"] for leg in legs), abs=0.06 * len(legs)
    )
    assert s["driving_distance_miles"] >= length - 0.06
    assert s["detour_miles"] == pytest.approx(
        sum(st["detour_miles"] for st in stops), abs=0.15 * max(len(stops), 1)
    )
    balance = (
        initial + s["fuel_purchased_gallons"] - s["fuel_used_gallons"] - remaining
    )  # all display-rounded
    assert balance == pytest.approx(0, abs=0.03)
    if stops:
        assert s["average_price_per_gallon"] == pytest.approx(
            s["total_fuel_cost"] / s["fuel_purchased_gallons"], abs=0.002
        )
        cheapest = min(float(st.price) for st in by_id.values())
        assert s["total_fuel_cost"] >= s["fuel_purchased_gallons"] * cheapest - 0.05
