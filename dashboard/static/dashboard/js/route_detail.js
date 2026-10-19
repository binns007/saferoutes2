(function () {
  const mapEl = document.getElementById("route-map");
  if (!mapEl) return;

  const map = L.map("route-map").setView([10.4, 76.4], 8);
  L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
    {
      attribution:
        "Tiles &copy; Esri &mdash; Esri, HERE, Garmin, USGS, EPA, NPS",
      maxZoom: 13,
    }
  ).addTo(map);

  const liveBtn = document.getElementById("live-track-btn");
  const liveStatus = document.getElementById("live-track-status");
  let liveMarker = null;
  let liveWatchId = null;
  let lastPingAt = 0;
  let lastLiveFix = null; // {lat, lon} of the most recent GPS fix, used for "Reroute me"
  const MIN_PING_INTERVAL_MS = 4000; // don't hammer the server on jittery GPS updates
  let emergencyContact = null;       // {name, auto_escalate} or null -- never the address
  const notifyBtn = document.getElementById("notify-contact-btn");

  function getCsrfToken() {
    const input = document.querySelector("#sr-csrf-form input[name=csrfmiddlewaretoken]");
    return input ? input.value : "";
  }

  function worstAlert(alerts) {
    const unseen = alerts.filter((a) => !a.acknowledged);
    if (!unseen.length) return null;
    const critical = unseen.find((a) => a.severity === "critical");
    return critical || unseen[0];
  }

  function notifyContact(automatic) {
    return window.SafeRoutesAlertModal.postAction(`/api/routes/${ROUTE_ID}/notify-contact/`, null, { automatic })
      .then((r) => r.json().then((body) => ({ ok: r.ok, body })))
      .then(({ ok, body }) => {
        if (liveStatus) {
          liveStatus.textContent = ok
            ? `${body.contact} has been emailed your journey and last known position.`
            : body.error || "Couldn't notify your contact.";
        }
        if (ok) setTimeout(() => window.location.reload(), 2500);
        return ok;
      })
      .catch(() => {
        if (liveStatus) liveStatus.textContent = "Couldn't reach SafeRoutes to notify your contact. Call the emergency number 112 if you need help now.";
      });
  }

  function showAlertPopup(alert, extraCount) {
    window.SafeRoutesAlertModal.show({
      severity: alert.severity,
      title: alert.severity === "critical" ? "You're in a high-risk zone" : "Route deviation detected",
      message: alert.message,
      subtext: extraCount > 0
        ? `+ ${extraCount} more alert${extraCount === 1 ? "" : "s"} on this journey.`
        : (emergencyContact
            ? (emergencyContact.auto_escalate
                ? `You chose to have ${emergencyContact.name} notified if you don't respond.`
                : `${emergencyContact.name} is only notified if you choose to.`)
            : "This alert is shown to you only. SafeRoutes does not contact anyone on your behalf."),
      contact: emergencyContact,
      onNotify: notifyContact,
      onReroute: () => {
        window.SafeRoutesAlertModal.postAction(`/api/routes/${ROUTE_ID}/reroute/`, null, lastLiveFix || undefined)
          .then(() => window.location.reload());
      },
      onContinue: () => {
        window.SafeRoutesAlertModal.postAction(`/api/routes/${ROUTE_ID}/acknowledge/`, "continued")
          .then(() => window.location.reload());
      },
      onDismiss: () => {
        window.SafeRoutesAlertModal.postAction(`/api/routes/${ROUTE_ID}/acknowledge/`, "dismissed")
          .then(() => window.location.reload());
      },
    });
  }

  function maybeShowAlertPopup(data) {
    const alert = worstAlert(data.alerts || []);
    if (!alert) return;
    const extraCount = data.alerts.filter((a) => !a.acknowledged).length - 1;
    showAlertPopup(alert, extraCount);
  }

  function renderStaticRoute(data) {
    const bounds = [];

    // other candidate roads that were considered but not recommended
    (data.alternatives || []).forEach((alt) => {
      L.polyline(alt.polyline, { color: "#9aa3b8", weight: 3, opacity: 0.7, dashArray: "2 8" })
        .addTo(map)
        .bindPopup(
          `Alternative road: ${alt.distance_km} km` +
          (alt.combined_score != null ? `<br/>Safety score ${alt.combined_score.toFixed(1)}/100` : "")
        );
    });

    // the route as first planned, if the traveller has since been rerouted
    if (data.original_waypoints) {
      L.polyline(data.original_waypoints.map((wp) => [wp[0], wp[1]]), {
        color: "#9aa3b8", weight: 3, opacity: 0.9,
      }).addTo(map).bindPopup("Original plan (before reroute)");
    }

    // 1.96 x sigma safety corridor around the recommended path
    if (data.buffer_polygon && data.buffer_polygon.length) {
      L.polygon(data.buffer_polygon, {
        color: "#35C4A1", weight: 1, opacity: 0.6, fillColor: "#35C4A1", fillOpacity: 0.12,
      }).addTo(map).bindPopup(`Safety buffer: ${data.safety_buffer_km} km either side of the path`);
    }

    // planned path
    const planned = data.waypoints.map((wp) => [wp[0], wp[1]]);
    L.polyline(planned, { color: "#1E2A4A", weight: 4 }).addTo(map);
    planned.forEach((p) => bounds.push(p));

    // start / end markers
    L.marker(planned[0]).addTo(map).bindPopup(`<strong>${data.source}</strong> (start)`);
    L.marker(planned[planned.length - 1]).addTo(map).bindPopup(`<strong>${data.destination}</strong> (end)`);

    // actual GPS pings (simulated and/or live, whichever have been recorded)
    const actualPath = [];
    data.pings.forEach((p) => {
      actualPath.push([p.lat, p.lon]);
      const color = p.in_unsafe_zone ? "#E8546B" : "#35C4A1";
      const marker = L.circleMarker([p.lat, p.lon], {
        radius: p.in_unsafe_zone ? 9 : 5,
        color,
        fillColor: color,
        fillOpacity: 0.8,
        weight: p.in_unsafe_zone ? 3 : 1,
      }).addTo(map);
      marker.bindPopup(`
        Ping #${p.sequence}<br/>
        Nearest: ${p.nearest_district ?? "-"}<br/>
        Safety score: ${p.safety_score ? p.safety_score.toFixed(1) : "-"}<br/>
        ${p.in_unsafe_zone ? "<strong style='color:#E8546B'>Deviation into unsafe zone</strong>" : "On safe path"}
      `);
      bounds.push([p.lat, p.lon]);
    });

    if (actualPath.length) {
      L.polyline(actualPath, { color: "#F2A65A", weight: 3 }).addTo(map);
    }

    if (bounds.length) map.fitBounds(bounds, { padding: [30, 30] });
  }

  // ---------- live location tracking (navigator.geolocation) ----------

  function sendLivePing(lat, lon, accuracy) {
    return fetch(`/api/routes/${ROUTE_ID}/live-ping/`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": getCsrfToken(),
      },
      body: JSON.stringify({ lat, lon, accuracy }),
    }).then((r) => r.json());
  }

  function updateLiveMarker(lat, lon, inUnsafeZone) {
    const color = inUnsafeZone ? "#E8546B" : "#35C4A1";
    if (!liveMarker) {
      liveMarker = L.circleMarker([lat, lon], {
        radius: 10,
        color: "#fff",
        weight: 2,
        fillColor: color,
        fillOpacity: 0.95,
      }).addTo(map);
      liveMarker.bindPopup("Your live location");
    } else {
      liveMarker.setLatLng([lat, lon]);
      liveMarker.setStyle({ fillColor: color });
    }
    map.panTo([lat, lon]);
  }

  function handlePosition(position) {
    const { latitude, longitude, accuracy } = position.coords;
    lastLiveFix = { lat: latitude, lon: longitude };
    updateLiveMarker(latitude, longitude, false);

    const now = Date.now();
    if (now - lastPingAt < MIN_PING_INTERVAL_MS) return;
    lastPingAt = now;

    sendLivePing(latitude, longitude, accuracy).then((data) => {
      if (!data || data.error) return;
      updateLiveMarker(latitude, longitude, data.ping.in_unsafe_zone);
      if (data.alert) showAlertPopup(data.alert, 0);
    });
  }

  function handlePositionError(err) {
    if (liveStatus) liveStatus.textContent = "Couldn't get your location: " + err.message;
    stopLiveTracking();
  }

  function startLiveTracking() {
    if (!("geolocation" in navigator)) {
      if (liveStatus) liveStatus.textContent = "Geolocation isn't supported on this device.";
      return;
    }
    liveWatchId = navigator.geolocation.watchPosition(handlePosition, handlePositionError, {
      enableHighAccuracy: true,
      maximumAge: 5000,
      timeout: 15000,
    });
    if (liveBtn) {
      liveBtn.textContent = "Stop live tracking";
      liveBtn.classList.add("btn-ghost");
    }
    if (liveStatus) {
      liveStatus.textContent =
        "Live tracking on \u2014 each position is sent to SafeRoutes and saved to this route, visible only to you.";
    }
  }

  function stopLiveTracking() {
    if (liveWatchId !== null) {
      navigator.geolocation.clearWatch(liveWatchId);
      liveWatchId = null;
    }
    if (liveBtn) {
      liveBtn.textContent = "Track my live location";
      liveBtn.classList.remove("btn-ghost");
    }
  }

  if (liveBtn) {
    liveBtn.addEventListener("click", () => {
      if (liveWatchId !== null) {
        stopLiveTracking();
        if (liveStatus) liveStatus.textContent = "Live tracking stopped.";
      } else {
        startLiveTracking();
      }
    });
  }

  if (notifyBtn) {
    notifyBtn.addEventListener("click", () => {
      if (window.confirm("Email your emergency contact your journey and last known position now?")) {
        notifyContact(false);
      }
    });
  }

  fetch(`/api/routes/${ROUTE_ID}/`)
    .then((r) => {
      if (!r.ok) throw new Error("route unavailable");
      return r.json();
    })
    .then((data) => {
      emergencyContact = data.emergency_contact || null;
      maybeShowAlertPopup(data);
      renderStaticRoute(data);
    })
    .catch(() => {
      if (liveStatus) liveStatus.textContent = "Couldn't load this route. Please log in again.";
    });
})();
