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

    ``place`` is the record value exactly as served, and is shared with the client's cache:
    treat it as read-only, and copy it before changing it.
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


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchResult:
    places: list[SearchHit]
    complete: bool
    overview: bool
    tiles: TileCounts


@dataclass(frozen=True, slots=True, kw_only=True)
class PlaceRecord:
    """One place by origin. ``place`` is read-only, as on :class:`SearchHit`."""

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
