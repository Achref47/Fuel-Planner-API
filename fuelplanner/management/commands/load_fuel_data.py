"""Import the fuel-price CSV into the database with approximate coordinates."""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fuelplanner.importer import DatasetError, clean_and_geocode, read_rows
from fuelplanner.models import Station
from fuelplanner.services.gazetteer import Gazetteer, GazetteerNotFoundError
from fuelplanner.services.stations import reset_station_index


class Command(BaseCommand):
    help = (
        "Clean the OPIS fuel-price CSV, locate each station via the offline gazetteer, "
        "and load the stations into the database (replacing existing rows)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--csv", default=None, help="Fuel CSV path (default FUEL_CSV_PATH)")
        parser.add_argument(
            "--gazetteer", default=None, help="Gazetteer path (default GAZETTEER_PATH)"
        )
        parser.add_argument(
            "--report",
            default="data/import_report.json",
            help="Where to write the JSON matching report (default data/import_report.json)",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Analyse only; do not write to the database"
        )

    def handle(self, *args, **options):
        cfg = settings.FUEL_PLANNER
        csv_path = Path(options["csv"] or cfg["FUEL_CSV_PATH"])
        gazetteer_path = Path(options["gazetteer"] or cfg["GAZETTEER_PATH"])
        try:
            rows = read_rows(csv_path)
            gazetteer = Gazetteer.from_file(gazetteer_path)
        except (DatasetError, GazetteerNotFoundError) as exc:
            raise CommandError(str(exc)) from exc

        stations, report = clean_and_geocode(rows, gazetteer)
        summary = report.as_dict()

        report_path = Path(options["report"])
        if not report_path.is_absolute():
            report_path = settings.BASE_DIR / report_path
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

        self._print_summary(summary, report_path)
        if not stations:
            raise CommandError("No stations could be located; nothing was imported.")
        if options["dry_run"]:
            self.stdout.write("Dry run: database not modified.")
            return

        with transaction.atomic():
            Station.objects.all().delete()
            Station.objects.bulk_create(
                [
                    Station(
                        opis_id=s.opis_id,
                        name=s.name,
                        address=s.address,
                        city=s.city,
                        state=s.state,
                        price=s.price,
                        latitude=s.latitude,
                        longitude=s.longitude,
                        location_precision=Station.PRECISION_CITY,
                        location_source=s.location_source,
                        source_rows=s.source_rows,
                    )
                    for s in stations
                ],
                batch_size=500,
            )
        reset_station_index()
        self.stdout.write(self.style.SUCCESS(f"Imported {len(stations):,} stations."))

    def _print_summary(self, summary: dict, report_path: Path) -> None:
        out = self.stdout.write
        out(f"CSV rows read:               {summary['csv_rows']:,}")
        out(f"Canadian rows excluded:      {summary['canadian_rows_excluded']:,}")
        out(f"Rejected rows:               {summary['rejected_rows'] or 0}")
        out(f"Exact duplicate rows:        {summary['exact_duplicate_rows']:,}")
        out(f"US stations (unique ids):    {summary['us_stations']:,}")
        out(
            f"  with differing prices:     {summary['stations_with_multiple_prices']:,}"
            " (lowest kept)"
        )
        out(
            f"Located via gazetteer:       {summary['matched_stations']:,}"
            f" ({summary['matched_percent']}%)"
        )
        out(f"  by source:                 {summary['matched_by_source']}")
        out(f"Not located (excluded):      {len(summary['unmatched_stations']):,}")
        out(f"Report written to {report_path}")
