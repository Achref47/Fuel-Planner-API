"""Offline gazetteer: place name -> approximate coordinates.

The fuel dataset has no coordinates, only ``City, State``. Instead of geocoding
thousands of stations through an external API, we join against public-domain
U.S. government place files:

* U.S. Census Bureau Gazetteer "Places" (incorporated places and CDPs)
* U.S. Census Bureau Gazetteer "County Subdivisions" (towns, townships)
* USGS GNIS "Populated Places"
* U.S. Census Bureau Gazetteer "ZCTAs" (ZIP code tabulation areas)

``build_gazetteer`` condenses them into one small compressed CSV
(``data/us_places.csv.gz``) that is committed to the repository, so reviewers
need no network access. Coordinates are the *internal point of the place*, i.e.
city-level, not the exact position of a fuel station.
"""

from __future__ import annotations

import csv
import gzip
import io
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .geometry import chord_to_miles, unit_vectors
from .text import normalize_name, place_key, strip_place_suffix

GAZETTEER_COLUMNS = ["key", "kind", "name", "state", "lat", "lon", "source"]

# Lower rank wins when several records share a lookup key.
SOURCE_RANK = {"place": 0, "cousub": 1, "gnis": 2}

# Alias spellings seen in the fuel data that no official file contains verbatim.
# Keys are "ST|normalised-name" -> "ST|normalised-official-name".
PLACE_ALIASES = {
    "AR|hotspringsnationalpark": "AR|hotsprings",
    "OK|scoffeyville": "OK|southcoffeyville",
}


@dataclass(frozen=True)
class Place:
    key: str
    kind: str  # "place" or "zip"
    name: str
    state: str
    lat: float
    lon: float
    source: str


class GazetteerNotFoundError(FileNotFoundError):
    """Raised when the gazetteer file has not been built / shipped."""


def _open_text(path: Path, mode: str):
    if str(path).endswith(".gz"):
        return gzip.open(path, mode + "t", encoding="utf-8", newline="")
    return open(path, mode, encoding="utf-8", newline="")


def read_pipe_table(data: bytes) -> list[dict[str, str]]:
    """Parse a pipe-delimited Census/USGS text table into stripped dict rows."""
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text), delimiter="|")
    reader.fieldnames = [(f or "").strip() for f in reader.fieldnames or []]
    return [{k: (v or "").strip() for k, v in row.items() if k} for row in reader]


def _rank(record: dict) -> tuple:
    # (exact-name variant before suffix-stripped variant, source, incorporated first,
    #  larger land area first)
    return (
        record["variant"],
        SOURCE_RANK[record["source"]],
        record["pri"],
        -record["land"],
        record["name"],
    )


def build_place_records(
    places: Iterable[dict[str, str]],
    cousubs: Iterable[dict[str, str]],
    gnis: Iterable[dict[str, str]],
    zctas: Iterable[dict[str, str]],
    fips_to_state: dict[str, str] | None = None,
) -> list[Place]:
    """Merge the four source tables into one deduplicated list of ``Place``.

    When several records normalise to the same ``ST|name`` key the highest
    ranked one is kept (incorporated place > county subdivision > GNIS, then
    the largest land area) - a deterministic, documented tie-break.
    """
    best: dict[str, dict] = {}

    def offer(key: str, record: dict) -> None:
        current = best.get(key)
        if current is None or _rank(record) < _rank(current):
            best[key] = record

    def add_census(rows: Iterable[dict[str, str]], source: str) -> None:
        for row in rows:
            state, raw = row["USPS"], row["NAME"]
            try:
                lat, lon = float(row["INTPTLAT"]), float(row["INTPTLONG"])
                land = float(row.get("ALAND") or 0)
            except ValueError:
                continue
            pri = 0 if row.get("FUNCSTAT", "A") == "A" else 1
            stripped = strip_place_suffix(raw)
            variants = {(0, raw), (1, stripped), (1, stripped.split("-")[0])}
            for variant, text in variants:
                key = place_key(state, text)
                offer(
                    key,
                    {
                        "variant": variant,
                        "source": source,
                        "pri": pri,
                        "land": land,
                        "name": stripped,
                        "state": state,
                        "lat": lat,
                        "lon": lon,
                    },
                )

    add_census(places, "place")
    add_census(cousubs, "cousub")

    fips_to_state = fips_to_state or {}
    for row in gnis:
        state = fips_to_state.get(str(row.get("state_numeric", "")).zfill(2))
        if not state:
            continue
        try:
            lat, lon = float(row["prim_lat_dec"]), float(row["prim_long_dec"])
        except (KeyError, ValueError):
            continue
        if lat == 0 and lon == 0:
            continue  # GNIS uses 0,0 for "no coordinates"
        name = row["feature_name"]
        offer(
            place_key(state, name),
            {
                "variant": 1,
                "source": "gnis",
                "pri": 1,
                "land": 0.0,
                "name": name,
                "state": state,
                "lat": lat,
                "lon": lon,
            },
        )

    result = [
        Place(key, "place", rec["name"], rec["state"], rec["lat"], rec["lon"], rec["source"])
        for key, rec in best.items()
    ]
    for row in zctas:
        try:
            lat, lon = float(row["INTPTLAT"]), float(row["INTPTLONG"])
        except (KeyError, ValueError):
            continue
        zip_code = row["GEOID"].zfill(5)
        result.append(Place(zip_code, "zip", zip_code, "", lat, lon, "zcta"))

    result.sort(key=lambda p: (p.kind, p.key))
    return result


def write_gazetteer(places: Iterable[Place], path: Path) -> int:
    """Write the condensed gazetteer (gzip CSV when ``path`` ends in .gz)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with _open_text(path, "w") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(GAZETTEER_COLUMNS)
        for p in places:
            writer.writerow(
                [p.key, p.kind, p.name, p.state, f"{p.lat:.5f}", f"{p.lon:.5f}", p.source]
            )
            count += 1
    return count


def _check_exists(path: Path) -> Path:
    path = Path(path)
    if not path.exists():
        raise GazetteerNotFoundError(
            f"Gazetteer file not found: {path}. It ships with the repository; "
            "rebuild it with `python manage.py build_gazetteer`."
        )
    return path


def _iter_raw_rows(path: Path) -> Iterator[list[str]]:
    with _open_text(_check_exists(path), "r") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header != GAZETTEER_COLUMNS:
            raise GazetteerNotFoundError(
                f"{path} is not a valid gazetteer file (unexpected columns {header}); "
                "rebuild it with `python manage.py build_gazetteer`."
            )
        yield from reader


def _to_place(row) -> Place:
    key, kind, name, state, lat, lon, source = row
    return Place(key, kind, name, state, float(lat), float(lon), source)


def iter_gazetteer(path: Path) -> Iterator[Place]:
    for row in _iter_raw_rows(path):
        yield _to_place(row)


class Gazetteer:
    """In-memory lookup over the condensed gazetteer file.

    Rows loaded from a file are kept as raw strings and converted to ``Place`` objects only
    when looked up, which keeps the one-off load of ~265k rows to a fraction of a second.
    """

    def __init__(self, places: Iterable[Place] = ()):
        self._places: dict[str, Place | list[str]] = {}
        self._zips: dict[str, Place | list[str]] = {}
        for p in places:
            (self._zips if p.kind == "zip" else self._places)[p.key] = p
        self._tree = None

    @classmethod
    def from_file(cls, path: Path) -> Gazetteer:
        gazetteer = cls()
        for row in _iter_raw_rows(path):
            (gazetteer._zips if row[1] == "zip" else gazetteer._places)[row[0]] = row
        return gazetteer

    def __len__(self) -> int:
        return len(self._places) + len(self._zips)

    @staticmethod
    def _get(table: dict, key: str) -> Place | None:
        value = table.get(key)
        if value is None:
            return None
        if not isinstance(value, Place):
            value = table[key] = _to_place(value)
        return value

    def find_city(self, city: str, state: str) -> Place | None:
        """Look a city up by name and 2-letter state, trying known aliases."""
        key = place_key(state, city)
        return self._get(self._places, key) or self._get(self._places, PLACE_ALIASES.get(key, ""))

    def find_zip(self, zip_code: str) -> Place | None:
        return self._get(self._zips, zip_code.strip()[:5].zfill(5))

    def nearest_place_miles(self, lat: float, lon: float) -> float | None:
        """Great-circle distance to the closest named place (None for an empty gazetteer)."""
        if self._tree is None:
            if not self._places:
                return None
            from scipy.spatial import cKDTree

            coords = [
                (v.lat, v.lon) if isinstance(v, Place) else (float(v[4]), float(v[5]))
                for v in self._places.values()
            ]
            lats, lons = zip(*coords, strict=True)
            self._tree = cKDTree(unit_vectors(lats, lons))
        chord, _ = self._tree.query(unit_vectors([lat], [lon])[0])
        return float(chord_to_miles(chord))


_shared: dict[Path, Gazetteer] = {}


def get_gazetteer(path: Path | None = None) -> Gazetteer:
    """The process-wide gazetteer (loaded on first use from ``GAZETTEER_PATH``)."""
    if path is None:
        from django.conf import settings

        path = settings.FUEL_PLANNER["GAZETTEER_PATH"]
    path = Path(path)
    if path not in _shared:
        _shared[path] = Gazetteer.from_file(path)
    return _shared[path]


def reset_gazetteer_cache() -> None:
    _shared.clear()


__all__ = [
    "GAZETTEER_COLUMNS",
    "PLACE_ALIASES",
    "Gazetteer",
    "GazetteerNotFoundError",
    "Place",
    "build_place_records",
    "get_gazetteer",
    "iter_gazetteer",
    "normalize_name",
    "read_pipe_table",
    "reset_gazetteer_cache",
    "write_gazetteer",
]
