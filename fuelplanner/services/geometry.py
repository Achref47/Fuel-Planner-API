"""Route geometry helpers (pure numpy, no Django).

Coordinates follow GeoJSON order, ``[lon, lat]``, in decimal degrees. Distances are
in statute miles, using a spherical Earth (great-circle / haversine). That is accurate
to ~0.5 % - far below the uncertainty of city-level station positions.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

EARTH_RADIUS_MILES = 3958.7613


def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles; accepts scalars or numpy arrays."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dlmb = np.radians(lon2) - np.radians(lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def unit_vectors(lat, lon) -> np.ndarray:
    """Points on the unit sphere, shape (n, 3). Euclidean distance there is monotonic in
    great-circle distance, which lets a KD-tree answer nearest-neighbour queries."""
    phi, lmb = np.radians(np.asarray(lat, dtype=float)), np.radians(np.asarray(lon, dtype=float))
    return np.column_stack((np.cos(phi) * np.cos(lmb), np.cos(phi) * np.sin(lmb), np.sin(phi)))


def miles_to_chord(miles: float) -> float:
    """Unit-sphere chord length corresponding to a great-circle distance."""
    return 2.0 * np.sin(miles / (2.0 * EARTH_RADIUS_MILES))


def chord_to_miles(chord):
    return 2.0 * EARTH_RADIUS_MILES * np.arcsin(np.clip(np.asarray(chord) / 2.0, 0.0, 1.0))


@dataclass(frozen=True)
class RoutePath:
    """A route sampled at (roughly) even spacing.

    ``miles[i]`` is the distance travelled along the route to point ``i``; the last
    value equals ``length``.
    """

    lat: np.ndarray
    lon: np.ndarray
    miles: np.ndarray
    length: float

    def __len__(self) -> int:
        return len(self.miles)


def cumulative_miles(coords: np.ndarray) -> np.ndarray:
    """Distance along a ``[lon, lat]`` polyline to each vertex (first value 0)."""
    coords = np.asarray(coords, dtype=float)
    if coords.ndim != 2 or coords.shape[1] != 2:
        raise ValueError("coords must be an (n, 2) array of [lon, lat]")
    if len(coords) < 2:
        raise ValueError("a route needs at least two points")
    legs = haversine_miles(coords[:-1, 1], coords[:-1, 0], coords[1:, 1], coords[1:, 0])
    return np.concatenate(([0.0], np.cumsum(legs)))


def resample_route(
    coords: np.ndarray, step_miles: float = 0.5, total_miles: float | None = None
) -> RoutePath:
    """Resample a route polyline at even distance steps.

    ``total_miles`` (the provider's reported driving distance) rescales the along-route
    mileage so ``RoutePath.length`` agrees with it; the geometry itself is untouched.
    Positions are linearly interpolated between vertices, which is exact enough because
    road geometries have a vertex every few hundred metres.
    """
    if step_miles <= 0:
        raise ValueError("step_miles must be positive")
    coords = np.asarray(coords, dtype=float)
    cum = cumulative_miles(coords)
    geometric_total = float(cum[-1])
    if total_miles is not None and geometric_total > 0:
        cum = cum * (float(total_miles) / geometric_total)
    length = float(cum[-1])

    if length <= 0:  # degenerate: all vertices identical
        return RoutePath(coords[:1, 1], coords[:1, 0], np.zeros(1), 0.0)

    samples = np.arange(0.0, length, step_miles)
    samples = np.append(samples, length)
    lat = np.interp(samples, cum, coords[:, 1])
    lon = np.interp(samples, cum, coords[:, 0])
    return RoutePath(lat, lon, samples, length)


def simplify_route(coords: np.ndarray, tolerance_miles: float) -> np.ndarray:
    """Ramer-Douglas-Peucker simplification of a ``[lon, lat]`` polyline.

    Drops vertices that deviate less than ``tolerance_miles`` from the straight line
    between retained neighbours. Endpoints are always kept. Deviations are measured on a
    local equirectangular projection (tolerance is approximate, accurate to a few %).
    """
    coords = np.asarray(coords, dtype=float)
    n = len(coords)
    if n <= 2 or tolerance_miles <= 0:
        return coords.copy()

    lat0 = np.radians(coords[:, 1].mean())
    miles_per_degree = EARTH_RADIUS_MILES * np.pi / 180.0
    x = (coords[:, 0] - coords[0, 0]) * np.cos(lat0) * miles_per_degree
    y = (coords[:, 1] - coords[0, 1]) * miles_per_degree

    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        dx, dy = x[b] - x[a], y[b] - y[a]
        px, py = x[a + 1 : b] - x[a], y[a + 1 : b] - y[a]
        seg_len = np.hypot(dx, dy)
        dist = np.hypot(px, py) if seg_len == 0 else np.abs(px * dy - py * dx) / seg_len
        i = int(np.argmax(dist))
        if dist[i] > tolerance_miles:
            mid = a + 1 + i
            keep[mid] = True
            stack.append((a, mid))
            stack.append((mid, b))
    return coords[keep]
