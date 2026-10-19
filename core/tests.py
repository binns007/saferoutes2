"""
Tests for data ingestion (Kerala Police parser + CSV loader).

All numbers below are TEST FIXTURES that exercise the parsing / validation
rules. They are not real crime statistics.
"""
import csv
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from clustering.models import BenchmarkCity
from core.ingest import kerala_police
from core.ingest.census import KERALA_DISTRICT_CENTRES
from core.ingest.kerala_police import ParseError, parse_table
from core.models import CrimeRecord, District


def _table(header, rows):
    th = "".join(f"<th>{h}</th>" for h in header)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<html><body><table><tr>{th}</tr>{body}</table></body></html>"


HEADER = ["Sl.No", "Crime Heads", "2022", "2023", "2025 (Up to October)"]


class ParseTableTests(SimpleTestCase):
    def test_parses_years_heads_and_partial_flag(self):
        html = _table(HEADER, [
            [1, "Rape", 10, 12, 4],
            [2, "Kidnapping & abduction", 5, 6, 1],
            [3, "Molestation", 85, 82, 15],
            ["", "Total cognizable crimes (Crime against women)", 100, 100, 20],
        ])
        years = {y.year: y for y in parse_table(html)}
        self.assertEqual(sorted(years), [2022, 2023, 2025])
        self.assertEqual((years[2022].rape, years[2022].kidnapping, years[2022].other, years[2022].total), (10, 5, 85, 100))
        self.assertTrue(years[2025].partial)
        self.assertFalse(years[2023].partial)
        self.assertTrue(all(y.checksum_ok for y in years.values()))

    def test_attempt_rape_is_not_counted_as_rape(self):
        html = _table(["Sl.No", "Crime Heads", "2023"], [
            [1, "Rape", 10], [2, "Attempt to commit Rape", 3], [3, "Other offences", 7],
            ["", "Total cognizable crimes", 20],
        ])
        (year,) = parse_table(html)
        self.assertEqual(year.rape, 10)
        self.assertEqual(year.other, 10)

    def test_published_total_is_used_and_checksum_mismatch_is_flagged(self):
        # heads sum to 15 but the page publishes 20: use 20, flag it
        html = _table(["Sl.No", "Crime Heads", "2023"], [
            [1, "Kidnapping", 5], [2, "Other offences", 10], ["", "Total cognizable crimes", 20],
        ])
        (year,) = parse_table(html)
        self.assertEqual(year.total, 20)
        self.assertFalse(year.checksum_ok)

    def test_short_row_is_refused_not_guessed(self):
        # a blank cell dropped from the markup would silently shift values into the wrong year
        html = _table(["Sl.No", "Crime Heads", "2022", "2023"], [
            [1, "Kidnapping", 4], ["", "Total cognizable crimes", 9, 8],
        ])
        with self.assertRaisesRegex(ParseError, "refusing to guess"):
            parse_table(html)

    def test_missing_total_row_is_an_error(self):
        html = _table(["Sl.No", "Crime Heads", "2023"], [[1, "Kidnapping", 4]])
        with self.assertRaisesRegex(ParseError, "Total"):
            parse_table(html)

    def test_page_without_table_is_an_error(self):
        with self.assertRaises(ParseError):
            parse_table("<html><body><p>Under maintenance</p></body></html>")

    def test_unpublished_year_is_skipped(self):
        html = _table(["Sl.No", "Crime Heads", "2023", "2024"], [
            [1, "Kidnapping", 4, ""], ["", "Total cognizable crimes", 9, ""],
        ])
        self.assertEqual([y.year for y in parse_table(html)], [2023])

    def test_non_numeric_cell_is_an_error(self):
        html = _table(["Sl.No", "Crime Heads", "2023"], [
            [1, "Kidnapping", "n/a?"], ["", "Total cognizable crimes", 9],
        ])
        with self.assertRaises(ParseError):
            parse_table(html)

    def test_every_police_unit_maps_to_a_known_district(self):
        self.assertEqual(set(kerala_police.UNIT_TO_DISTRICT.values()), set(KERALA_DISTRICT_CENTRES))


# ---------------------------------------------------------------- loader

DISTRICT_FIELDS = ["district", "latitude", "longitude", "population", "literacy_rate",
                   "police_strength", "per_capita_expenditure", "forest_cover_pct",
                   "population_growth_rate", "source"]


class LoadRealDataTests(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.districts_csv = self.tmp / "districts.csv"
        self.crime_csv = self.tmp / "crime.csv"
        self.bench_csv = self.tmp / "bench.csv"  # intentionally absent unless a test creates it

    def _write_districts(self):
        with open(self.districts_csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=DISTRICT_FIELDS)
            w.writeheader()
            for name, (lat, lon) in KERALA_DISTRICT_CENTRES.items():
                w.writerow({"district": name, "latitude": lat, "longitude": lon,
                            "population": 1_000_000, "literacy_rate": 85.0, "source": "fixture"})

    def _write_crime(self, years=((2021, 0), (2022, 0), (2023, 0), (2024, 1)), drop=None, extra_rows=()):
        """`years` = (year, partial). `drop` = {(unit, year)} rows to omit."""
        drop = drop or set()
        fields = ["unit", "district", "year", "partial", "rape", "kidnapping", "other", "total",
                  "checksum_ok", "source_url", "retrieved_on"]
        with open(self.crime_csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            for unit, district in kerala_police.UNIT_TO_DISTRICT.items():
                for year, partial in years:
                    if (unit, year) in drop:
                        continue
                    w.writerow({"unit": unit, "district": district, "year": year, "partial": partial,
                                "rape": 10, "kidnapping": 5, "other": 85, "total": 100, "checksum_ok": 1,
                                "source_url": f"https://{unit}.example/fixture", "retrieved_on": "2026-10-19"})
            for row in extra_rows:
                w.writerow(row)

    def _load(self, **kw):
        out = StringIO()
        call_command("load_real_data", districts_csv=str(self.districts_csv), crime_csv=str(self.crime_csv),
                     benchmarks_csv=str(self.bench_csv), stdout=out, **kw)
        return out.getvalue()

    def test_sums_units_and_averages_complete_years_only(self):
        self._write_districts()
        self._write_crime()
        self._load()
        self.assertEqual(District.objects.count(), 14)
        tvm = District.objects.get(name="Thiruvananthapuram")  # two units: city + rural
        self.assertEqual(tvm.crime_years, "2021-2023")          # partial 2024 excluded
        self.assertEqual(tvm.total_crimes_against_women, 200)   # 100 per unit x 2 units
        pta = District.objects.get(name="Pathanamthitta")       # single unit
        self.assertEqual(pta.total_crimes_against_women, 100)
        self.assertEqual(CrimeRecord.objects.filter(district=tvm, year=2024, partial_year=True).count(), 1)

    def test_district_year_with_a_missing_unit_is_skipped_not_understated(self):
        self._write_districts()
        self._write_crime(drop={("tvmrural", 2023)})
        out = self._load()
        self.assertIn("skip Thiruvananthapuram 2023", out)
        tvm = District.objects.get(name="Thiruvananthapuram")
        self.assertFalse(CrimeRecord.objects.filter(district=tvm, year=2023).exists())
        # the other districts still have 2023, but a year every district has is needed
        self.assertEqual(tvm.crime_years, "2021-2022")

    def test_no_police_or_other_unpublished_indicators_are_invented(self):
        self._write_districts()
        self._write_crime()
        self._load()
        d = District.objects.first()
        self.assertIsNone(d.police_strength)
        self.assertIsNone(d.per_capita_expenditure)
        self.assertIsNone(d.forest_cover_pct)

    def test_missing_crime_file_is_a_clear_error(self):
        self._write_districts()
        with self.assertRaisesRegex(CommandError, "not found"):
            self._load()

    def test_district_with_no_crime_rows_is_an_error(self):
        self._write_districts()
        self._write_crime()
        rows = list(csv.DictReader(open(self.crime_csv)))
        with open(self.crime_csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(r for r in rows if r["district"] != "Idukki")
        with self.assertRaisesRegex(CommandError, "Idukki"):
            self._load()

    def test_only_partial_years_available_is_an_error(self):
        self._write_districts()
        self._write_crime(years=((2025, 1),))
        with self.assertRaisesRegex(CommandError, "complete"):
            self._load()

    def test_benchmarks_must_use_the_same_metric(self):
        self._write_districts()
        self._write_crime()
        with open(self.bench_csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["name", "country", "year", "crime_metric", "crime_rate_per_100k", "source"])
            w.writeheader()
            w.writerow({"name": "Fixtureville", "country": "X", "year": 2023,
                        "crime_metric": "overall_crime_per_100k", "crime_rate_per_100k": 20, "source": "fixture"})
        with self.assertRaisesRegex(CommandError, "women_crime_per_100k"):
            self._load()
        self.assertEqual(BenchmarkCity.objects.count(), 0)

    def test_matching_metric_benchmarks_are_loaded(self):
        self._write_districts()
        self._write_crime()
        with open(self.bench_csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["name", "country", "year", "crime_metric", "crime_rate_per_100k", "source"])
            w.writeheader()
            w.writerow({"name": "Fixtureville", "country": "X", "year": 2023,
                        "crime_metric": "women_crime_per_100k", "crime_rate_per_100k": 20, "source": "fixture"})
        self._load()
        self.assertEqual(BenchmarkCity.objects.get().name, "Fixtureville")

    def test_reload_is_idempotent(self):
        self._write_districts()
        self._write_crime()
        self._load()
        self._load()
        self.assertEqual(District.objects.count(), 14)
        self.assertEqual(CrimeRecord.objects.count(), 14 * 4)  # one row per district per year, no duplicates
        self.assertEqual(CrimeRecord.objects.filter(district__name="Kottayam").count(), 4)
