from django.db import models


class BenchmarkCity(models.Model):
    """
    Optional reference rows (e.g. globally recognised safe cities) that
    district scores are normalised against (Stage 4: Safety Scoring).

    Rows are only meaningful if `crime_rate_per_100k` is the SAME measure the
    districts use -- crimes against women per 100,000 population. The loader
    (`manage.py load_real_data`) refuses rows that don't declare that metric,
    because mixing, say, overall crime with crimes-against-women silently
    skews every score. With no benchmark rows loaded, districts are scored
    relative to each other and the UI says so.
    """

    name = models.CharField(max_length=120)
    country = models.CharField(max_length=120)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    crime_rate_per_100k = models.FloatField(help_text="Crimes against women per 100k population")
    police_per_100k = models.FloatField(null=True, blank=True)
    literacy_rate = models.FloatField(null=True, blank=True)
    source = models.CharField(max_length=300, blank=True)
    reference_safety_score = models.FloatField(
        null=True, blank=True, help_text="Optional 0-100 published score (informational; not used in scoring)"
    )

    class Meta:
        verbose_name_plural = "Benchmark cities"

    def __str__(self):
        return f"{self.name}, {self.country}"


class ModelRun(models.Model):
    """One execution of the clustering pipeline, with everything needed to audit it."""

    trained_at = models.DateTimeField(auto_now_add=True)
    n_districts = models.PositiveIntegerField()
    data_years = models.CharField(max_length=60, blank=True)
    features = models.JSONField(default=list, help_text="Features fed to the clustering models")
    dropped_features = models.JSONField(default=list, help_text="[{feature, reason}] removed during feature selection")
    pearson = models.JSONField(null=True, blank=True)
    spearman = models.JSONField(null=True, blank=True)
    scoring_weights = models.JSONField(default=dict)
    scoring_reference = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-trained_at"]

    def __str__(self):
        return f"Model run {self.trained_at:%Y-%m-%d %H:%M} ({self.n_districts} districts)"


class ClusterEvaluation(models.Model):
    """Validity metrics for one (algorithm, k) fit in a run -- the data behind the elbow/BIC plots."""

    run = models.ForeignKey(ModelRun, on_delete=models.CASCADE, related_name="evaluations")
    algorithm = models.CharField(max_length=20, choices=[("kmeans", "K-Means"), ("gmm", "Gaussian Mixture")])
    k = models.PositiveSmallIntegerField()
    inertia = models.FloatField(null=True, blank=True, help_text="K-Means within-cluster sum of squares (elbow)")
    bic = models.FloatField(null=True, blank=True)
    aic = models.FloatField(null=True, blank=True)
    silhouette = models.FloatField(null=True, blank=True)
    calinski_harabasz = models.FloatField(null=True, blank=True)
    davies_bouldin = models.FloatField(null=True, blank=True)
    selected = models.BooleanField(default=False)

    class Meta:
        ordering = ["algorithm", "k"]
        constraints = [
            models.UniqueConstraint(fields=["run", "algorithm", "k"], name="unique_evaluation_per_run_algo_k")
        ]

    def __str__(self):
        return f"{self.algorithm} k={self.k}{' *' if self.selected else ''}"


class SafetyCluster(models.Model):
    """One cluster produced by the clustering model, with its safety tier."""

    TIER_CHOICES = [
        ("I", "Tier I - Benchmark Safe"),
        ("II", "Tier II - Moderate"),
        ("III", "Tier III - Elevated Risk"),
        ("IV", "Tier IV - High Risk"),
    ]

    algorithm = models.CharField(max_length=20, choices=[("kmeans", "K-Means"), ("gmm", "Gaussian Mixture")])
    cluster_index = models.IntegerField(help_text="Raw label id output by the model, e.g. 0-3")
    tier = models.CharField(max_length=4, choices=TIER_CHOICES)
    color_hex = models.CharField(max_length=7, default="#35C4A1")
    avg_safety_score = models.FloatField(help_text="0-100, higher = safer")
    district_count = models.PositiveIntegerField(default=0)
    silhouette_score = models.FloatField(
        null=True, blank=True, help_text="Mean silhouette of THIS cluster's members (cohesion vs separation)"
    )
    trained_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-avg_safety_score"]

    def __str__(self):
        return f"[{self.algorithm}] Cluster {self.cluster_index} - {self.get_tier_display()}"


class DistrictClusterAssignment(models.Model):
    """Links a District to the SafetyCluster it was placed in, with its own score."""

    district = models.OneToOneField(
        "core.District", on_delete=models.CASCADE, related_name="cluster_assignment"
    )
    cluster = models.ForeignKey(SafetyCluster, on_delete=models.CASCADE, related_name="districts")
    safety_score = models.FloatField(help_text="0-100, higher = safer")
    assigned_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.district.name} -> {self.cluster}"
