"""
Kerala Police "Crime against women" statistics.

Every police unit (the 14 districts are served by 20 units: six districts
have a separate City and Rural unit) publishes a year-by-year table at
    https://<unit>.keralapolice.gov.in/crime-statistics/crime-against-woman
with one row per crime head and a final "Total cognizable crimes" row.

`parse_table()` turns that page into per-year figures and validates it:
  * year columns come from the header row (a header such as
    "2025 (Up to October)" marks that year as partial);
  * every row must have exactly as many cells as the header, so a blank cell
    can never silently shift a value into the wrong year;
  * the published total is what the pipeline uses; the crime heads are summed
    per year and compared with it (`checksum_ok`) as a parse-sanity check.

The crime-head names change over time (IPC -> BNS, new POCSO rows), so heads
are classified by pattern: anything mentioning rape (except "attempt") counts
as rape, anything mentioning kidnap/abduct as kidnapping, everything else as
other. `other` is derived as total - rape - kidnapping so it always reconciles.
"""
import csv
import re
import time
from dataclasses import dataclass
from datetime import date

import requests
from bs4 import BeautifulSoup

BASE = "https://{unit}.keralapolice.gov.in/crime-statistics/crime-against-woman"

# unit subdomain -> district it belongs to
UNIT_TO_DISTRICT = {
    "tvmcity": "Thiruvananthapuram",
    "tvmrural": "Thiruvananthapuram",
    "kollamcity": "Kollam",
    "kollamrural": "Kollam",
    "pathanamthitta": "Pathanamthitta",
    "alappuzha": "Alappuzha",
    "kottayam": "Kottayam",
    "idukki": "Idukki",
    "kochicity": "Ernakulam",
    "ernakulamrural": "Ernakulam",
    "thrissurcity": "Thrissur",
    "thrissurrural": "Thrissur",
    "palakkad": "Palakkad",
    "malappuram": "Malappuram",
    "kozhikodecity": "Kozhikode",
    "kozhikoderural": "Kozhikode",
    "wayanad": "Wayanad",
    "kannurcity": "Kannur",
    "kannurrural": "Kannur",
    "kasaragod": "Kasaragod",
}

CRIME_FIELDS = [
    "unit", "district", "year", "partial", "rape", "kidnapping", "other", "total",
    "checksum_ok", "source_url", "retrieved_on",
]

_RAPE = re.compile(r"\brape\b", re.I)
_ATTEMPT = re.compile(r"attempt", re.I)
_KIDNAP = re.compile(r"kidnap|abduct", re.I)
_TOTAL = re.compile(r"^\s*total\b", re.I)
_YEAR = re.compile(r"^\s*(20\d{2})\b")


class ParseError(ValueError):
    pass


@dataclass
class YearFigures:
    year: int
    partial: bool
    rape: int
    kidnapping: int
    other: int
    total: int
    checksum_ok: bool


def _to_int(text):
    text = (text or "").strip().replace(",", "")
    if text in {"", "-", "–", "—", "NA", "N/A"}:
        return 0
    if not re.fullmatch(r"\d+", text):
        raise ParseError(f"non-numeric cell: {text!r}")
    return int(text)


def _find_table(soup):
    for table in soup.find_all("table"):
        header_text = " ".join(th.get_text(" ", strip=True) for th in table.find_all("th")[:12])
        first_row = table.find("tr")
        row_text = first_row.get_text(" ", strip=True) if first_row else ""
        if re.search(r"crime\s*heads?", header_text + " " + row_text, re.I):
            return table
    raise ParseError("no 'Crime Heads' table found on the page")


def _extract_rows(soup):
    """Rows as lists of cell text. Supports the site's div grid (div.mytable1) and plain <table>s."""
    rows = []
    grid = soup.find("div", class_="mytable1")
    if grid is not None:
        for row in grid.find_all("div", recursive=False):
            cells = [c.get_text(" ", strip=True) for c in row.find_all("div", recursive=False)]
            if cells:
                rows.append(cells)
    else:
        table = _find_table(soup)
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)
    if not rows or not re.search(r"crime\s*heads?", " ".join(rows[0]), re.I):
        raise ParseError("no 'Crime Heads' table found on the page")
    return rows


def parse_table(html, current_year=None):
    """Parse one unit's page -> list[YearFigures] (years with a published total)."""
    current_year = current_year or date.today().year
    soup = BeautifulSoup(html, "lxml")
    rows = _extract_rows(soup)

    header = rows[0]
    year_cols = []  # (column index, year, partial)
    for idx, text in enumerate(header):
        m = _YEAR.match(text)
        if m:
            year = int(m.group(1))
            labelled = bool(re.search(r"up\s*to|till|upto", text, re.I))
            # the current year is incomplete even if the header doesn't say so
            year_cols.append((idx, year, labelled or year >= current_year))
    if not year_cols:
        raise ParseError(f"no year columns in header {header}")
    head_col = next((i for i, t in enumerate(header) if re.search(r"crime\s*heads?", t, re.I)), 1)

    heads, total_row = [], None
    for cells in rows[1:]:
        # the Total row sometimes drops the empty Sl.No cell; that is the only gap we repair
        if len(cells) == len(header) - 1 and _TOTAL.match(cells[0]):
            cells = [""] + cells
        if len(cells) != len(header):
            raise ParseError(
                f"row {cells[:3]}... has {len(cells)} cells but the header has {len(header)}; "
                "refusing to guess which year each value belongs to"
            )
        name = cells[head_col]
        values = {year: _to_int(cells[idx]) for idx, year, _p in year_cols}
        if _TOTAL.match(name):
            total_row = values
        else:
            heads.append((name, values))
    if total_row is None:
        raise ParseError("no 'Total cognizable crimes' row found")

    out = []
    for _idx, year, partial in year_cols:
        total = total_row[year]
        head_sum = sum(v[year] for _n, v in heads)
        if total == 0:
            continue  # year not published (or published without a total, which we can't use)
        rape = sum(v[year] for n, v in heads if _RAPE.search(n) and not _ATTEMPT.search(n))
        kidnap = sum(v[year] for n, v in heads if _KIDNAP.search(n))
        out.append(YearFigures(
            year=year, partial=partial, rape=rape, kidnapping=kidnap,
            other=max(total - rape - kidnap, 0), total=total, checksum_ok=(head_sum == total),
        ))
    if not out:
        raise ParseError("table has no year with published figures")
    return out


def fetch_unit(unit, session=None, timeout=30):
    session = session or requests.Session()
    url = BASE.format(unit=unit)
    response = session.get(url, timeout=timeout, headers={"User-Agent": "SafeRoutes-student-project/1.0"})
    response.raise_for_status()
    return url, parse_table(response.text)


def fetch_all(units=None, delay_seconds=1.0, log=print):
    """Fetch every unit. Returns (rows, failures) where rows are CSV-ready dicts."""
    session = requests.Session()
    rows, failures = [], {}
    today = date.today().isoformat()
    for unit in units or UNIT_TO_DISTRICT:
        try:
            url, years = fetch_unit(unit, session)
        except (requests.RequestException, ParseError) as exc:
            failures[unit] = str(exc)
            log(f"  FAILED {unit}: {exc}")
            continue
        bad = [y.year for y in years if not y.checksum_ok]
        log(f"  ok     {unit}: years {[y.year for y in years]}"
            + (f"  (head rows don't sum to the total for {bad})" if bad else ""))
        for y in years:
            rows.append({
                "unit": unit, "district": UNIT_TO_DISTRICT[unit], "year": y.year,
                "partial": int(y.partial), "rape": y.rape, "kidnapping": y.kidnapping,
                "other": y.other, "total": y.total, "checksum_ok": int(y.checksum_ok),
                "source_url": url, "retrieved_on": today,
            })
        time.sleep(delay_seconds)
    return rows, failures


def write_crime_csv(path, rows):
    rows = sorted(rows, key=lambda r: (r["district"], r["unit"], r["year"]))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CRIME_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
