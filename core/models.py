from django.db import models


class District(models.Model):
    """
    One administrative district: reference geography, socio-economic
    indicators, and crime figures aggregated from official records.

    Crime counts are the annual average over the most recent complete years
    available (see `crime_years`), built from the per-year `CrimeRecord` rows
    by `manage.py load_real_data`. Indicators with no published district-level
    source (police strength, per-capita expenditure, forest cover, growth rate)
    are left empty rather than invented; the ML pipeline only uses indicators
    that are actually present for every district.
    """

    name = models.CharField(max_length=120, unique=True)
    state = models.CharField(max_length=120, default="Kerala")
    latitude = models.FloatField()
    longitude = models.FloatField()

    # --- raw ingested fields (Stage 1: Data Ingestion) ---
    population = models.PositiveIntegerField(help_text="District population (Census)")
    literacy_rate = models.FloatField(help_text="Percent, 0-100 (Census)")
    police_strength = models.PositiveIntegerField(
        null=True, blank=True, help_text="Sanctioned police personnel, if a district-level source is loaded"
    )
    per_capita_expenditure = models.FloatField(
        null=True, blank=True, help_text="Annual government spend per capita, INR, if a source is loaded"
    )
    forest_cover_pct = models.FloatField(
        null=True, blank=True, help_text="Percent of district area under forest cover, if a source is loaded"
    )

    crime_rape = models.PositiveIntegerField(default=0, help_text="Rape cases / year (annual average)")
    crime_kidnapping = models.PositiveIntegerField(default=0, help_text="Kidnapping & abduction cases / year (annual average)")
    crime_other_ipc_women = models.PositiveIntegerField(
        default=0, help_text="All other crimes against women, cases / year (annual average)"
    )
    crime_years = models.CharField(
        max_length=40, blank=True, help_text="Years the crime averages cover, e.g. 2023-2025"
    )

    # --- engineered fields (Stage 2: Pre-processing / feature engineering) ---
    crime_rate_per_100k = models.FloatField(null=True, blank=True, help_text="Crimes against women per 100k population / year")
    police_per_100k = models.FloatField(null=True, blank=True)
    literacy_disparity = models.FloatField(
        null=True, blank=True, help_text="Absolute gap vs. the mean literacy of the loaded districts"
    )
    population_growth_rate = models.FloatField(
        null=True, blank=True, help_text="Decadal growth, percent (Census), if loaded"
    )

    data_source = models.CharField(max_length=300, blank=True, default="")
    last_ingested_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name}, {self.state}"

    @property
    def total_crimes_against_women(self):
        return self.crime_rape + self.crime_kidnapping + self.crime_other_ipc_women


class CrimeRecord(models.Model):
    """
    Crimes against women registered in one district in one year, as published
    by the source. A district may be served by several police units (e.g.
    Thiruvananthapuram City + Rural); their figures are summed into one row
    and the contributing units are listed in `units`.
    """

    district = models.ForeignKey(District, on_delete=models.CASCADE, related_name="crime_records")
    year = models.PositiveSmallIntegerField()
    rape = models.PositiveIntegerField(default=0)
    kidnapping = models.PositiveIntegerField(default=0)
    other = models.PositiveIntegerField(default=0, help_text="Total minus rape and kidnapping")
    total = models.PositiveIntegerField(help_text="Published total of cognizable crimes against women")
    partial_year = models.BooleanField(
        default=False, help_text="The source labels this year as incomplete (e.g. 'up to October')"
    )
    units = models.JSONField(default=list, blank=True, help_text="Police units summed into this row")
    source_urls = models.JSONField(default=list, blank=True)
    retrieved_on = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["district__name", "-year"]
        constraints = [
            models.UniqueConstraint(fields=["district", "year"], name="unique_crime_record_per_district_year")
        ]

    def __str__(self):
        return f"{self.district.name} {self.year}: {self.total}"
