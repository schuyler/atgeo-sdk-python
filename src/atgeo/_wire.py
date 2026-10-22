"""JSON decoding and the lenient readers (contract §1, §16).

Every response is read by hand from the parsed JSON: a missing or wrong-typed field is absent, a list
the lexicon requires is empty when it is missing or not a list, an unreadable list element is dropped,
and unknown keys are ignored. Type checks use ``type(v) is ...``, because ``bool`` is an ``int`` to
``isinstance`` and the reference does not treat ``true`` as a number.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

from ._locations import record_key
from .errors import AtgeoDecodingError, AtgeoError
from .types import (
    CategoryCount,
    CollectionAttribution,
    CollectionInfo,
    CollectionRef,
    Extent,
    ServedTileset,
    ServiceDescription,
    Tileset,
)

_T = TypeVar("_T")

# What parse_body can raise on a malformed body: JSONDecodeError and the integer-length limit are
# ValueErrors, and nesting past the recursion limit is a RecursionError. MemoryError is left to fail the call.
PARSE_ERRORS = (ValueError, RecursionError)


@dataclass(frozen=True, slots=True)
class TileRecord:
    value: Mapping[str, Any]
    uri: str | None = None
    importance: int | None = None


@dataclass(frozen=True, slots=True)
class TileList:
    tiles: list[str]
    overview: bool


def _reject_constant(name: str) -> object:
    raise ValueError(f"{name} is not JSON")


def parse_body(body: bytes) -> object:
    """Decode as ``JSON.parse(await response.text())`` would: BOM dropped, bad UTF-8 replaced, no NaN."""
    text = body.decode("utf-8-sig", "replace")
    return json.loads(text, parse_constant=_reject_constant)


def decode(body: bytes) -> object:
    """parse_body for a 2xx XRPC answer: a body that does not parse is an AtgeoDecodingError."""
    try:
        return parse_body(body)
    except PARSE_ERRORS as e:
        raise AtgeoDecodingError(f"response is not JSON: {e}") from e


def importance_of(v: object) -> int | None:
    """An integer read through a double, as the contract requires; absent unless a finite number below 2^63."""
    if not (type(v) is int or type(v) is float):  # bool, str, list, None: absent
        return None
    try:
        d = float(v)
    except OverflowError:  # an integer literal past 1.8e308, which JavaScript reads as Infinity
        return None
    if not math.isfinite(d) or abs(d) >= 2.0**63:
        return None
    return int(d)  # truncates toward zero


_ABSENT: Any = object()


def truthy(v: object = _ABSENT) -> bool:
    """JavaScript truthiness: false for absent, null, false, 0, -0 and ""; true otherwise, [] and {} included."""
    if v is _ABSENT or v is None or v is False:
        return False
    if type(v) is int or type(v) is float:
        return v != 0  # -0.0 == 0; NaN cannot arrive, parse_body rejects it
    if type(v) is str:
        return v != ""
    return True


def error_from(status: int, body: bytes) -> AtgeoError:
    """The AtgeoError for a non-2xx response; each of ``error`` and ``message`` is judged on its own."""
    try:
        parsed = parse_body(body)
    except PARSE_ERRORS:
        parsed = None
    error = parsed.get("error") if type(parsed) is dict else None
    message = parsed.get("message") if type(parsed) is dict else None
    return AtgeoError(
        error if type(error) is str else "HttpError",
        message if type(message) is str else f"HTTP {status}",
        status,
    )


def read_tile_list(body: object) -> TileList:
    tiles = body.get("tiles") if type(body) is dict else None
    if type(tiles) is not list:
        raise AtgeoDecodingError("getTiles response has no tiles list")
    assert type(body) is dict
    overview = truthy(body["overview"]) if "overview" in body else False
    return TileList([t for t in tiles if type(t) is str], overview)


def read_tile_payload(body: object) -> list[TileRecord]:
    """A tile's usable records. Raises (a failed tile) only when there is no records list at all."""
    records = body.get("records") if type(body) is dict else None
    if type(records) is not list:
        raise AtgeoDecodingError("tile has no records list")
    out = []
    for r in records:
        if type(r) is not dict:
            continue
        value = r.get("value")
        if type(value) is not dict:
            continue
        uri = r.get("uri")
        out.append(TileRecord(value, uri if type(uri) is str else None, importance_of(r.get("importance"))))
    return out


def record_keys(records: list[TileRecord]) -> frozenset[str]:
    return frozenset(k for r in records if (k := record_key(r.value, r.uri)) is not None)


def read_place(body: object) -> tuple[dict[str, Any], int | None]:
    """getPlace's record and importance; ``uri`` is not read."""
    record = body.get("record") if type(body) is dict else None
    if type(record) is not dict:
        raise AtgeoDecodingError("getPlace response has no record object")
    assert type(body) is dict
    return record, importance_of(body.get("importance"))


# The describe types.


def _str(o: Mapping[str, Any], key: str) -> str | None:
    v = o.get(key)
    return v if type(v) is str else None


def _strings(o: Mapping[str, Any], key: str) -> tuple[str, ...] | None:
    v = o.get(key)
    return tuple(s for s in v if type(s) is str) if type(v) is list else None


def _objects(o: Mapping[str, Any], key: str, read: Callable[[dict[str, Any]], _T]) -> tuple[_T, ...] | None:
    v = o.get(key)
    return tuple(read(e) for e in v if type(e) is dict) if type(v) is list else None


def _collection_ref(o: dict[str, Any]) -> CollectionRef:
    return CollectionRef(dataset=_str(o, "dataset"), collection=_str(o, "collection"))


def _served_tileset(o: dict[str, Any]) -> ServedTileset:
    return ServedTileset(slug=_str(o, "slug"), collections=_objects(o, "collections", _collection_ref) or ())


def _attribution(o: dict[str, Any]) -> CollectionAttribution:
    return CollectionAttribution(
        dataset=_str(o, "dataset"),
        collection=_str(o, "collection"),
        release=_str(o, "release"),
        source=_str(o, "source"),
        licenses=_strings(o, "licenses"),
        attribution=_str(o, "attribution"),
    )


def _category(o: dict[str, Any]) -> CategoryCount:
    return CategoryCount(value=_str(o, "value"), count=importance_of(o.get("count")))


def _collection_info(o: dict[str, Any]) -> CollectionInfo:
    return CollectionInfo(
        dataset=_str(o, "dataset"),
        collection=_str(o, "collection"),
        release=_str(o, "release"),
        source=_str(o, "source"),
        licenses=_strings(o, "licenses"),
        attribution=_str(o, "attribution"),
        record_count=importance_of(o.get("recordCount")),
        location_formats=_strings(o, "locationFormats"),
        containment_types=_strings(o, "containmentTypes"),
        categories=_objects(o, "categories", _category),
        attributes=_strings(o, "attributes"),
    )


def _extent(o: Mapping[str, Any]) -> Extent | None:
    e = o.get("extent")
    if type(e) is not dict:
        return None
    return Extent(north=_str(e, "north"), south=_str(e, "south"), east=_str(e, "east"), west=_str(e, "west"))


def read_service_description(body: object) -> ServiceDescription:
    if type(body) is not dict:
        raise AtgeoDecodingError("describeService response is not an object")
    return ServiceDescription(did=_str(body, "did"), tilesets=_objects(body, "tilesets", _served_tileset) or ())


def read_tileset(body: object) -> Tileset:
    if type(body) is not dict:
        raise AtgeoDecodingError("describeTileset response is not an object")
    return Tileset(
        slug=_str(body, "slug"),
        published_at=_str(body, "publishedAt"),
        extent=_extent(body),
        collections=_objects(body, "collections", _collection_info) or (),
        references=_objects(body, "references", _attribution),
    )
