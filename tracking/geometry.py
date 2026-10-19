"""
Small geodesy helpers for route work.

Everything here works on (lat, lon) pairs in degrees and returns kilometres.
Distances to a path are measured to the *segments* between vertices, not just
to the vertices themselves, so a point sitting exactly on a long straight
segment reads as ~0 km off-route rather than half the vertex spacing.

For point-to-segment and buffering we project to a local equirectangular
plane (x = east, y = north, in km) centred on the area of interest. At
Kerala's scale (a few hundred km) that is accurate to well under 1%, which is
far inside the noise of GPS and district-level scoring.
"""
import math

from shapely.geometry import LineString

EARTH_RADIUS_KM = 6371.0088
KM_PER_DEG_LAT = math.pi * EARTH_RADIUS_KM / 180.0


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _project(lat, lon, lat0, lon0):
    x = (lon - lon0) * math.cos(math.radians(lat0)) * KM_PER_DEG_LAT
    y = (lat - lat0) * KM_PER_DEG_LAT
    return x, y


def _unproject(x, y, lat0, lon0):
    lat = lat0 + y / KM_PER_DEG_LAT
    lon = lon0 + x / (KM_PER_DEG_LAT * math.cos(math.radians(lat0)))
    return lat, lon


def distance_to_polyline_km(lat, lon, polyline):
    """Shortest distance from a point to the polyline's segments."""
    pts = [(p[0], p[1]) for p in polyline]
    if not pts:
        raise ValueError("polyline is empty")
    if len(pts) == 1:
        return haversine_km(lat, lon, pts[0][0], pts[0][1])

    # project relative to the query point, so the point is the origin
    xy = [_project(p[0], p[1], lat, lon) for p in pts]
    best = float("inf")
    for (ax, ay), (bx, by) in zip(xy, xy[1:]):
        dx, dy = bx - ax, by - ay
        seg_len_sq = dx * dx + dy * dy
        if seg_len_sq == 0:
            d = math.hypot(ax, ay)
        else:
            t = max(0.0, min(1.0, -(ax * dx + ay * dy) / seg_len_sq))
            d = math.hypot(ax + t * dx, ay + t * dy)
        if d < best:
            best = d
    return best


def polyline_length_km(polyline):
    return sum(
        haversine_km(a[0], a[1], b[0], b[1]) for a, b in zip(polyline, polyline[1:])
    )


def resample_polyline(polyline, count):
    """`count` points spaced evenly by distance along the polyline (ends included)."""
    pts = [(p[0], p[1]) for p in polyline]
    if count < 2 or len(pts) < 2:
        return pts[:1] * max(count, 1)
    seg_lengths = [haversine_km(a[0], a[1], b[0], b[1]) for a, b in zip(pts, pts[1:])]
    total = sum(seg_lengths)
    if total == 0:
        return [pts[0]] * count

    targets = [total * i / (count - 1) for i in range(count)]
    out, seg_index, walked = [], 0, 0.0
    for target in targets:
        while seg_index < len(seg_lengths) - 1 and walked + seg_lengths[seg_index] < target:
            walked += seg_lengths[seg_index]
            seg_index += 1
        seg = seg_lengths[seg_index]
        t = 0.0 if seg == 0 else min(max((target - walked) / seg, 0.0), 1.0)
        a, b = pts[seg_index], pts[seg_index + 1]
        out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return out


def sample_every_km(polyline, step_km):
    """Points roughly every `step_km` along the polyline (always includes both ends)."""
    length = polyline_length_km(polyline)
    count = max(2, int(math.ceil(length / step_km)) + 1)
    return resample_polyline(polyline, count)


def simplify_polyline(polyline, tolerance_km=0.05):
    """Douglas-Peucker simplification, to keep stored/serialised geometry small."""
    pts = [(p[0], p[1]) for p in polyline]
    if len(pts) <= 2:
        return pts
    lat0, lon0 = pts[0]
    xy = [_project(lat, lon, lat0, lon0) for lat, lon in pts]
    simplified = LineString(xy).simplify(tolerance_km, preserve_topology=False)
    return [_unproject(x, y, lat0, lon0) for x, y in simplified.coords]


def buffer_polygon(polyline, radius_km, simplify_km=0.1):
    """Ring of [lat, lon] points around the polyline, `radius_km` out on every side."""
    pts = [(p[0], p[1]) for p in polyline]
    if not pts or radius_km <= 0:
        return []
    lat0, lon0 = pts[0]
    xy = [_project(lat, lon, lat0, lon0) for lat, lon in pts]
    shape = LineString(xy) if len(xy) > 1 else None
    if shape is None:
        from shapely.geometry import Point
        poly = Point(xy[0]).buffer(radius_km)
    else:
        poly = shape.simplify(simplify_km).buffer(radius_km, quad_segs=6)
    return [list(_unproject(x, y, lat0, lon0)) for x, y in poly.exterior.coords]
