"""
Census 2011 district indicators for Kerala.

Source: Census of India 2011 Primary Census Abstract, district level, as
redistributed in the open GitHub dataset below (a CSV of the official
tables). Population matches the published Census totals.

Literacy caveat: this file has total literates and total population but not
the 0-6 age population, so the *official* literacy rate (literates / population
aged 7+) can't be reproduced. `literacy_rate` here is literates as a share of
the TOTAL population ("crude" literacy), which runs several points below the
official rate for every district but keeps the districts' relative order well
enough for scoring. It is labelled as such in the CSV's `source` column.
"""
import csv
import io
from datetime import date

import requests

CENSUS_URL = (
    "https://raw.githubusercontent.com/nishusharma1608/India-Census-2011-Analysis/"
    "master/india-districts-census-2011.csv"
)

# Approximate district-headquarters coordinates (reference geography, not statistics).
KERALA_DISTRICT_CENTRES = {
    "Thiruvananthapuram": (8.5241, 76.9366),
    "Kollam": (8.8932, 76.6141),
    "Pathanamthitta": (9.2648, 76.7870),
    "Alappuzha": (9.4981, 76.3388),
    "Kottayam": (9.5916, 76.5222),
    "Idukki": (9.9189, 77.1025),
    "Ernakulam": (9.9312, 76.2673),
    "Thrissur": (10.5276, 76.2144),
    "Palakkad": (10.7867, 76.6548),
    "Malappuram": (11.0510, 76.0711),
    "Kozhikode": (11.2588, 75.7804),
    "Wayanad": (11.6854, 76.1320),
    "Kannur": (11.8745, 75.3704),
    "Kasaragod": (12.4996, 74.9869),
}

DISTRICT_FIELDS = [
    "district", "latitude", "longitude", "population", "literacy_rate",
    "police_strength", "per_capita_expenditure", "forest_cover_pct", "population_growth_rate",
    "source",
]


def parse_kerala_rows(csv_text):
    """Return {district: {"population": int, "literate": int}} for Kerala from the census CSV."""
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = {}
    for row in reader:
        if (row.get("State name") or "").strip().upper() != "KERALA":
            continue
        name = row["District name"].strip()
        rows[name] = {"population": int(row["Population"]), "literate": int(row["Literate"])}
    missing = set(KERALA_DISTRICT_CENTRES) - set(rows)
    if missing:
        raise ValueError(f"Census file is missing Kerala districts: {sorted(missing)}")
    return rows


def build_district_rows(census):
    rows = []
    for name, (lat, lon) in KERALA_DISTRICT_CENTRES.items():
        c = census[name]
        rows.append({
            "district": name,
            "latitude": lat,
            "longitude": lon,
            "population": c["population"],
            "literacy_rate": round(c["literate"] / c["population"] * 100, 2),
            "police_strength": "",
            "per_capita_expenditure": "",
            "forest_cover_pct": "",
            "population_growth_rate": "",
            "source": (
                f"Census of India 2011 (PCA district tables, via {CENSUS_URL}); "
                "literacy_rate = literates / total population (crude, not the official 7+ rate); "
                f"coordinates approximate district HQ; retrieved {date.today().isoformat()}"
            ),
        })
    return rows


def fetch_districts(timeout=60):
    response = requests.get(CENSUS_URL, timeout=timeout)
    response.raise_for_status()
    return build_district_rows(parse_kerala_rows(response.text))


def write_districts_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=DISTRICT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
