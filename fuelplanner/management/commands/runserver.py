"""``runserver`` that loads the place table and stations before serving the first request."""

from django.core.management.commands.runserver import Command as RunserverCommand

from fuelplanner.services.warmup import warm_up


class Command(RunserverCommand):
    help = RunserverCommand.help + " Loads the planner's data first so the first request is fast."

    def inner_run(self, *args, **options):
        # inner_run executes in the process that serves requests (the reloader's child).
        loaded = warm_up()
        self.stdout.write(
            f"Warmed up: {loaded['gazetteer_places']:,} places and "
            f"{loaded['stations_loaded']:,} stations in {loaded['warmup_ms']} ms."
        )
        if not loaded["stations_loaded"]:
            self.stdout.write(
                self.style.WARNING(
                    "No fuel stations loaded yet - run `python manage.py load_fuel_data`."
                )
            )
        super().inner_run(*args, **options)
