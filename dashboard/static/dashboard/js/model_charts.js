(function () {
  const el = document.getElementById("chart-data");
  if (!el || typeof Chart === "undefined") return;
  const d = JSON.parse(el.textContent);

  const NAVY = "#1E2A4A", TEAL = "#35C4A1", AMBER = "#F2A65A";
  const grid = { color: "rgba(30,42,74,0.08)" };

  function line(canvasId, datasets, yTitle) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    new Chart(canvas, {
      type: "line",
      data: { labels: d.k, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: datasets.length > 1 } },
        scales: {
          x: { title: { display: true, text: "Number of clusters (k)" }, grid },
          y: { title: { display: true, text: yTitle }, grid },
        },
      },
    });
  }

  const style = (color) => ({
    borderColor: color, backgroundColor: color, pointRadius: 4, tension: 0.2, borderWidth: 2,
  });

  line("chart-elbow", [{ label: "Inertia", data: d.inertia, ...style(NAVY) }], "Within-cluster sum of squares");
  line("chart-sil", [
    { label: "K-Means", data: d.kmeans_silhouette, ...style(TEAL) },
    { label: "GMM", data: d.gmm_silhouette, ...style(AMBER) },
  ], "Silhouette");
  line("chart-bic", [{ label: "BIC", data: d.gmm_bic, ...style(AMBER) }], "BIC");
})();
