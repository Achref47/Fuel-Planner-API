"""Clean the OPIS fuel-price CSV and attach approximate coordinates.

Pure functions (no database access) so the import is reproducible and testable.
Station coordinates are never invented: a station whose city cannot be matched in
the gazetteer is left out of the result and listed in the report.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .services.gazetteer import Gazetteer

REQUIRED_COLUMNS = [
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
]
US_STATE_CODES = frozenset(
    [
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "DC",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
    ]
)
CANADIAN_PROVINCES = frozenset(
    ["AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"]
)
# Sanity bounds for a USD/gallon price; anything outside is rejected, not "fixed".
MIN_PRICE = Decimal("0.50")
MAX_PRICE = Decimal("20")


class DatasetError(Exception):
    """The fuel CSV is missing or not in the expected format."""


@dataclass
class ImportedStation:
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    price: Decimal
    latitude: float
    longitude: float
    location_source: str
    source_rows: int


@dataclass
class ImportReport:
    csv_rows: int = 0
    rejected_rows: Counter = field(default_factory=Counter)
    canadian_rows: int = 0
    exact_duplicate_rows: int = 0
    us_stations: int = 0
    stations_with_price_changes: int = 0
    stations_with_name_variants: int = 0
    matched_stations: int = 0
    matched_by_source: Counter = field(default_factory=Counter)
    unmatched: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        pct = round(100 * self.matched_stations / self.us_stations, 2) if self.us_stations else 0.0
        return {
            "csv_rows": self.csv_rows,
            "rejected_rows": dict(self.rejected_rows),
            "canadian_rows_excluded": self.canadian_rows,
            "exact_duplicate_rows": self.exact_duplicate_rows,
            "us_stations": self.us_stations,
            "stations_with_multiple_prices": self.stations_with_price_changes,
            "stations_with_name_variants": self.stations_with_name_variants,
            "matched_stations": self.matched_stations,
            "matched_percent": pct,
            "matched_by_source": dict(self.matched_by_source),
            "unmatched_stations": self.unmatched,
        }


def _parse_price(raw: str) -> Decimal | None:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    return value if MIN_PRICE <= value <= MAX_PRICE else None


def read_rows(csv_path: Path) -> list[dict[str, str]]:
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise DatasetError(
            f"Fuel price CSV not found at {csv_path}.\n"
            "The assessment dataset is not distributed with this repository. Place the supplied "
            "file at data/fuel-prices-for-be-assessment.csv, or set FUEL_CSV_PATH in .env / pass "
            "--csv <path>."
        )
    with open(csv_path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise DatasetError(
                f"{csv_path} is missing expected column(s): {', '.join(missing)}. "
                f"Expected: {', '.join(REQUIRED_COLUMNS)}."
            )
        return [{k: (v or "").strip() for k, v in row.items() if k} for row in reader]


def clean_and_geocode(
    rows: list[dict[str, str]], gazetteer: Gazetteer
) -> tuple[list[ImportedStation], ImportReport]:
    """Return one ``ImportedStation`` per OPIS id, plus a matching report.

    Rules (documented in the README):
    * Canadian and unknown-state rows are excluded; malformed ids/prices are rejected.
    * Rows sharing an OPIS id describe the same station (identical address/city/state);
      the *lowest* listed price is kept.
    * Coordinates come from the gazetteer city centroid; unmatched stations are dropped.
    """
    report = ImportReport(csv_rows=len(rows))
    groups: dict[int, list[dict]] = {}
    seen_rows: set[tuple] = set()

    for row in rows:
        state = row["State"].upper()
        if state in CANADIAN_PROVINCES:
            report.canadian_rows += 1
            continue
        if state not in US_STATE_CODES:
            report.rejected_rows["unknown_state"] += 1
            continue
        try:
            opis_id = int(row["OPIS Truckstop ID"])
        except ValueError:
            report.rejected_rows["bad_station_id"] += 1
            continue
        price = _parse_price(row["Retail Price"])
        if price is None:
            report.rejected_rows["bad_price"] += 1
            continue
        if not row["City"]:
            report.rejected_rows["missing_city"] += 1
            continue
        fingerprint = tuple(row[c] for c in REQUIRED_COLUMNS)
        if fingerprint in seen_rows:
            report.exact_duplicate_rows += 1
        seen_rows.add(fingerprint)
        groups.setdefault(opis_id, []).append({**row, "_price": price, "_state": state})

    stations: list[ImportedStation] = []
    report.us_stations = len(groups)
    for opis_id, members in sorted(groups.items()):
        first = members[0]
        prices = {m["_price"] for m in members}
        if len(prices) > 1:
            report.stations_with_price_changes += 1
        if len({m["Truckstop Name"] for m in members}) > 1:
            report.stations_with_name_variants += 1
        place = gazetteer.find_city(first["City"], first["_state"])
        if place is None:
            report.unmatched.append(
                {"opis_id": opis_id, "city": first["City"], "state": first["_state"]}
            )
            continue
        report.matched_stations += 1
        report.matched_by_source[place.source] += 1
        stations.append(
            ImportedStation(
                opis_id=opis_id,
                name=first["Truckstop Name"],
                address=first["Address"],
                city=first["City"],
                state=first["_state"],
                price=min(prices),
                latitude=place.lat,
                longitude=place.lon,
                location_source=place.source,
                source_rows=len(members),
            )
        )
    return stations, report
