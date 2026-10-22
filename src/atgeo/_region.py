"""Regions: validation, centre, the circle's enclosing box, snapping and bbox strings (contract §3)."""

from __future__ import annotations

import math

from .types import Bbox, Circle, Region

DEG_TO_M = 111194.927
EARTH_RADIUS_M = 6371008.8
_EPSILON = 1e-9
_RAD = math.pi / 180

LonSegment = tuple[float, float]


def _ok(x: float, limit: float) -> bool:
    return math.isfinite(x) and abs(x) <= limit


def validate_region(region: Region) -> None:
    """Raise before any request, in the contract's order, so a call with two faults fails the same everywhere."""
    if isinstance(region, Bbox):
        if not all(_ok(x, 180) for x in (region.min_lon, region.max_lon)):
            raise ValueError(f"invalid bbox longitude: {region}")
        if not all(_ok(x, 90) for x in (region.min_lat, region.max_lat)):
            raise ValueError(f"invalid bbox latitude: {region}")
        if region.min_lat > region.max_lat:
            raise ValueError(f"invalid bbox: min_lat > max_lat: {region}")
    elif isinstance(region, Circle):
        if not _ok(region.lat, 90) or not _ok(region.lon, 180):
            raise ValueError(f"invalid circle centre: {region}")
        if not math.isfinite(region.radius) or region.radius < 0:
            raise ValueError(f"invalid circle radius: {region}")
    else:
        raise TypeError(f"region must be a Circle or a Bbox, not {type(region).__name__}")


def center(region: Region) -> tuple[float, float]:
    """(lat, lon) of the region's centre."""
    if isinstance(region, Circle):
        return region.lat, region.lon
    if region.min_lon <= region.max_lon:
        lon = (region.min_lon + region.max_lon) / 2
    else:
        lon = (region.min_lon + region.max_lon + 360) / 2
    if lon > 180:
        lon -= 360
    return (region.min_lat + region.max_lat) / 2, lon


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    a = (
        math.sin((lat2 - lat1) * _RAD / 2) ** 2
        + math.cos(lat1 * _RAD) * math.cos(lat2 * _RAD) * math.sin((lon2 - lon1) * _RAD / 2) ** 2
    )
    # As JavaScript computes it: out-of-range place coordinates (kept as parsed, contract §5) can make `a`
    # negative, where math.sqrt raises and Math.sqrt gives NaN, and Math.min(1, NaN) is NaN where min() is 1.
    root = math.sqrt(a) if a >= 0 else math.nan
    return 2 * EARTH_RADIUS_M * math.asin(1.0 if root > 1 else root)


def lon_segments(min_lon: float, max_lon: float) -> list[LonSegment]:
    """One segment, or two when the span crosses the antimeridian."""
    if min_lon <= max_lon:
        return [(min_lon, max_lon)]
    return [(min_lon, 180.0), (-180.0, max_lon)]


def lon_in_segments(lon: float, segments: list[LonSegment]) -> bool:
    return any(lo <= lon <= hi for lo, hi in segments)


def lon_segments_overlap(a: list[LonSegment], b: list[LonSegment]) -> bool:
    return any(alo <= bhi and blo <= ahi for alo, ahi in a for blo, bhi in b)


def _circle_box(lat: float, lon: float, radius: float) -> tuple[float, float, float, float]:
    half = radius / DEG_TO_M
    min_lat = max(lat - half, -90.0)
    max_lat = min(lat + half, 90.0)
    # The spherical cap's true half-width; at or past 1 (or NaN) the cap holds a pole.
    sin_half_lon = math.sin(half * _RAD) / math.cos(lat * _RAD)
    if min_lat == -90 or max_lat == 90 or not sin_half_lon < 1:
        return -180.0, min_lat, 180.0, max_lat
    half_lon = math.asin(sin_half_lon) / _RAD

    def wrap(x: float) -> float:
        return x - 360 if x > 180 else x + 360 if x < -180 else x

    return wrap(lon - half_lon), min_lat, wrap(lon + half_lon), max_lat


def _snap_down(x: float) -> int:
    # round()'s tie rule never matters: r is used only when x * 100 is within 1e-9 of it.
    r = round(x * 100)
    return r if abs(x * 100 - r) < _EPSILON else math.floor(x * 100)


def _snap_up(x: float) -> int:
    r = round(x * 100)
    return r if abs(x * 100 - r) < _EPSILON else math.ceil(x * 100)


def _snap_span(lo_deg: float, hi_deg: float, limit: int) -> tuple[int, int]:
    lo, hi = _snap_down(lo_deg), _snap_up(hi_deg)
    if lo == hi:
        if hi < limit * 100:
            hi += 1
        else:
            lo -= 1
    return lo, hi


def _fixed(units: int) -> str:
    # From the integer, so there is no locale, no float and no negative zero.
    a = abs(units)
    return f"{'-' if units < 0 else ''}{a // 100}.{a % 100:02d}"


def query_bboxes(region: Region) -> list[str]:
    """The snapped getTiles bbox strings, one per antimeridian half, in call order."""
    if isinstance(region, Bbox):
        min_lon, min_lat, max_lon, max_lat = region.min_lon, region.min_lat, region.max_lon, region.max_lat
    else:
        min_lon, min_lat, max_lon, max_lat = _circle_box(region.lat, region.lon, region.radius)
    lat0, lat1 = _snap_span(min_lat, max_lat, 90)
    out = []
    for lo, hi in lon_segments(min_lon, max_lon):
        lon0, lon1 = _snap_span(lo, hi, 180)
        out.append(",".join(_fixed(u) for u in (lon0, lat0, lon1, lat1)))
    return out
