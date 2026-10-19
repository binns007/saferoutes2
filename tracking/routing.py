"""
Road-routing client (OSRM-compatible HTTP API).

`fetch_candidates()` asks the routing server for the fastest route plus
alternatives. The safety logic lives in `services.py`; this module only
turns an origin/destination into candidate geometries, so the engine can be
swapped (self-hosted OSRM, a Google Directions adapter) without touching the
scoring code.
"""
from dataclasses import dataclass

import requests
from django.conf import settings


class RoutingUnavailable(Exception):
    """The routing server couldn't be reached or returned no usable route."""


@dataclass
class Candidate:
    polyline: list          # [(lat, lon), ...]
    distance_km: float
    duration_min: float


def fetch_candidates(origin, destination, max_alternatives=3):
    """
    origin/destination are (lat, lon). Returns a list of Candidate (best first
    as ranked by the routing engine). Raises RoutingUnavailable on any failure.
    """
    base = settings.OSRM_BASE_URL.rstrip("/")
    coords = f"{origin[1]:.6f},{origin[0]:.6f};{destination[1]:.6f},{destination[0]:.6f}"
    url = f"{base}/route/v1/driving/{coords}"
    params = {
        "alternatives": str(max_alternatives),
        "overview": "full",
        "geometries": "geojson",
        "steps": "false",
    }
    try:
        response = requests.get(url, params=params, timeout=settings.OSRM_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RoutingUnavailable(str(exc)) from exc

    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise RoutingUnavailable(payload.get("message") or payload.get("code") or "no route")

    candidates = []
    for route in payload["routes"]:
        coords_lonlat = route.get("geometry", {}).get("coordinates") or []
        if len(coords_lonlat) < 2:
            continue
        candidates.append(Candidate(
            polyline=[(lat, lon) for lon, lat in coords_lonlat],
            distance_km=route["distance"] / 1000.0,
            duration_min=route["duration"] / 60.0,
        ))
    if not candidates:
        raise RoutingUnavailable("routing server returned empty geometry")
    return candidates
