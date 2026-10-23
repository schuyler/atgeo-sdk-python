"""The values the client takes and returns."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


@dataclass(frozen=True, slots=True)
class Circle:
    """A circle; ``radius`` in metres. Not validated here: ``search_places`` rejects a bad one."""

    lat: float
    lon: float
    radius: float


@dataclass(frozen=True, slots=True)
class Bbox:
    """A box in degrees. ``min_lon > max_lon`` crosses the antimeridian."""

    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float


Region = Circle | Bbox


class SortMode(StrEnum):
    FUSED = "fused"
    DISTANCE = "distance"
    SIMILARITY = "similarity"


@dataclass(frozen=True, slots=True, kw_only=True)
class Point:
    lat: float
    lon: float


@dataclass(frozen=True, slots=True, kw_only=True)
class Box:
    """A place's bounding box, with the lexicon's names. ``west > east`` crosses ±180."""

    south: float
    west: float
    north: float
    east: float


@dataclass(frozen=True, slots=True, kw_only=True)
class PlaceLocation:
    """What the client reads from a place's ``locations`` (contract §5)."""

    point: Point | None
    bbox: Box | None


def _name(place: Mapping[str, Any]) -> str | None:
    name = place.get("name")
    return name if type(name) is str else None


def _location(place: Mapping[str, Any]) -> PlaceLocation:
    from ._locations import locate  # noqa: PLC0415  (_locations imports this module)

    return locate(place).public()


@dataclass(frozen=True, slots=True, kw_only=True)
class TileCounts:
    fetched: int
    failed: int


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchHit:
    """One place a search found.

    ``place`` is the record value exactly as served, in a read-only :class:`types.MappingProxyType` over
    the very dict in the client's cache, not a copy. Assigning to or deleting one of its keys raises
    ``TypeError``, and ``type(hit.place) is dict`` is false. Only the top level is guarded: the lists and
    dicts inside it are the cache's own and still mutable, so copy with ``copy.deepcopy(dict(hit.place))``
    before changing any of it (``copy.deepcopy(hit.place)`` itself raises ``TypeError``, and ``dict(hit.place)``
    alone is a top-level copy, enough for ``json.dumps``).
    """

    origin: str | None
    place: Mapping[str, Any]
    importance: int | None
    distance: float
    score: float | None

    @property
    def name(self) -> str | None:
        """``place["name"]`` when that is a string."""
        return _name(self.place)

    @property
    def location(self) -> PlaceLocation:
        """The location the client measured ``distance`` to (contract §5)."""
        return _location(self.place)


class TileFailureKind(StrEnum):
    """Why a tile failed (contract §4.4)."""

    REFUSED = "refused"  # not an absolute http or https URL, or it carries credentials; no request was made
    STATUS = "status"  # a non-2xx
    DECODE = "decode"  # a 2xx whose body is not a usable tile payload
    TRANSPORT = "transport"  # the transport raised: a network error, a timeout, a body over its cap, anything


@dataclass(frozen=True, slots=True, kw_only=True)
class TileFailure:
    """One tile a search skipped.

    ``url`` is the tile URL as the tile list gave it, and ``status`` the HTTP status for ``STATUS``, else None.
    ``error`` is what went wrong, with its traceback: an :class:`AtgeoError` for ``STATUS``, whatever the
    parser raised for ``DECODE``, the transport's own exception for ``TRANSPORT``, a ``ValueError`` for
    ``REFUSED``. It is left out of ``==`` and ``hash``, since its type and message are Python's own. Holding
    on to it keeps its traceback, and through it the client, alive.
    """

    url: str
    kind: TileFailureKind
    status: int | None
    error: Exception = field(compare=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchResult:
    """A search's places. ``complete`` is False when any tile failed; ``tile_failures`` lists each, in tile URL
    order, and ``tiles.failed`` is its length."""

    places: list[SearchHit]
    complete: bool
    overview: bool
    tiles: TileCounts
    tile_failures: tuple[TileFailure, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class PlaceRecord:
    """One place by origin. ``place`` is a read-only mapping, as on :class:`SearchHit`."""

    origin: str
    place: Mapping[str, Any]
    importance: int | None

    @property
    def name(self) -> str | None:
        return _name(self.place)

    @property
    def location(self) -> PlaceLocation:
        return _location(self.place)


@dataclass(frozen=True, slots=True, kw_only=True)
class CollectionRef:
    dataset: str | None = None
    collection: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ServedTileset:
    slug: str | None = None
    collections: tuple[CollectionRef, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceDescription:
    did: str | None = None
    tilesets: tuple[ServedTileset, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class CollectionAttribution:
    dataset: str | None = None
    collection: str | None = None
    release: str | None = None
    source: str | None = None
    licenses: tuple[str, ...] | None = None
    attribution: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CategoryCount:
    value: str | None = None
    count: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CollectionInfo:
    dataset: str | None = None
    collection: str | None = None
    release: str | None = None
    source: str | None = None
    licenses: tuple[str, ...] | None = None
    attribution: str | None = None
    record_count: int | None = None
    location_formats: tuple[str, ...] | None = None
    containment_types: tuple[str, ...] | None = None
    categories: tuple[CategoryCount, ...] | None = None
    attributes: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Extent:
    north: str | None = None
    south: str | None = None
    east: str | None = None
    west: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Tileset:
    slug: str | None = None
    published_at: str | None = None
    extent: Extent | None = None
    collections: tuple[CollectionInfo, ...] = field(default=())
    references: tuple[CollectionAttribution, ...] | None = None
