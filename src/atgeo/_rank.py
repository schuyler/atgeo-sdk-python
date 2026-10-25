"""Hits and their order (contract §8)."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from ._locations import Located, distance_to, locate, record_key
from ._region import center
from ._text import JW_THRESHOLD, text_score, utf16
from ._wire import TileRecord
from .types import Attribution, CollectionKey, Region, SearchHit, SortMode, _ReadOnlyMapping

_RAD = math.pi / 180


def round3(x: float) -> float:
    """``Math.round(x * 1000) / 1000``: half up, where Python's round() is half to even."""
    n = x * 1000
    f = math.floor(n)  # n - floor(n) is exact, so this avoids floor(n + 0.5)'s error at 0.49999999999999994
    return (f + 1 if n - f >= 0.5 else f) / 1000


def specificity(loc: Located) -> float:
    """Relative spherical area of a box; a point (or nothing) is 0, so it ranks before any box containing it."""
    if loc.point is not None or loc.bbox is None:
        return 0.0
    box = loc.bbox
    width = sum((hi - lo) * _RAD for lo, hi in box.segments)
    return (math.sin(box.north * _RAD) - math.sin(box.south * _RAD)) * width


def _number(x: float) -> tuple[bool, float]:
    # NaN (only out-of-range place coordinates make one) sorts after every number; -0.0 == 0.0 as in subtraction.
    return (math.isnan(x), x)


class _Hit:
    __slots__ = ("hit", "spec")

    def __init__(self, hit: SearchHit, spec: float) -> None:
        self.hit = hit
        self.spec = spec


def _by_score(h: _Hit) -> float:
    return -round3(h.hit.score or 0.0)


def _by_importance(h: _Hit) -> int:
    return -(h.hit.importance or 0)  # Python ints do not overflow


def _by_distance(h: _Hit) -> tuple[bool, float]:
    return _number(h.hit.distance)


def _by_specificity(h: _Hit) -> tuple[bool, float]:
    return _number(h.spec)


def _by_origin(h: _Hit) -> tuple[bool, bytes]:
    o = h.hit.origin
    return (o is None, utf16(o) if o is not None else b"")


_ORDERS: dict[SortMode, tuple[Callable[[_Hit], object], ...]] = {
    SortMode.FUSED: (_by_score, _by_importance, _by_distance, _by_origin),
    SortMode.DISTANCE: (_by_distance, _by_specificity, _by_importance, _by_score, _by_origin),
    SortMode.SIMILARITY: (_by_score, _by_distance, _by_origin),
}


def collection_of(value: Mapping[str, Any], attribution: Sequence[Attribution]) -> CollectionKey | None:
    """The first entry, in served order, whose prefix starts ``value.origin`` (§8): the test of §4.7, on the
    origin alone, never the uri. A new pair, not the entry."""
    origin = value.get("origin")
    if type(origin) is not str:
        return None
    for a in attribution:
        if origin.startswith(f"urn:atgeo:{a.dataset}:{a.collection}:"):
            return CollectionKey(dataset=a.dataset, collection=a.collection)
    return None


def rank(
    records: Iterable[TileRecord],
    region: Region,
    query: str,
    sort: SortMode,
    attributions: Iterable[Sequence[Attribution]] | None = None,
) -> list[SearchHit]:
    """Score, drop poor matches, and sort. ``query`` is already normalized; "" means no text.

    ``attributions``, when given, is the attribution of the tile each record came from, in step with ``records``."""
    lat, lon = center(region)
    hits: list[_Hit] = []
    sources = iter(attributions) if attributions is not None else None
    for record in records:
        source = next(sources) if sources is not None else ()
        score = text_score(query, record.value) if query else None
        if score is not None and score < JW_THRESHOLD:
            continue
        loc = locate(record.value)
        distance = distance_to(loc, lat, lon)
        assert distance is not None  # the region filter already required a location
        hit = SearchHit(
            origin=record_key(record.value, record.uri),
            place=_ReadOnlyMapping(record.value),  # a read-only view of the cache's own dict, not a copy
            importance=record.importance,
            distance=distance,
            score=score,
            collection=collection_of(record.value, source),
        )
        hits.append(_Hit(hit, specificity(loc)))
    keys = _ORDERS[sort]
    # list.sort is stable, so hits tied on every key keep their input order (contract §8).
    hits.sort(key=lambda h: tuple(k(h) for k in keys))
    return [h.hit for h in hits]
