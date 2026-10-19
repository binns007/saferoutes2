(function () {
  const mapEl = document.getElementById("map");
  if (!mapEl) return;

  const map = L.map("map", { scrollWheelZoom: true }).setView([10.4, 76.4], 7);

  L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
    {
      attribution:
        "Tiles &copy; Esri &mdash; Esri, HERE, Garmin, USGS, EPA, NPS",
      maxZoom: 12,
    }
  ).addTo(map);

  fetch("/api/clusters/")
    .then((r) => r.json())
    .then((data) => {
      const bounds = [];
      data.districts.forEach((d) => {
        const radius = 8 + (100 - d.safety_score) / 6;
        const marker = L.circleMarker([d.lat, d.lon], {
          radius: radius,
          color: d.color,
          fillColor: d.color,
          fillOpacity: 0.55,
          weight: 2,
        }).addTo(map);

        marker.bindPopup(`
          <strong>${d.name}</strong><br/>
          Tier: ${d.tier_label}<br/>
          Safety score: ${d.safety_score.toFixed(1)} / 100<br/>
          Crime / 100k: ${d.crime_rate_per_100k?.toFixed(1) ?? "-"}<br/>
          Police / 100k: ${d.police_per_100k?.toFixed(1) ?? "-"}<br/>
          Literacy: ${d.literacy_rate?.toFixed(1) ?? "-"}%
        `);

        bounds.push([d.lat, d.lon]);
      });
      if (bounds.length) map.fitBounds(bounds, { padding: [30, 30] });
    });
})();