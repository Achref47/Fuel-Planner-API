"""Load the SYNTHETIC demo stations (never the private assessment CSV)."""

import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from fuelplanner.importer import read_rows
from fuelplanner.models import Station

DEMO_CSV = Path(settings.BASE_DIR) / "data" / "demo" / "synthetic_stations.csv"
DEMO_ID_START = 900_000  # real OPIS ids are far below this


class Command(BaseCommand):
    help = (
        "Replace the station table with the synthetic demo dataset in data/demo/ (fictional "
        "station names, invented prices). Safe to repeat. Refuses to overwrite real stations "
        "unless --force is given."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--force", action="store_true", help="Also replace non-demo (real) stations"
        )

    def handle(self, *args, **options):
        if not DEMO_CSV.exists():
            raise CommandError(f"Demo dataset not found at {DEMO_CSV}.")
        rows = read_rows(DEMO_CSV)
        bad = [
            r["OPIS Truckstop ID"]
            for r in rows
            if not r["Truckstop Name"].startswith("DEMO ")
            or not r["OPIS Truckstop ID"].isdigit()
            or int(r["OPIS Truckstop ID"]) <= DEMO_ID_START
        ]
        if bad:
            raise CommandError("The demo file contains rows that are not clearly synthetic.")

        if not options["force"] and Station.objects.filter(opis_id__lte=DEMO_ID_START).exists():
            raise CommandError(
                "The station table holds real (non-demo) stations; refusing to replace them. "
                "Use --force only if you really want to."
            )

        report = Path(tempfile.gettempdir()) / "fuelplanner_demo_import_report.json"
        call_command(
            "load_fuel_data",
            csv=str(DEMO_CSV),  # explicit: FUEL_CSV_PATH (the private file) is never consulted
            report=str(report),
            stdout=self.stdout,
        )
        self.stdout.write(self.style.WARNING("Loaded SYNTHETIC demo data: prices are not real."))
