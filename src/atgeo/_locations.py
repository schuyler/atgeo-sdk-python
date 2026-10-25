"""Reading a place's location, distance and containment, record keys and dedupe (contract §4.6, §5, §6)."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from ._region import (
    LonSegment,
    haversine,
    lon_in_segments,
    lon_segments,
    lon_segments_overlap,
)
from .types import Bbox, Box, Circle, PlaceLocation, Point, Region

GEO = "community.lexicon.location.geo"
BBOX = "community.lexicon.location.bbox"

# A decimal number and nothing else. [0-9], not \d, which matches every Unicode digit.
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def coordinate(v: object) -> float | None:
    """A JSON string holding a decimal number, or a JSON number; finite, or None (contract §5)."""
    if type(v) is str:
        if not _NUMBER.fullmatch(v):
            return None
        d = float(v)
    elif type(v) is int or type(v) is float:
        try:
            d = float(v)
        except OverflowError:  # an integer literal past 1.8e308
            return None
    else:
        return None  # a list (contract §16), bool, None, dict
    return d if math.isfinite(d) else None


@dataclass(frozen=True, slots=True)
class LocatedBox:
    south: float
    north: float
    west: float
    east: float
    segments: list[LonSegment]


@dataclass(frozen=True, slots=True)
class Located:
    point: tuple[float, float] | None  # (lat, lon)
    bbox: LocatedBox | None

    def public(self) -> PlaceLocation:
        return PlaceLocation(
            point=Point(lat=self.point[0], lon=self.point[1]) if self.point else None,
            bbox=Box(south=self.bbox.south, west=self.bbox.west, north=self.bbox.north, east=self.bbox.east)
            if self.bbox
            else None,
        )


def locate(value: Mapping[str, Any]) -> Located:
    point: tuple[float, float] | None = None
    box: LocatedBox | None = None
    locations = value.get("locations")
    if type(locations) is not list:
        return Located(None, None)
    for loc in locations:
        if type(loc) is not dict:  # null and other non-objects are skipped (contract §16)
            continue
        kind = loc.get("$type")
        if kind == GEO and point is None:
            lat, lon = coordinate(loc.get("latitude")), coordinate(loc.get("longitude"))
            if lat is not None and lon is not None:
                point = (lat, lon)
        elif kind == BBOX and box is None:
            s, n, w, e = (coordinate(loc.get(k)) for k in ("south", "north", "west", "east"))
            if s is not None and n is not None and w is not None and e is not None:
                box = LocatedBox(s, n, w, e, lon_segments(w, e))
        if point is not None and box is not None:
            break
    return Located(point, box)


def record_key(value: Mapping[str, Any], uri: str | None) -> str | None:
    origin = value.get("origin")
    return origin if type(origin) is str else uri


class _Keyed(Protocol):
    @property
    def value(self) -> Mapping[str, Any]: ...
    @property
    def uri(self) -> str | None: ...


_K = TypeVar("_K", bound=_Keyed)


def dedupe(records: Iterable[_K]) -> list[_K]:
    """The first record for each key, in order; records with no key always pass."""
    seen: set[str] = set()
    out: list[_K] = []
    for r in records:
        key = record_key(r.value, r.uri)
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        out.append(r)
    return out


def _gap(a: float, b: float) -> float:
    d = abs(a - b)
    if not math.isfinite(d):  # math.fmod raises on infinity, where JavaScript's % gives NaN
        return math.nan
    d = math.fmod(d, 360)
    return min(d, 360 - d)


def _nearest_lon(lon: float, segments: list[LonSegment]) -> float:
    if lon_in_segments(lon, segments):
        return lon
    ends = [e for seg in segments for e in seg]
    best = ends[0]
    for e in ends[1:]:
        if _gap(e, lon) < _gap(best, lon):  # strict: on a tie the first endpoint wins
            best = e
    return best


def _covers(south: float, north: float, segments: list[LonSegment], lat: float, lon: float) -> bool:
    # ±180 are one meridian, and every meridian meets at a pole.
    if lat < south or lat > north:
        return False
    return abs(lat) == 90 or lon_in_segments(lon, segments) or (abs(lon) == 180 and lon_in_segments(-lon, segments))


def _meets(box: LocatedBox, south: float, north: float, segments: list[LonSegment]) -> bool:
    lo = max(box.south, south)
    hi = min(box.north, north)
    if lo > hi:
        return False
    twins: list[LonSegment] = []
    for seg in box.segments:
        twins.append(seg)
        if seg[1] == 180:
            twins.append((-180.0, -180.0))
        if seg[0] == -180:
            twins.append((180.0, 180.0))
    return hi == 90 or lo == -90 or lon_segments_overlap(twins, segments)


def distance_to(loc: Located, lat: float, lon: float) -> float | None:
    """Metres from (lat, lon) to the nearest point of the place; None without a location."""
    point, box = loc.point, loc.bbox
    # Containment first, so that it is exactly 0 rather than round-trip noise.
    if point is not None:
        contained = _covers(point[0], point[0], [(point[1], point[1])], lat, lon)
    else:
        contained = box is not None and _covers(box.south, box.north, box.segments, lat, lon)
    if contained:
        return 0.0
    if point is not None:
        return haversine(lat, lon, point[0], point[1])
    if box is None:
        return None
    lon_star = _nearest_lon(lon, box.segments)
    phi0 = lat * math.pi / 180
    d_lon = _gap(lon_star, lon) * math.pi / 180
    phi_c = math.atan2(math.sin(phi0), math.cos(phi0) * math.cos(d_lon)) * 180 / math.pi
    candidates = (
        haversine(lat, lon, min(max(phi_c, box.south), box.north), lon_star),
        haversine(lat, lon, box.south, lon_star),
        haversine(lat, lon, box.north, lon_star),
    )
    # Math.min is NaN if any argument is; min() skips a NaN that is not first.
    return math.nan if any(math.isnan(d) for d in candidates) else min(candidates)


def in_region(loc: Located, region: Region) -> bool:
    if isinstance(region, Bbox):
        segments = lon_segments(region.west, region.east)
        if loc.point is not None:
            return _covers(region.south, region.north, segments, loc.point[0], loc.point[1])
        return loc.bbox is not None and _meets(loc.bbox, region.south, region.north, segments)
    assert isinstance(region, Circle)
    d = distance_to(loc, region.lat, region.lon)
    return d is not None and d <= region.radius
