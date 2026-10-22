"""A client for the atgeo gazetteer, which searches places without telling the server what you searched for."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._async import AsyncAtgeoClient
from ._sync import AtgeoClient
from ._urllib3 import Urllib3Transport
from .errors import AtgeoDecodingError, AtgeoError
from .transport import AsyncTransport, BodyError, HttpResponse, RedirectError, Transport
from .types import (
    Bbox,
    Box,
    CategoryCount,
    Circle,
    CollectionAttribution,
    CollectionInfo,
    CollectionRef,
    Extent,
    PlaceLocation,
    PlaceRecord,
    Point,
    Region,
    SearchHit,
    SearchResult,
    ServedTileset,
    ServiceDescription,
    SortMode,
    TileCounts,
    Tileset,
)

if TYPE_CHECKING:
    from ._aiohttp import AiohttpTransport as AiohttpTransport  # an explicit re-export, kept out of __all__

# AiohttpTransport is left out: a star import resolves every name listed here, and that one needs the
# async extra. `from atgeo import AiohttpTransport` still works, through __getattr__ below.
__all__ = [
    "AsyncAtgeoClient",
    "AsyncTransport",
    "AtgeoClient",
    "AtgeoDecodingError",
    "AtgeoError",
    "Bbox",
    "BodyError",
    "Box",
    "CategoryCount",
    "Circle",
    "CollectionAttribution",
    "CollectionInfo",
    "CollectionRef",
    "Extent",
    "HttpResponse",
    "PlaceLocation",
    "PlaceRecord",
    "Point",
    "RedirectError",
    "Region",
    "SearchHit",
    "SearchResult",
    "ServedTileset",
    "ServiceDescription",
    "SortMode",
    "TileCounts",
    "Tileset",
    "Transport",
    "Urllib3Transport",
]


def __getattr__(name: str) -> Any:
    # Imported on first use, so that `import atgeo` never needs the async extra.
    if name == "AiohttpTransport":
        from ._aiohttp import AiohttpTransport  # noqa: PLC0415

        return AiohttpTransport
    raise AttributeError(f"module 'atgeo' has no attribute {name!r}")
