"""
Stages 2-4 of the SafeRoutes pipeline: pre-processing, feature selection,
clustering (K-Means + Gaussian Mixture Model) and benchmark-based safety
scoring. Runnable standalone from the `train_clusters` management command.

Design notes
------------
* Only indicators that actually have data for every district are used.
  Nothing is imputed or invented: a feature with any missing value is
  dropped and the reason recorded on the ModelRun.
* Feature selection is correlation-based: an optional feature that is
  (near-)constant, or correlated |r| >= CORRELATION_DROP_THRESHOLD with a
  feature already kept, is dropped. The three "core" indicators that also
  drive the safety score (crime rate, police presence, literacy) are never
  dropped for correlation.
* Cluster counts 2..MAX_K are evaluated for both algorithms and stored (the
  data behind the elbow / BIC / silhouette plots). The *selected* model is
  restricted to SELECTABLE_K (3 or 4) because the tier scheme has 3-4 levels;
  if the sweep disagrees, that is recorded in ModelRun.notes rather than hidden.
* Safety scores are min-max normalised against the districts plus any
  benchmark rows. Benchmark rows are used only when present; with none, the
  score is relative to the loaded districts and the run says so.
* GMMs use diagonal covariance: with ~14 districts, full covariance has more
  parameters than data.
"""
import json

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.metrics import (
    calinski_harabasz_score, davies_bouldin_score, silhouette_samples, silhouette_score,
)
from sklearn.mixture import GaussianMixture
from sklearn.preprocessing import StandardScaler

from clustering.models import (
    BenchmarkCity, ClusterEvaluation, DistrictClusterAssignment, ModelRun, SafetyCluster,
)
from core.models import District

CORE_FEATURES = ["crime_rate_per_100k", "police_per_100k", "literacy_rate"]
OPTIONAL_FEATURES = [
    "literacy_disparity", "per_capita_expenditure", "forest_cover_pct", "population_growth_rate",
]
CANDIDATE_FEATURES = CORE_FEATURES + OPTIONAL_FEATURES
CORRELATION_DROP_THRESHOLD = 0.90

SCORE_WEIGHTS = {"crime_rate_per_100k": 0.5, "police_per_100k": 0.3, "literacy_rate": 0.2}

EVAL_K_RANGE = range(2, 7)       # evidence: every k tried
SELECTABLE_K = (3, 4)            # what the 4-tier scheme can represent

TIER_COLORS = {
    "I": "#35C4A1",    # Safe Teal
    "II": "#7CC6A6",   # softer teal-green
    "III": "#F2A65A",  # Beacon Amber
    "IV": "#E8546B",   # Risk Coral
}


def _normalize(series):
    lo, hi = series.min(), series.max()
    if hi - lo < 1e-9:
        return series * 0 + 0.5
    return (series - lo) / (hi - lo)


def _matrix_json(df):
    """DataFrame -> JSON-safe dict (NaN becomes null)."""
    return json.loads(df.round(3).to_json())


# ------------------------------------------------------------ stage 1 -> 2

def load_district_frame():
    """Stage 1->2: pull districts from the DB and (re)compute engineered fields."""
    qs = District.objects.all()
    if not qs.exists():
        raise ValueError("No districts found. Run `manage.py load_real_data` first.")

    df = pd.DataFrame.from_records(qs.values(
        "id", "name", "latitude", "longitude", "population", "literacy_rate",
        "police_strength", "per_capita_expenditure", "forest_cover_pct",
        "population_growth_rate", "crime_rape", "crime_kidnapping", "crime_other_ipc_women",
        "crime_years",
    ))
    for col in ("police_strength", "per_capita_expenditure", "forest_cover_pct", "population_growth_rate"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["total_crimes"] = df["crime_rape"] + df["crime_kidnapping"] + df["crime_other_ipc_women"]
    df["crime_rate_per_100k"] = df["total_crimes"] / df["population"] * 100_000
    df["police_per_100k"] = df["police_strength"] / df["population"] * 100_000
    df["literacy_disparity"] = (df["literacy_rate"] - df["literacy_rate"].mean()).abs()

    # persist engineered fields back onto the District rows (Stage 2 output)
    for row in df.itertuples():
        District.objects.filter(pk=row.id).update(
            crime_rate_per_100k=round(row.crime_rate_per_100k, 3),
            police_per_100k=None if pd.isna(row.police_per_100k) else round(row.police_per_100k, 3),
            literacy_disparity=round(row.literacy_disparity, 3),
        )
    return df


# ------------------------------------------------------------ stage 3

def feature_correlation_report(df, features):
    """Pearson + Spearman correlation matrices over the given features."""
    return df[features].corr(method="pearson"), df[features].corr(method="spearman")


def select_features(df):
    """
    Correlation-based feature selection. Returns (kept, dropped) where dropped
    is a list of {"feature", "reason"}.
    """
    dropped, available = [], []
    for col in CANDIDATE_FEATURES:
        series = df[col]
        if series.isna().any():
            dropped.append({"feature": col, "reason": "no data for one or more districts"})
        elif series.std() < 1e-9:
            dropped.append({"feature": col, "reason": "constant across districts"})
        else:
            available.append(col)

    pearson = df[available].corr(method="pearson") if available else pd.DataFrame()
    kept = []
    for col in available:
        if col in CORE_FEATURES:
            kept.append(col)
            continue
        clash = next((k for k in kept if abs(pearson.loc[col, k]) >= CORRELATION_DROP_THRESHOLD), None)
        if clash:
            dropped.append({
                "feature": col,
                "reason": f"|Pearson r| = {abs(pearson.loc[col, clash]):.2f} with {clash} (redundant)",
            })
        else:
            kept.append(col)
    return kept, dropped


# ------------------------------------------------------------ stage 4

def compute_safety_scores(df):
    """
    Benchmark each district on crime rate, police presence and literacy,
    producing a 0-100 comparative safety score. Weights are renormalised over
    the indicators that exist. Returns (df_with_scores, weights_used, reference_label).
    """
    indicators = [c for c in SCORE_WEIGHTS if not df[c].isna().any()]
    if "crime_rate_per_100k" not in indicators:
        raise ValueError("Crime rate is required for safety scoring.")
    weights = {c: SCORE_WEIGHTS[c] / sum(SCORE_WEIGHTS[i] for i in indicators) for c in indicators}

    frames = [df[indicators]]
    reference = "relative to the loaded districts (no benchmark cities loaded)"
    bench_qs = BenchmarkCity.objects.all()
    if bench_qs.exists():
        bench = pd.DataFrame.from_records(bench_qs.values(*indicators))
        usable = bench.dropna(subset=indicators)
        if not usable.empty:
            frames.append(usable)
            reference = f"districts + {len(usable)} benchmark cities"
    combined = pd.concat(frames, ignore_index=True)

    score = 0
    for col, w in weights.items():
        norm = _normalize(combined[col])
        score = score + w * ((1 - norm) if col == "crime_rate_per_100k" else norm)
    score = score * 100

    df = df.copy()
    df["safety_score"] = score.iloc[: len(df)].round(2).values
    return df, {k: round(v, 3) for k, v in weights.items()}, reference


def _fit_kmeans(X, k):
    model = KMeans(n_clusters=k, random_state=42, n_init=10)
    return model, model.fit_predict(X)


def _fit_gmm(X, k):
    model = GaussianMixture(n_components=k, covariance_type="diag", random_state=42, n_init=5)
    return model, model.fit_predict(X)


def _metrics(X, labels):
    if len(set(labels)) < 2 or len(set(labels)) >= len(X):
        return {"silhouette": None, "calinski_harabasz": None, "davies_bouldin": None}
    return {
        "silhouette": float(silhouette_score(X, labels)),
        "calinski_harabasz": float(calinski_harabasz_score(X, labels)),
        "davies_bouldin": float(davies_bouldin_score(X, labels)),
    }


def _tier_for_rank(rank, n_clusters):
    # rank 0 = safest cluster
    if n_clusters == 3:
        mapping = {0: "I", 1: "II", 2: "IV"}
    else:
        mapping = {0: "I", 1: "II", 2: "III", 3: "IV"}
    return mapping.get(rank, "IV")


def run_pipeline(stdout=None):
    def log(msg):
        if stdout:
            stdout.write(msg)
        else:
            print(msg)

    df = load_district_frame()
    n = len(df)
    log(f"Loaded {n} districts.")
    if n < 5:
        raise ValueError(f"Need at least 5 districts to cluster meaningfully; found {n}.")

    features, dropped = select_features(df)
    for d in dropped:
        log(f"Feature dropped: {d['feature']} ({d['reason']})")
    log(f"Features used for clustering: {', '.join(features)}")

    pearson, spearman = feature_correlation_report(df, features)
    log("Pearson correlation:\n" + pearson.round(3).to_string())
    log("Spearman correlation:\n" + spearman.round(3).to_string())

    df, weights, reference = compute_safety_scores(df)
    log(f"Safety score weights: {weights}; scored {reference}.")

    X = StandardScaler().fit_transform(df[features])
    max_k = min(max(EVAL_K_RANGE), n - 1)
    k_values = [k for k in EVAL_K_RANGE if k <= max_k]

    run = ModelRun.objects.create(
        n_districts=n,
        data_years=", ".join(sorted({y for y in df["crime_years"] if y})),
        features=features,
        dropped_features=dropped,
        pearson=_matrix_json(pearson),
        spearman=_matrix_json(spearman),
        scoring_weights=weights,
        scoring_reference=reference,
    )

    fits = {"kmeans": {}, "gmm": {}}
    for k in k_values:
        for algo, fitter in (("kmeans", _fit_kmeans), ("gmm", _fit_gmm)):
            model, labels = fitter(X, k)
            m = _metrics(X, labels)
            fits[algo][k] = (model, labels, m)
            ClusterEvaluation.objects.create(
                run=run, algorithm=algo, k=k,
                inertia=float(model.inertia_) if algo == "kmeans" else None,
                bic=float(model.bic(X)) if algo == "gmm" else None,
                aic=float(model.aic(X)) if algo == "gmm" else None,
                **m,
            )

    selectable = [k for k in SELECTABLE_K if k in k_values]
    chosen = {
        "kmeans": max(selectable, key=lambda k: fits["kmeans"][k][2]["silhouette"] or -1),
        "gmm": min(selectable, key=lambda k: ClusterEvaluation.objects.get(run=run, algorithm="gmm", k=k).bic),
    }

    notes = []
    overall = max(k_values, key=lambda k: fits["kmeans"][k][2]["silhouette"] or -1)
    if overall not in SELECTABLE_K:
        notes.append(
            f"K-Means silhouette peaks at k={overall} across k={k_values[0]}-{k_values[-1]}, but the "
            f"tier scheme represents 3-4 clusters, so k={chosen['kmeans']} was selected from {list(SELECTABLE_K)}."
        )
    run.notes = " ".join(notes)
    run.save(update_fields=["notes"])
    if notes:
        log("NOTE: " + run.notes)

    results = {}
    for algo in ("kmeans", "gmm"):
        k = chosen[algo]
        _model, labels, m = fits[algo][k]
        ClusterEvaluation.objects.filter(run=run, algorithm=algo, k=k).update(selected=True)
        log(f"[{algo}] chosen k={k} silhouette={m['silhouette']:.3f} "
            f"calinski_harabasz={m['calinski_harabasz']:.1f} davies_bouldin={m['davies_bouldin']:.3f}")
        results[algo] = {"k": k, "labels": labels, **m}

        per_sample_sil = silhouette_samples(X, labels)

        tmp = df.copy()
        tmp["_label"] = labels
        cluster_means = tmp.groupby("_label")["safety_score"].mean().sort_values(ascending=False)
        rank_of_label = {label: rank for rank, label in enumerate(cluster_means.index)}

        SafetyCluster.objects.filter(algorithm=algo).delete()
        cluster_objs = {}
        for label, mean_score in cluster_means.items():
            tier = _tier_for_rank(rank_of_label[label], k)
            members = labels == label
            cluster_objs[label] = SafetyCluster.objects.create(
                algorithm=algo,
                cluster_index=int(label),
                tier=tier,
                color_hex=TIER_COLORS[tier],
                avg_safety_score=round(float(mean_score), 2),
                district_count=int(members.sum()),
                silhouette_score=round(float(per_sample_sil[members].mean()), 4),
            )

        if algo == "kmeans":
            # K-Means is the primary, user-facing safety assignment. GMM clusters
            # are saved for comparison in the admin and don't touch this table.
            DistrictClusterAssignment.objects.all().delete()
            for row, label in zip(df.itertuples(), labels):
                DistrictClusterAssignment.objects.update_or_create(
                    district_id=row.id,
                    defaults={"cluster": cluster_objs[label], "safety_score": round(float(row.safety_score), 2)},
                )

    log("Pipeline complete. K-Means assignments are the primary safety map;"
        " GMM clusters are stored for comparison in the admin panel.")
    return results
