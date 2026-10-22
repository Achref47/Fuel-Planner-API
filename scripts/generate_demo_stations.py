"""Generate ``data/demo/synthetic_stations.csv``: a SYNTHETIC fuel-station dataset for the demo.

Everything here is invented or public:

* city locations come from the shipped public-domain place table (``data/us_places.csv.gz``);
* stations are placed in towns that lie on the recorded routes in ``tests/fixtures/osrm``, about
  one every 85 miles, so the demo's example trips are feasible;
* station names are obviously fictional ("DEMO FUEL STOP #n") and prices come from a
  deterministic formula. They are NOT real prices and are unrelated to the private assessment
  data, which this script never reads.

Run from the project root:  ``python scripts/generate_demo_stations.py``
The output is committed, so deployments do not need to run this.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings_test")

import django  # noqa: E402

django.setup()

from scipy.spatial import cKDTree  # noqa: E402

from fuelplanner.importer import REQUIRED_COLUMNS  # noqa: E402
from fuelplanner.services.gazetteer import Gazetteer, iter_gazetteer  # noqa: E402
from fuelplanner.services.geometry import (  # noqa: E402
    chord_to_miles,
    miles_to_chord,
    resample_route,
    unit_vectors,
)

GAZETTEER = ROOT / "data" / "us_places.csv.gz"
ROUTES = ROOT / "tests" / "fixtures" / "osrm"
OUT = ROOT / "data" / "demo" / "synthetic_stations.csv"

SPACING_MILES = 85  # one station per window along each route
WINDOW_MILES = 35  # choose within +/- this of each target mile
MAX_OFF_ROUTE_MILES = 3.0
DEMO_ID_START = 900_001
LOWER_48 = (24.3, 49.7, -125.1, -66.7)
# Invented regional price tilt (USD/gal) so the optimiser has something to optimise.
REGION_TILT = {"CA": 0.55, "WA": 0.35, "OR": 0.30, "NV": 0.40, "AZ": 0.20, "TX": -0.20,
               "OK": -0.20, "LA": -0.15, "MS": -0.15, "AL": -0.10, "NE": -0.05}  # fmt: skip


def synthetic_price(state: str, city: str) -> str:
    digest = hashlib.sha256(f"demo|{state}|{city}".encode()).hexdigest()
    base = 3.05 + (int(digest[:8], 16) / 0xFFFFFFFF) * 0.60
    return f"{base + REGION_TILT.get(state, 0.0):.3f}"


def main() -> None:
    gazetteer = Gazetteer.from_file(GAZETTEER)
    towns = [
        p
        for p in iter_gazetteer(GAZETTEER)
        if p.kind == "place"
        and p.source == "place"
        and LOWER_48[0] <= p.lat <= LOWER_48[1]
        and LOWER_48[2] <= p.lon <= LOWER_48[3]
    ]
    town_xyz = unit_vectors([t.lat for t in towns], [t.lon for t in towns])

    chosen: dict[tuple[str, str], tuple] = {}
    for route_file in sorted(ROUTES.glob("*.json")):
        body = json.loads(route_file.read_text(encoding="utf-8"))["routes"][0]
        coords = np.array(body["geometry"]["coordinates"], dtype=float)
        length = body["distance"] / 1609.344
        path = resample_route(coords, 0.5, total_miles=length)
        tree = cKDTree(unit_vectors(path.lat, path.lon))
        chord, nearest = tree.query(
            town_xyz, distance_upper_bound=miles_to_chord(MAX_OFF_ROUTE_MILES)
        )
        near = np.flatnonzero(np.isfinite(chord))
        miles = path.miles[nearest[near]]
        offs = chord_to_miles(chord[near])
        options = [
            (float(m), float(o), towns[i]) for i, m, o in zip(near, miles, offs, strict=True)
        ]

        target = 0.0
        while target <= length:
            window = [o for o in options if abs(o[0] - target) <= WINDOW_MILES]
            window.sort(key=lambda o: (o[1], o[2].state, o[2].name))
            for _, _, town in window:
                found = gazetteer.find_city(town.name, town.state)
                if found and (found.lat, found.lon) == (town.lat, town.lon):
                    chosen.setdefault((town.state, town.name), (town, route_file.stem))
                    break
            target += SPACING_MILES

    rows = sorted(chosen.values(), key=lambda item: (item[0].state, item[0].name))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(REQUIRED_COLUMNS)
        for number, (town, _) in enumerate(rows, start=DEMO_ID_START):
            writer.writerow(
                [number, f"DEMO FUEL STOP #{number - DEMO_ID_START + 1:03d}", "DEMO HWY, EXIT 0",
                 town.name, town.state, 0, synthetic_price(town.state, town.name)]
            )  # fmt: skip
    states = sorted({t.state for t, _ in rows})
    print(f"wrote {len(rows)} synthetic stations in {len(states)} states to {OUT}")


if __name__ == "__main__":
    main()
