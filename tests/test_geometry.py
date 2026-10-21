import time

import numpy as np
import pytest

from fuelplanner.services.geometry import (
    EARTH_RADIUS_MILES,
    chord_to_miles,
    cumulative_miles,
    haversine_miles,
    miles_to_chord,
    resample_route,
    simplify_route,
    unit_vectors,
)

MILES_PER_DEGREE = EARTH_RADIUS_MILES * np.pi / 180


def meridian(lat0, lat1, n=11, lon=-100.0):
    lats = np.linspace(lat0, lat1, n)
    return np.column_stack((np.full(n, lon), lats))


def test_haversine_known_distances():
    assert haversine_miles(0, 0, 1, 0) == pytest.approx(MILES_PER_DEGREE, rel=1e-9)
    assert haversine_miles(40.7128, -74.0060, 34.0522, -118.2437) == pytest.approx(2445, abs=5)
    assert haversine_miles(10, 20, 10, 20) == 0


def test_haversine_is_vectorised():
    d = haversine_miles(np.array([0.0, 0.0]), np.array([0.0, 0.0]), np.array([1.0, 2.0]), 0.0)
    assert d == pytest.approx([MILES_PER_DEGREE, 2 * MILES_PER_DEGREE])


def test_chord_conversion_roundtrip():
    for miles in (0.5, 5, 25, 500):
        assert chord_to_miles(miles_to_chord(miles)) == pytest.approx(miles)


def test_unit_vectors_are_unit_length():
    v = unit_vectors([0, 45, -80], [0, -100, 170])
    assert np.linalg.norm(v, axis=1) == pytest.approx(1.0)


def test_cumulative_miles_along_a_meridian():
    cum = cumulative_miles(meridian(30, 40))
    assert cum[0] == 0
    assert cum[-1] == pytest.approx(10 * MILES_PER_DEGREE, rel=1e-9)
    assert np.all(np.diff(cum) > 0)


@pytest.mark.parametrize("bad", [np.zeros((1, 2)), np.zeros((3, 3)), np.zeros(4)])
def test_cumulative_miles_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        cumulative_miles(bad)


def test_resample_even_steps_and_exact_endpoints():
    coords = meridian(30, 40)  # ~690.9 miles
    path = resample_route(coords, step_miles=0.5)
    assert path.miles[0] == 0
    assert path.miles[-1] == pytest.approx(path.length)
    assert path.length == pytest.approx(10 * MILES_PER_DEGREE, rel=1e-9)
    steps = np.diff(path.miles)
    assert steps.max() <= 0.5 + 1e-9
    assert (path.lat[0], path.lon[0]) == (30.0, -100.0)
    assert path.lat[-1] == pytest.approx(40.0)
    assert np.all(np.diff(path.lat) > 0)  # monotone for a monotone route


def test_resample_rescales_mileage_to_provider_distance():
    coords = meridian(30, 40)
    path = resample_route(coords, step_miles=1.0, total_miles=700.0)
    assert path.length == 700.0
    assert path.miles[-1] == 700.0
    # Geometry unchanged: the route still ends at 40 degrees north.
    assert path.lat[-1] == pytest.approx(40.0)


def test_resample_handles_repeated_vertices_and_degenerate_routes():
    coords = np.array([[-100.0, 30.0], [-100.0, 30.0], [-100.0, 31.0]])
    path = resample_route(coords, step_miles=10)
    assert path.length == pytest.approx(MILES_PER_DEGREE)
    same = resample_route(np.array([[-100.0, 30.0], [-100.0, 30.0]]), step_miles=1)
    assert same.length == 0 and len(same) == 1


def test_resample_rejects_non_positive_step():
    with pytest.raises(ValueError):
        resample_route(meridian(30, 31), step_miles=0)


def test_simplify_collapses_collinear_points():
    out = simplify_route(meridian(30, 40, n=500), tolerance_miles=0.05)
    assert len(out) == 2
    assert out[0].tolist() == [-100.0, 30.0] and out[-1].tolist() == [-100.0, 40.0]


def test_simplify_keeps_corners_and_respects_tolerance():
    # An "L": north 10 degrees then east 10 degrees, densely sampled.
    north = meridian(30, 40, n=200)
    east = np.column_stack((np.linspace(-100, -90, 200), np.full(200, 40.0)))
    coords = np.vstack((north, east[1:]))
    out = simplify_route(coords, tolerance_miles=0.5)
    assert len(out) == 3
    assert [-100.0, 40.0] in out.tolist()

    # Generic property: every dropped vertex lies within tolerance of the simplified line.
    rng = np.random.default_rng(1)
    lon = np.linspace(-100, -90, 400)
    lat = 40 + np.cumsum(rng.normal(0, 0.003, 400))
    wiggly = np.column_stack((lon, lat))
    tol = 0.3
    simple = simplify_route(wiggly, tol)
    assert 2 <= len(simple) < len(wiggly)
    lat0 = np.radians(lat.mean())

    def xy(pts):
        return np.column_stack(
            (
                (pts[:, 0] - wiggly[0, 0]) * np.cos(lat0) * MILES_PER_DEGREE,
                (pts[:, 1] - wiggly[0, 1]) * MILES_PER_DEGREE,
            )
        )

    full_xy, simple_xy = xy(wiggly), xy(simple)
    worst = 0.0
    for p in full_xy:
        best = np.inf
        for a, b in zip(simple_xy[:-1], simple_xy[1:], strict=True):
            ab = b - a
            t = np.clip(np.dot(p - a, ab) / np.dot(ab, ab), 0, 1)
            best = min(best, np.linalg.norm(p - (a + t * ab)))
        worst = max(worst, best)
    assert worst <= tol + 1e-6


def test_simplify_passthrough_cases():
    two = meridian(30, 31, n=2)
    assert simplify_route(two, 1.0).tolist() == two.tolist()
    many = meridian(30, 40, n=50)
    assert len(simplify_route(many, 0)) == 50


def test_large_route_processing_is_fast():
    # ~2,800-mile route with ~35k vertices, like a coast-to-coast OSRM response.
    n = 35_000
    lon = np.linspace(-118.2, -74.0, n)
    lat = 37 + 3 * np.sin(np.linspace(0, 6, n)) + np.random.default_rng(0).normal(0, 1e-4, n)
    coords = np.column_stack((lon, lat))
    start = time.perf_counter()
    path = resample_route(coords, 0.5)
    simplified = simplify_route(coords, 0.1)
    elapsed = time.perf_counter() - start
    assert len(path) > 5000
    assert 2 < len(simplified) < n
    assert elapsed < 2.0
