from django.contrib import admin
from .models import (
    BenchmarkCity, ClusterEvaluation, DistrictClusterAssignment, ModelRun, SafetyCluster,
)


@admin.register(BenchmarkCity)
class BenchmarkCityAdmin(admin.ModelAdmin):
    list_display = ("name", "country", "year", "crime_rate_per_100k", "police_per_100k", "literacy_rate", "source")


class DistrictClusterInline(admin.TabularInline):
    model = DistrictClusterAssignment
    extra = 0
    readonly_fields = ("district", "safety_score", "assigned_at")
    can_delete = False


@admin.register(SafetyCluster)
class SafetyClusterAdmin(admin.ModelAdmin):
    list_display = ("__str__", "algorithm", "avg_safety_score", "district_count", "silhouette_score", "trained_at")
    list_filter = ("algorithm", "tier")
    inlines = [DistrictClusterInline]


@admin.register(DistrictClusterAssignment)
class DistrictClusterAssignmentAdmin(admin.ModelAdmin):
    list_display = ("district", "cluster", "safety_score", "assigned_at")
    list_filter = ("cluster__tier", "cluster__algorithm")


class ClusterEvaluationInline(admin.TabularInline):
    model = ClusterEvaluation
    extra = 0
    can_delete = False
    readonly_fields = ("algorithm", "k", "inertia", "bic", "aic", "silhouette",
                       "calinski_harabasz", "davies_bouldin", "selected")


@admin.register(ModelRun)
class ModelRunAdmin(admin.ModelAdmin):
    list_display = ("__str__", "n_districts", "data_years", "scoring_reference")
    inlines = [ClusterEvaluationInline]
