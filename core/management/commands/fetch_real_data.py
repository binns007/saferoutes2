"""
Refresh the bundled CSVs in core/data/ from their public sources.

    python manage.py fetch_real_data              # census + crime
    python manage.py fetch_real_data --only crime
    python manage.py fetch_real_data --only census

This only writes CSV files (so you can review / commit them); load them into
the database with `python manage.py load_real_data`.
"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.ingest import census, kerala_police

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


class Command(BaseCommand):
    help = "Download Census 2011 district data and Kerala Police crime-against-women statistics into core/data/*.csv"

    def add_arguments(self, parser):
        parser.add_argument("--only", choices=["census", "crime"], help="Fetch just one source")
        parser.add_argument(
            "--allow-partial", action="store_true",
            help="Write the crime CSV even if some police units failed to download/parse",
        )

    def handle(self, *args, **options):
        only = options["only"]
        DATA_DIR.mkdir(exist_ok=True)

        if only in (None, "census"):
            self.stdout.write("Census 2011 (Kerala districts)...")
            rows = census.fetch_districts()
            census.write_districts_csv(DATA_DIR / "kerala_districts.csv", rows)
            self.stdout.write(self.style.SUCCESS(f"  wrote {len(rows)} districts"))

        if only in (None, "crime"):
            self.stdout.write("Kerala Police crime against women (20 units)...")
            rows, failures = kerala_police.fetch_all(log=self.stdout.write)
            if failures and not options["allow_partial"]:
                raise CommandError(
                    f"{len(failures)} unit(s) failed: {', '.join(failures)}. Nothing written. "
                    "Re-run, or pass --allow-partial to keep the units that worked "
                    "(districts missing a unit are skipped on load)."
                )
            kerala_police.write_crime_csv(DATA_DIR / "kerala_crime_against_women.csv", rows)
            self.stdout.write(self.style.SUCCESS(f"  wrote {len(rows)} unit-year rows"))

        self.stdout.write("Next: python manage.py load_real_data && python manage.py train_clusters")
