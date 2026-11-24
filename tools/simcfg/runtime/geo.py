"""Basit coğrafi yardımcılar (referans kalitesi: küresel yer, yerel düzlem yaklaşımı)."""
from __future__ import annotations

import math

R_EARTH = 6371000.0


def distance(a: dict, b: dict, alt_a: float = 0.0, alt_b: float = 0.0) -> float:
    """Haversine yatay mesafe + irtifa farkı (m)."""
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    horiz = 2 * R_EARTH * math.asin(min(1.0, math.sqrt(h)))
    return math.hypot(horiz, (alt_b or 0) - (alt_a or 0))


def bearing(a: dict, b: dict) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _local(p: dict, ref: dict) -> tuple[float, float]:
    x = math.radians(p["lon"] - ref["lon"]) * R_EARTH * math.cos(math.radians(ref["lat"]))
    y = math.radians(p["lat"] - ref["lat"]) * R_EARTH
    return x, y


def _seg_dist(p, a, b) -> float:
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def in_geometry(pos: dict, g: dict) -> bool:
    shape = g.get("shape")
    if shape == "circle":
        return distance(pos, g["center"]) <= g["radius"]
    if shape == "sector":
        d = distance(g["center"], pos)
        if d > g["radius"] or d < g.get("minRadius", 0):
            return False
        brg, s, e = bearing(g["center"], pos), g["startBearing"] % 360, g["endBearing"] % 360
        return s <= brg <= e if s <= e else (brg >= s or brg <= e)
    if shape == "ellipse":
        x, y = _local(pos, g["center"])
        th = math.radians(g.get("orientation", 0))
        u, v = x * math.sin(th) + y * math.cos(th), x * math.cos(th) - y * math.sin(th)
        return (u / g["semiMajor"]) ** 2 + (v / g["semiMinor"]) ** 2 <= 1
    pts = g.get("points") or []
    if not pts:
        return False
    ref = pts[0]
    p = _local(pos, ref)
    loc = [_local(q, ref) for q in pts]
    if shape == "corridor":
        return any(_seg_dist(p, loc[i], loc[i + 1]) <= g["width"] / 2 for i in range(len(loc) - 1))
    inside = False  # polygon: ray casting
    for i in range(len(loc)):
        (x1, y1), (x2, y2) = loc[i], loc[(i + 1) % len(loc)]
        if (y1 > p[1]) != (y2 > p[1]) and p[0] < (x2 - x1) * (p[1] - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def in_area(pos: dict | None, alt: float | None, area: dict) -> bool:
    if not pos or not area.get("active", True):
        return False
    a = area.get("altitude") or {}
    if alt is not None and (("min" in a and alt < a["min"]) or ("max" in a and alt > a["max"])):
        return False
    return in_geometry(pos, area["geometry"])
