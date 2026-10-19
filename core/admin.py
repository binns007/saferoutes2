from django.contrib import admin
from .models import CrimeRecord, District


class CrimeRecordInline(admin.TabularInline):
    model = CrimeRecord
    extra = 0
    can_delete = False
    readonly_fields = ("year", "rape", "kidnapping", "other", "total", "partial_year", "units", "retrieved_on")


@admin.register(District)
class DistrictAdmin(admin.ModelAdmin):
    list_display = (
        "name", "state", "population", "literacy_rate", "crime_rate_per_100k",
        "crime_years", "police_per_100k", "last_ingested_at",
    )
    list_filter = ("state",)
    search_fields = ("name",)
    readonly_fields = ("last_ingested_at",)
    inlines = [CrimeRecordInline]
    fieldsets = (
        ("Identity", {"fields": ("name", "state", "latitude", "longitude")}),
        ("Raw ingested data (Stage 1)", {
            "fields": (
                "population", "literacy_rate", "police_strength",
                "per_capita_expenditure", "forest_cover_pct",
                "crime_rape", "crime_kidnapping", "crime_other_ipc_women", "crime_years",
            )
        }),
        ("Engineered features (Stage 2)", {
            "fields": (
                "crime_rate_per_100k", "police_per_100k",
                "literacy_disparity", "population_growth_rate",
            )
        }),
        ("Provenance", {"fields": ("data_source", "last_ingested_at")}),
    )


@admin.register(CrimeRecord)
class CrimeRecordAdmin(admin.ModelAdmin):
    list_display = ("district", "year", "total", "rape", "kidnapping", "partial_year", "retrieved_on")
    list_filter = ("year", "partial_year")
