"""
Pipeline tests. District numbers come from core.testing FIXTURE_STATS: they
exercise the maths and are not real statistics.
"""
from django.test import TestCase

from clustering.ml.pipeline import compute_safety_scores, load_district_frame, run_pipeline, select_features
from clustering.models import (
    BenchmarkCity, ClusterEvaluation, DistrictClusterAssignment, ModelRun, SafetyCluster,
)
from core.models import District
from core.testing import _Quiet, make_clustered_districts, make_districts


class PipelineTests(TestCase):
    def test_no_districts_is_a_clear_error(self):
        with self.assertRaisesRegex(ValueError, "load_real_data"):
            run_pipeline(stdout=_Quiet())

    def test_missing_police_data_is_dropped_not_invented(self):
        make_clustered_districts(with_police=False)
        run = ModelRun.objects.get()
        self.assertNotIn("police_per_100k", run.features)
        self.assertIn("police_per_100k", [d["feature"] for d in run.dropped_features])
        self.assertNotIn("police_per_100k", run.scoring_weights)
        self.assertAlmostEqual(sum(run.scoring_weights.values()), 1.0, places=2)  # weights renormalised
        self.assertIsNone(District.objects.first().police_per_100k)

    def test_police_data_is_used_when_present(self):
        make_clustered_districts(with_police=True)
        run = ModelRun.objects.get()
        self.assertIn("police_per_100k", run.features)
        self.assertEqual(set(run.scoring_weights), {"crime_rate_per_100k", "police_per_100k", "literacy_rate"})

    def test_k_sweep_is_stored_for_both_algorithms_and_one_is_selected_each(self):
        make_clustered_districts()
        for algo in ("kmeans", "gmm"):
            ks = list(ClusterEvaluation.objects.filter(algorithm=algo).values_list("k", flat=True))
            self.assertEqual(sorted(ks), [2, 3, 4, 5, 6])
            selected = ClusterEvaluation.objects.filter(algorithm=algo, selected=True)
            self.assertEqual(selected.count(), 1)
            self.assertIn(selected.get().k, (3, 4))
        self.assertIsNotNone(ClusterEvaluation.objects.filter(algorithm="kmeans").first().inertia)  # elbow data
        self.assertIsNotNone(ClusterEvaluation.objects.filter(algorithm="gmm").first().bic)

    def test_every_district_gets_one_assignment_with_a_bounded_score(self):
        make_clustered_districts()
        self.assertEqual(DistrictClusterAssignment.objects.count(), 14)
        for a in DistrictClusterAssignment.objects.all():
            self.assertTrue(0 <= a.safety_score <= 100)

    def test_tiers_are_ordered_by_mean_safety_score(self):
        make_clustered_districts()
        clusters = list(SafetyCluster.objects.filter(algorithm="kmeans").order_by("-avg_safety_score"))
        order = ["I", "II", "III", "IV"]
        tiers = [c.tier for c in clusters]
        self.assertEqual(tiers, sorted(tiers, key=order.index))
        self.assertEqual(tiers[0], "I")
        self.assertEqual(tiers[-1], "IV")

    def test_cluster_silhouette_is_per_cluster_not_one_global_number(self):
        make_clustered_districts()
        values = {c.silhouette_score for c in SafetyCluster.objects.filter(algorithm="kmeans")}
        self.assertGreater(len(values), 1)

    def test_correlation_matrices_are_stored(self):
        make_clustered_districts()
        run = ModelRun.objects.get()
        self.assertEqual(set(run.pearson), set(run.features))
        self.assertEqual(set(run.spearman), set(run.features))

    def test_higher_crime_rate_means_lower_safety_score(self):
        make_districts()
        df, _w, _ref = compute_safety_scores(load_district_frame())
        by_rate = df.sort_values("crime_rate_per_100k")
        self.assertGreater(by_rate["safety_score"].iloc[0], by_rate["safety_score"].iloc[-1])

    def test_benchmark_rows_change_the_reference_and_are_reported(self):
        make_districts()
        _df, _w, reference = compute_safety_scores(load_district_frame())
        self.assertIn("no benchmark", reference)
        BenchmarkCity.objects.create(name="Fixtureville", country="X", crime_rate_per_100k=1.0,
                                     police_per_100k=300.0, literacy_rate=99.0)
        _df, _w, reference = compute_safety_scores(load_district_frame())
        self.assertIn("1 benchmark", reference)

    def test_correlated_optional_feature_is_dropped(self):
        make_districts()
        for i, d in enumerate(District.objects.all()):
            d.per_capita_expenditure = 1000.0 + i * 10          # varies
            d.forest_cover_pct = 5.0 + i * 0.5                  # perfectly correlated with the above
            d.save()
        kept, dropped = select_features(load_district_frame())
        self.assertIn("per_capita_expenditure", kept)
        self.assertNotIn("forest_cover_pct", kept)
        self.assertIn("redundant", next(d for d in dropped if d["feature"] == "forest_cover_pct")["reason"])

    def test_retraining_replaces_rather_than_duplicates_clusters(self):
        make_clustered_districts()
        run_pipeline(stdout=_Quiet())
        self.assertEqual(DistrictClusterAssignment.objects.count(), 14)
        self.assertEqual(SafetyCluster.objects.filter(algorithm="kmeans").count(),
                         ClusterEvaluation.objects.filter(algorithm="kmeans", selected=True, run=ModelRun.objects.first()).get().k)
