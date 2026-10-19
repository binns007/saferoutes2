"""Shared helpers for the test-suite. The numbers here are TEST FIXTURES, not real statistics."""
from clustering.ml.pipeline import run_pipeline
from core.ingest.census import KERALA_DISTRICT_CENTRES
from core.models import District

# name -> (population, literacy %, crimes/yr total, police strength or None)
FIXTURE_STATS = {
    "Thiruvananthapuram": (3_300_000, 84.4, 3000, 9000),
    "Kollam": (2_600_000, 85.0, 1500, 4000),
    "Pathanamthitta": (1_200_000, 88.7, 500, 2500),
    "Alappuzha": (2_100_000, 87.1, 900, 3000),
    "Kottayam": (1_970_000, 88.6, 1100, 3300),
    "Idukki": (1_100_000, 83.2, 700, 1500),
    "Ernakulam": (3_280_000, 87.0, 2900, 7000),
    "Thrissur": (3_120_000, 85.8, 1700, 4000),
    "Palakkad": (2_800_000, 79.7, 1200, 3200),
    "Malappuram": (4_100_000, 80.5, 2100, 4200),
    "Kozhikode": (3_080_000, 84.7, 2300, 5200),
    "Wayanad": (820_000, 79.0, 600, 1100),
    "Kannur": (2_520_000, 84.8, 1900, 3800),
    "Kasaragod": (1_300_000, 79.4, 800, 1700),
}


def make_districts(with_police=True):
    out = []
    for name, (lat, lon) in KERALA_DISTRICT_CENTRES.items():
        pop, lit, crimes, police = FIXTURE_STATS[name]
        out.append(District.objects.create(
            name=name, latitude=lat, longitude=lon, population=pop, literacy_rate=lit,
            police_strength=police if with_police else None,
            crime_rape=int(crimes * 0.1), crime_kidnapping=int(crimes * 0.05),
            crime_other_ipc_women=crimes - int(crimes * 0.1) - int(crimes * 0.05),
            crime_years="2023-2025",
        ))
    return out


def make_clustered_districts(with_police=True):
    districts = make_districts(with_police)
    run_pipeline(stdout=_Quiet())
    return districts


class _Quiet:
    def write(self, *_a, **_k):
        pass
