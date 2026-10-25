"""The values the client takes and returns."""

from __future__ import annotations

import copy
from collections.abc import ItemsView, Iterator, KeysView, Mapping, ValuesView
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
    """A box in degrees. ``west > east`` crosses the antimeridian."""

    west: float
    south: float
    east: float
    north: float


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


class _ReadOnlyMapping(Mapping[str, Any]):
    """A place's top level, read-only, over the dict the client's cache holds (not a copy).

    What a caller gets as ``hit.place``, annotated plain ``Mapping``. It is not a ``dict``: ``type(place)
    is dict`` and ``isinstance(place, dict)`` are false, there is no ``__setitem__``, ``__delitem__``,
    ``setdefault``, ``update``, ``pop``, ``popitem`` or ``clear`` (item assignment and deletion raise
    ``TypeError``, the methods ``AttributeError``), ``|`` and ``reversed()`` are unsupported, and
    ``json.dumps`` needs ``place.copy()``. Nested values are the cache's own, and still mutable.

    What it shares with a dict: ``==`` both ways, ``get``, ``in``, ``len``, iteration in served order,
    ``bool``, and ``keys()``, ``items()`` and ``values()``, which are the wrapped dict's own read-only
    views. ``place.copy()`` and ``copy.copy(place)`` are a mutable top-level ``dict``,
    ``copy.deepcopy(place)`` (so ``dataclasses.asdict`` of a hit or result) a deep, plain one, and a pickle
    round-trips to another view.
    """

    __slots__ = ("_d",)

    def __init__(self, d: Mapping[str, Any]) -> None:
        self._d = d

    def __getitem__(self, key: str) -> Any:
        return self._d[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._d)

    def __len__(self) -> int:
        return len(self._d)

    # Delegated, so these skip the Mapping mixins' per-call overhead.
    def __contains__(self, key: object) -> bool:
        return key in self._d

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)

    # The wrapped dict's own views: read-only, and no copy.
    def keys(self) -> KeysView[str]:
        return self._d.keys()

    def items(self) -> ItemsView[str, Any]:
        return self._d.items()

    def values(self) -> ValuesView[Any]:
        return self._d.values()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _ReadOnlyMapping):
            other = other._d
        if isinstance(other, dict):
            return self._d == other
        if isinstance(other, Mapping):
            return self._d == dict(other.items())
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]  # defining __eq__ resets it; a mapping is unhashable

    def __repr__(self) -> str:
        return repr(self._d)

    def copy(self) -> dict[str, Any]:
        return dict(self._d)  # a mutable top-level copy, as a dict's own copy()

    def __copy__(self) -> dict[str, Any]:
        return dict(self._d)

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        return copy.deepcopy(self._d, memo)  # a plain, mutable dict, so asdict(result) is all plain data

    def __reduce__(self) -> tuple[type[_ReadOnlyMapping], tuple[Mapping[str, Any]]]:
        return (_ReadOnlyMapping, (self._d,))


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchHit:
    """One place a search found.

    ``place`` is the record value exactly as served, as a read-only mapping over the very dict in the
    client's cache, not a copy. Assigning to or deleting one of its keys raises ``TypeError``, and
    ``type(hit.place) is dict`` is false. Only the top level is guarded: the lists and dicts inside it are
    the cache's own and still mutable, so ``copy.deepcopy(hit.place)`` before changing any of it; that gives
    a plain dict. ``hit.place.copy()`` is a fast top-level copy (a mutable ``dict``), which is all
    ``json.dumps`` needs (it does not take ``hit.place`` itself). ``dataclasses.asdict``,
    ``copy.deepcopy`` and ``pickle`` of a hit work.

    ``collection`` is the collection ``origin`` names, found in the attribution of the tile the record came from
    (contract §8): a frozen pair, or None when nothing matches.
    """

    origin: str | None
    place: Mapping[str, Any]
    importance: int | None
    distance: float
    score: float | None
    collection: CollectionKey | None = None

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
    order, and ``tiles.failed`` is its length.

    ``attribution`` credits every collection whose data the used tiles hold, whether or not any of a tile's
    records survived: one entry per (dataset, collection), the lowest tile URL's winning, sorted by dataset then
    collection. Always a tuple, empty when no tile was used. Entries are immutable, so there is nothing to copy.
    """

    places: list[SearchHit]
    complete: bool
    overview: bool
    tiles: TileCounts
    tile_failures: tuple[TileFailure, ...] = ()
    attribution: tuple[Attribution, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class PlaceRecord:
    """One place by origin. ``place`` is a read-only mapping, as on :class:`SearchHit`."""

    origin: str
    place: Mapping[str, Any]
    importance: int | None
    attribution: tuple[Attribution, ...] = ()

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
    max_tiles: int | None = None
    tilesets: tuple[ServedTileset, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class CollectionKey:
    """The collection a hit belongs to: exactly its dataset and collection (contract §8)."""

    dataset: str
    collection: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Attribution:
    """One collection's credit in a search result or a place (contract §1, §4.10): dataset and collection are
    always strings, as the readers keep an entry only when both are. Immutable, ``licenses`` a tuple."""

    dataset: str
    collection: str
    release: str | None = None
    source: str | None = None
    licenses: tuple[str, ...] | None = None
    attribution: str | None = None


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
class TilesetOverview:
    """A tileset's overview band: ``records`` is how many records it holds, when the server says."""

    records: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Tileset:
    slug: str | None = None
    published_at: str | None = None
    extent: Extent | None = None
    collections: tuple[CollectionInfo, ...] = field(default=())
    references: tuple[CollectionAttribution, ...] | None = None
    overview: TilesetOverview | None = None
