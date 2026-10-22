"""The sans-I/O core both clients drive (contract §4, §9, §10).

The engine owns the caches, behind one lock that is never held across a request or a parse. It validates
and plans a search, turns responses into tile lists, tiles, places and descriptions, and assembles the
result. The drivers (_sync.py, _async.py) supply the requests and the concurrency, and call these methods
in the same order, so the two clients cannot drift apart.
"""

from __future__ import annotations

import threading
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ._cache import ParsedTiles, TileCache, TileEntry, TtlCache
from ._locations import dedupe, in_region, locate, record_key
from ._rank import rank
from ._region import query_bboxes, validate_region
from ._text import normalize, utf16
from ._urls import is_http_url, prepare_url
from ._wire import (
    TileList,
    TileRecord,
    decode,
    error_from,
    parse_body,
    read_place,
    read_service_description,
    read_tile_list,
    read_tile_payload,
    read_tileset,
    record_keys,
)
from .transport import HttpResponse
from .types import PlaceRecord, Region, SearchResult, ServiceDescription, SortMode, TileCounts, Tileset

DEFAULT_SERVER = "https://places.atgeo.org"
DEFAULT_CACHE_BUDGET_BYTES = 50 * 1024 * 1024
DEFAULT_DATASET = "overturemaps.org"
DEFAULT_COLLECTION = "place"
MAX_TILES_IN_FLIGHT = 8

# A used tile: the cache entry its records came from, and the records.
Tile = tuple[TileEntry, list[TileRecord]]


@dataclass(frozen=True, slots=True)
class TileListRequest:
    key: str  # the tile-list cache key, "{dataset}|{collection or empty}|{bbox}"
    url: str


@dataclass(frozen=True, slots=True)
class SearchPlan:
    region: Region
    query: str  # normalized; "" means no text
    sort: SortMode
    dataset: str
    collection: str | None
    lists: list[TileListRequest]  # one per antimeridian half, in call order


def check_server(server: str) -> str:
    """The server with trailing slashes removed. Without a scheme urllib3 would fetch it over cleartext."""
    if type(server) is not str or not is_http_url(server):
        raise ValueError(f"server must be an absolute http or https URL with a host, not {server!r}")
    return server.rstrip("/")


class Engine:
    def __init__(self, server: str, cache_budget_bytes: int, clock: Callable[[], float]) -> None:
        self._base = check_server(server)
        self._lock = threading.Lock()
        self._tiles = TileCache(cache_budget_bytes)
        self._parsed = ParsedTiles()
        self._tile_lists: TtlCache[TileList] = TtlCache(clock)
        self._service: TtlCache[ServiceDescription] = TtlCache(clock)

    # URLs and responses.

    def xrpc_url(self, nsid: str, params: Sequence[tuple[str, str]]) -> str:
        # Form encoding as URLSearchParams does it (a literal + as %2B); the trailing ? stays with no parameters.
        return prepare_url(f"{self._base}/xrpc/{nsid}?{urllib.parse.urlencode(params)}")

    @staticmethod
    def body_of(response: HttpResponse) -> object:
        """The decoded body of an XRPC answer; a non-2xx is AtgeoError, read before the status is judged."""
        if not 200 <= response.status < 300:
            raise error_from(response.status, response.body)
        return decode(response.body)

    # searchPlaces.

    def plan(
        self,
        region: Region,
        text: str | None,
        dataset: str | None,
        collection: str | None,
        sort: SortMode | str,
    ) -> SearchPlan:
        """Validate, in the contract's order (§3), before anything is looked up or fetched."""
        validate_region(region)
        try:
            mode = SortMode(sort)
        except ValueError:
            raise ValueError(f"unknown sort {sort!r}; expected one of {[m.value for m in SortMode]}") from None
        query = normalize(text if text is not None else "")
        if mode is SortMode.SIMILARITY and not query:
            raise ValueError("sort 'similarity' requires text")
        # Absent only when None, as the reference's ?? has it: dataset="" is a dataset.
        ds = dataset if dataset is not None else DEFAULT_DATASET
        coll = collection if collection is not None else (DEFAULT_COLLECTION if dataset is None else None)
        lists = []
        for bbox in query_bboxes(region):
            # In the reference's order, and collection only when the search has one (§4.1).
            params = [("dataset", ds), *([] if coll is None else [("collection", coll)]), ("bbox", bbox)]
            lists.append(TileListRequest(f"{ds}|{coll or ''}|{bbox}", self.xrpc_url("org.atgeo.getTiles", params)))
        return SearchPlan(region, query, mode, ds, coll, lists)

    def cached_tile_list(self, key: str) -> TileList | None:
        with self._lock:
            return self._tile_lists.get(key)

    def store_tile_list(self, key: str, response: HttpResponse) -> TileList:
        """A getTiles answer to a tile list, cached for an hour. Raises, caching nothing, on an unusable answer."""
        tile_list = read_tile_list(self.body_of(response))
        with self._lock:
            self._tile_lists.set(key, tile_list)
        return tile_list

    @staticmethod
    def tile_urls(lists: Sequence[TileList]) -> list[str]:
        """The union of the lists' tiles, deduplicated and sorted by UTF-16 code units (§4.3)."""
        return sorted({url for tile_list in lists for url in tile_list.tiles}, key=utf16)

    def cached_tile(self, url: str) -> TileEntry | None:
        """The cached tile for a URL, marked most recently used, as a worker reaches it."""
        with self._lock:
            return self._tiles.get(url)

    def memo(self, url: str, entry: TileEntry) -> list[TileRecord] | None:
        """A cached tile's parsed records, while the latest search's memo holds this very entry."""
        with self._lock:
            return self._parsed.get(url, entry)

    def parsed(self, url: str, entry: TileEntry) -> Tile:
        """A cached tile's records, from the memo, else parsed again (no lock held)."""
        records = self.memo(url, entry)
        return entry, records if records is not None else entry.records()

    @staticmethod
    def tile_request(url: str) -> str:
        """The URL a tile is fetched from. A tile URL that is not absolute http(s) is a failed tile."""
        if not is_http_url(url):
            raise ValueError(f"tile URL {url!r} is not an absolute http or https URL")
        return prepare_url(url)

    def store_tile(self, url: str, response: HttpResponse) -> Tile:
        """Parse a fetched tile and cache it. Raises (a failed tile) for a non-2xx or an unusable body."""
        if not 200 <= response.status < 300:
            raise error_from(response.status, response.body)
        records = read_tile_payload(parse_body(response.body))
        entry = TileEntry(response.body, record_keys(records))
        with self._lock:
            self._tiles.set(url, entry)
        return entry, records

    def assemble(
        self, plan: SearchPlan, urls: Sequence[str], tiles: Sequence[Tile | None], overview: bool
    ) -> SearchResult:
        """§4.5 to §4.9 over the tiles that loaded; ``tiles[i]`` is None for a failed ``urls[i]``."""
        used = {url: tile for url, tile in zip(urls, tiles, strict=True) if tile is not None}
        with self._lock:
            self._parsed.replace(used)
        # Dedupe picks a winner before the region filter runs: if copies sharing an origin differ in location
        # and the winner lies outside the region, the place is dropped though another copy is inside (§4.6).
        records = dedupe(r for url in urls if url in used for r in used[url][1])
        if plan.collection is not None:
            prefix = f"urn:atgeo:{plan.dataset}:{plan.collection}:"
            records = [r for r in records if type(o := r.value.get("origin")) is str and o.startswith(prefix)]
        records = [r for r in records if in_region(locate(r.value), plan.region)]
        return SearchResult(
            places=rank(records, plan.region, plan.query, plan.sort),
            complete=len(used) == len(urls),
            overview=overview,
            tiles=TileCounts(fetched=len(used), failed=len(urls) - len(used)),
        )

    # getPlace, describeService, describeTileset.

    def cached_place(self, origin: str) -> PlaceRecord | None:
        """The record from the lowest cached tile URL holding this key, without touching recency (§9, §10)."""
        with self._lock:
            found = self._tiles.find(origin)
        if found is None:
            return None
        _, records = self.parsed(*found)
        for r in records:
            if record_key(r.value, r.uri) == origin:
                return PlaceRecord(origin=origin, place=r.value, importance=r.importance)
        return None  # unreachable: the entry's key set came from these records

    def place_url(self, origin: str) -> str:
        return self.xrpc_url("org.atgeo.getPlace", [("uri", origin)])

    def read_place(self, origin: str, response: HttpResponse) -> PlaceRecord:
        record, importance = read_place(self.body_of(response))
        return PlaceRecord(origin=origin, place=record, importance=importance)  # the caller's origin, not uri

    def cached_service(self) -> ServiceDescription | None:
        with self._lock:
            return self._service.get("")

    def service_url(self) -> str:
        return self.xrpc_url("org.atgeo.describeService", [])

    def store_service(self, response: HttpResponse) -> ServiceDescription:
        service = read_service_description(self.body_of(response))
        with self._lock:
            self._service.set("", service)
        return service

    def tileset_url(self, slug: str) -> str:
        return self.xrpc_url("org.atgeo.describeTileset", [("slug", slug)])

    def read_tileset(self, response: HttpResponse) -> Tileset:
        return read_tileset(self.body_of(response))


def is_tile_failure(e: BaseException) -> bool:
    """Whether an exception from a tile fetch counts as a failed tile rather than failing the search.

    Any Exception does, except MemoryError, which fails the search, as Kotlin's tile loop does not catch
    Throwable. Cancellation (a BaseException) never reaches here.
    """
    return isinstance(e, Exception) and not isinstance(e, MemoryError)
