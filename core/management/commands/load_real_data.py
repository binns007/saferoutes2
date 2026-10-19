"""
Load the CSVs in core/data/ into the database (Stage 1: data ingestion).

    python manage.py load_real_data [--years 3]

Inputs (override paths with the options below):
  kerala_districts.csv            Census 2011 population / literacy + coordinates
  kerala_crime_against_women.csv  Kerala Police unit-by-year crime figures
  benchmark_cities.csv            OPTIONAL reference cities (see below)

Crime figures are summed across a district's police units (City + Rural) and
averaged over the latest N *complete* years that every district has. A unit
missing from a year makes that district-year unusable rather than understated.

Benchmark rows must declare `crime_metric = women_crime_per_100k`, i.e. the
same measure the districts use. Rows measuring anything else are rejected,
because normalising districts against a different metric silently skews
every score.
"""
import csv
import statistics
from collections import defaultdict
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from clustering.models import BenchmarkCity, ClusterEvaluation, DistrictClusterAssignment, ModelRun, SafetyCluster
from core.ingest.kerala_police import UNIT_TO_DISTRICT
from core.models import CrimeRecord, District

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
REQUIRED_DISTRICT_COLS = {"district", "latitude", "longitude", "population", "literacy_rate"}
REQUIRED_CRIME_COLS = {"unit", "district", "year", "partial", "rape", "kidnapping", "other", "total"}
BENCHMARK_METRIC = "women_crime_per_100k"


def _read_csv(path, required, label):
    path = Path(path)
    if not path.exists():
        raise CommandError(f"{label} not found at {path}. Run `python manage.py fetch_real_data` first.")
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise CommandError(f"{path.name} is missing columns: {sorted(missing)}")
        return list(reader)


def _opt_float(value):
    return float(value) if value not in (None, "") else None


def _opt_int(value):
    return int(float(value)) if value not in (None, "") else None


class Command(BaseCommand):
    help = "Load Census + Kerala Police crime data from core/data/*.csv into the database."

    def add_arguments(self, parser):
        parser.add_argument("--districts-csv", default=str(DATA_DIR / "kerala_districts.csv"))
        parser.add_argument("--crime-csv", default=str(DATA_DIR / "kerala_crime_against_women.csv"))
        parser.add_argument("--benchmarks-csv", default=str(DATA_DIR / "benchmark_cities.csv"))
        parser.add_argument("--years", type=int, default=3, help="Average over the latest N complete years (default 3)")

    @transaction.atomic
    def handle(self, *args, **options):
        district_rows = self._load_districts(options["districts_csv"])
        crime_by_district = self._aggregate_crime(options["crime_csv"], set(district_rows))
        years = self._choose_years(crime_by_district, set(district_rows), options["years"])
        self.stdout.write(f"Crime years used: {years[0]}-{years[-1]}" if len(years) > 1 else f"Crime year used: {years[0]}")

        # data changed, so any previous clustering is stale
        DistrictClusterAssignment.objects.all().delete()
        SafetyCluster.objects.all().delete()
        ClusterEvaluation.objects.all().delete()
        ModelRun.objects.all().delete()
        District.objects.exclude(name__in=district_rows).delete()
        CrimeRecord.objects.all().delete()

        for name, row in district_rows.items():
            per_year = crime_by_district[name]
            rape = round(statistics.mean(per_year[y]["rape"] for y in years))
            kidnap = round(statistics.mean(per_year[y]["kidnapping"] for y in years))
            total = round(statistics.mean(per_year[y]["total"] for y in years))
            other = max(total - rape - kidnap, 0)

            district, _ = District.objects.update_or_create(name=name, defaults={
                "state": "Kerala",
                "latitude": float(row["latitude"]),
                "longitude": float(row["longitude"]),
                "population": int(row["population"]),
                "literacy_rate": float(row["literacy_rate"]),
                "police_strength": _opt_int(row.get("police_strength")),
                "per_capita_expenditure": _opt_float(row.get("per_capita_expenditure")),
                "forest_cover_pct": _opt_float(row.get("forest_cover_pct")),
                "population_growth_rate": _opt_float(row.get("population_growth_rate")),
                "crime_rape": rape, "crime_kidnapping": kidnap, "crime_other_ipc_women": other,
                "crime_years": f"{years[0]}-{years[-1]}" if len(years) > 1 else str(years[0]),
                "data_source": "Census 2011; Kerala Police crime-against-women statistics",
                "crime_rate_per_100k": None, "police_per_100k": None, "literacy_disparity": None,
            })
            for year, rec in sorted(per_year.items()):
                CrimeRecord.objects.create(
                    district=district, year=year, rape=rec["rape"], kidnapping=rec["kidnapping"],
                    other=rec["other"], total=rec["total"], partial_year=rec["partial"],
                    units=sorted(rec["units"]), source_urls=sorted(rec["urls"]),
                    retrieved_on=rec["retrieved_on"],
                )

        self._load_benchmarks(options["benchmarks_csv"])
        self.stdout.write(self.style.SUCCESS(f"Loaded {len(district_rows)} districts."))
        self.stdout.write("Next: python manage.py train_clusters")

    # ------------------------------------------------------------------ helpers

    def _load_districts(self, path):
        rows = {}
        for row in _read_csv(path, REQUIRED_DISTRICT_COLS, "Districts CSV"):
            for col in REQUIRED_DISTRICT_COLS:
                if row[col] in (None, ""):
                    raise CommandError(f"District {row['district']!r} has no value for {col}.")
            rows[row["district"].strip()] = row
        return rows

    def _aggregate_crime(self, path, district_names):
        """-> {district: {year: {rape, kidnapping, other, total, partial, units, urls, retrieved_on}}}
        containing only district-years where every expected police unit reported."""
        expected_units = defaultdict(set)
        for unit, district in UNIT_TO_DISTRICT.items():
            expected_units[district].add(unit)

        grouped = defaultdict(lambda: defaultdict(list))
        for row in _read_csv(path, REQUIRED_CRIME_COLS, "Crime CSV"):
            grouped[row["district"]][int(row["year"])].append(row)

        out = {}
        for district in district_names:
            if district not in grouped:
                raise CommandError(f"No crime data for district {district!r} in the crime CSV.")
            years = {}
            for year, rows in grouped[district].items():
                units = {r["unit"] for r in rows}
                if units != expected_units[district]:
                    self.stdout.write(self.style.WARNING(
                        f"  skip {district} {year}: have units {sorted(units)}, expected {sorted(expected_units[district])}"
                    ))
                    continue
                retrieved = max((r.get("retrieved_on") or "" for r in rows), default="")
                years[year] = {
                    "rape": sum(int(r["rape"]) for r in rows),
                    "kidnapping": sum(int(r["kidnapping"]) for r in rows),
                    "other": sum(int(r["other"]) for r in rows),
                    "total": sum(int(r["total"]) for r in rows),
                    "partial": any(int(r["partial"]) for r in rows),
                    "units": units,
                    "urls": {r["source_url"] for r in rows if r.get("source_url")},
                    "retrieved_on": date.fromisoformat(retrieved) if retrieved else None,
                }
            out[district] = years
        return out

    def _choose_years(self, crime_by_district, district_names, n_years):
        complete = None
        for name in district_names:
            ys = {y for y, rec in crime_by_district[name].items() if not rec["partial"]}
            complete = ys if complete is None else complete & ys
        if not complete:
            raise CommandError(
                "No complete (non-partial) year is available for every district. "
                "Check the crime CSV, or re-run `fetch_real_data`."
            )
        chosen = sorted(complete)[-n_years:]
        if len(chosen) < n_years:
            self.stdout.write(self.style.WARNING(f"Only {len(chosen)} complete year(s) available (asked for {n_years})."))
        return chosen

    def _load_benchmarks(self, path):
        BenchmarkCity.objects.all().delete()
        path = Path(path)
        if not path.exists():
            self.stdout.write("No benchmark_cities.csv: districts will be scored relative to each other.")
            return
        count = 0
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("crime_metric") != BENCHMARK_METRIC:
                    raise CommandError(
                        f"Benchmark {row.get('name')!r}: crime_metric must be {BENCHMARK_METRIC!r} "
                        "(crimes against women per 100k population), the same measure the districts use."
                    )
                BenchmarkCity.objects.create(
                    name=row["name"], country=row["country"], year=_opt_int(row.get("year")),
                    crime_rate_per_100k=float(row["crime_rate_per_100k"]),
                    police_per_100k=_opt_float(row.get("police_per_100k")),
                    literacy_rate=_opt_float(row.get("literacy_rate")),
                    source=row.get("source", ""),
                )
                count += 1
        self.stdout.write(f"Loaded {count} benchmark cities.")
