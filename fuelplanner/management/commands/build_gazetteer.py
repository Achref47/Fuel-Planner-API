"""Rebuild data/us_places.csv.gz from public-domain U.S. government place files."""

import io
import zipfile
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from fuelplanner.services.gazetteer import build_place_records, read_pipe_table, write_gazetteer

CENSUS_BASE = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/{year}_Gazetteer/"
CENSUS_FILES = {
    "places": "{year}_Gaz_place_national.zip",
    "cousubs": "{year}_Gaz_cousubs_national.zip",
    "zctas": "{year}_Gaz_zcta_national.zip",
}
GNIS_URL = (
    "https://prd-tnm.s3.amazonaws.com/StagedProducts/GeographicNames/Topical/"
    "PopulatedPlaces_National_Text.zip"
)
GNIS_MEMBER = "Text/PopulatedPlaces_National.txt"


class Command(BaseCommand):
    help = (
        "Download the Census Gazetteer (places, county subdivisions, ZCTAs) and USGS GNIS "
        "populated places, and condense them into the gazetteer used to place fuel stations."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--year", default="2026", help="Census Gazetteer vintage (default 2026)"
        )
        parser.add_argument("--raw-dir", default=None, help="Cache directory for downloads")
        parser.add_argument("--output", default=None, help="Output file (default GAZETTEER_PATH)")

    def handle(self, *args, **options):
        cfg = settings.FUEL_PLANNER
        raw_dir = Path(options["raw_dir"] or settings.BASE_DIR / "data" / "raw")
        output = Path(options["output"] or cfg["GAZETTEER_PATH"])
        year = options["year"]
        raw_dir.mkdir(parents=True, exist_ok=True)

        tables = {}
        for name, template in CENSUS_FILES.items():
            filename = template.format(year=year)
            data = self._fetch(CENSUS_BASE.format(year=year) + filename, raw_dir / filename)
            tables[name] = read_pipe_table(self._unzip(data))
            self.stdout.write(f"  {name}: {len(tables[name]):,} rows")

        data = self._fetch(GNIS_URL, raw_dir / "PopulatedPlaces_National_Text.zip")
        gnis = read_pipe_table(self._unzip(data, GNIS_MEMBER))
        self.stdout.write(f"  gnis: {len(gnis):,} rows")

        fips_to_state = {r["GEOID"][:2]: r["USPS"] for r in tables["places"]}
        records = build_place_records(
            tables["places"], tables["cousubs"], gnis, tables["zctas"], fips_to_state
        )
        count = write_gazetteer(records, output)
        size_kb = output.stat().st_size // 1024
        self.stdout.write(self.style.SUCCESS(f"Wrote {count:,} records to {output} ({size_kb} KB)"))

    def _fetch(self, url: str, cache_path: Path) -> bytes:
        if cache_path.exists():
            self.stdout.write(f"Using cached {cache_path.name}")
            return cache_path.read_bytes()
        self.stdout.write(f"Downloading {url}")
        try:
            response = requests.get(
                url, timeout=120, headers={"User-Agent": settings.FUEL_PLANNER["HTTP_USER_AGENT"]}
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise CommandError(f"Could not download {url}: {exc}") from exc
        cache_path.write_bytes(response.content)
        return response.content

    @staticmethod
    def _unzip(data: bytes, member: str | None = None) -> bytes:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            name = member or next(n for n in archive.namelist() if n.lower().endswith(".txt"))
            return archive.read(name)
