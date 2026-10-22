"""The sans-I/O core both clients drive (contract §4, §9, §10).

The engine owns the caches, behind one lock that is never held across a request or a parse. It validates
and plans a search, turns responses into tile lists, tiles, places and descriptions, and assembles the
result. The drivers (_sync.py, _async.py) supply the requests and the concurrency, and call these methods
in the same order, so the two clients cannot drift apart.

A tile's outcome is a Tile or a TileFailure, and which errors make a failure is fixed by where they arise
(contract §4.4): the URL check (tile_request), the transport call (in the drivers), the status check and
decoding (load_tile, reparse). The cache insert after decoding is the client's own work and is not guarded,
so an error there fails the search. MemoryError fails the search wherever it arises.
"""

from __future__ import annotations

import logging
import operator
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
from .types import (
    PlaceRecord,
    Region,
    SearchResult,
    ServiceDescription,
    SortMode,
    TileCounts,
    TileFailure,
    TileFailureKind,
    Tileset,
)

DEFAULT_SERVER = "https://places.atgeo.org"
DEFAULT_CACHE_BUDGET_BYTES = 50 * 1024 * 1024
DEFAULT_DATASET = "overturemaps.org"
DEFAULT_COLLECTION = "place"
MAX_TILES_IN_FLIGHT = 8

# A used tile: the cache entry its records came from, and the records.
Tile = tuple[TileEntry, list[TileRecord]]
# What a tile came to: used, or failed.
Outcome = Tile | TileFailure

# The library convention: no handler of our own, so a record reaches the app's handlers, or at DEBUG nowhere.
_log = logging.getLogger("atgeo")


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


def _scalar_values(s: str) -> str:
    """Lone surrogates as U+FFFD, as URLSearchParams sends them; urlencode would raise. A pair of surrogate
    code points becomes the character it encodes, as in JavaScript's UTF-16 strings."""
    return s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def check_budget(budget: int) -> int:
    """A byte count. Checked here, since a wrong type would otherwise fail inside every tile store, failing
    every search with an error from deep in the cache rather than one naming the argument."""
    if isinstance(budget, bool):
        raise TypeError("cache_budget_bytes must be an int, not bool")
    try:
        budget = operator.index(budget)  # an int, or an int-like such as numpy.int64; not a float or a str
    except TypeError:
        raise TypeError(f"cache_budget_bytes must be an int, not {type(budget).__name__}") from None
    if budget < 0:
        raise ValueError(f"cache_budget_bytes must not be negative, not {budget}")
    return budget


class Engine:
    def __init__(self, server: str, cache_budget_bytes: int, clock: Callable[[], float]) -> None:
        self._base = check_server(server)
        cache_budget_bytes = check_budget(cache_budget_bytes)
        self._lock = threading.Lock()
        self._tiles = TileCache(cache_budget_bytes)
        self._parsed = ParsedTiles()
        self._tile_lists: TtlCache[TileList] = TtlCache(clock)
        self._service: TtlCache[ServiceDescription] = TtlCache(clock)

    # URLs and responses.

    def xrpc_url(self, nsid: str, params: Sequence[tuple[str, str]]) -> str:
        # Form encoding as URLSearchParams does it (a literal + as %2B); the trailing ? stays with no parameters.
        query = urllib.parse.urlencode([(k, _scalar_values(v)) for k, v in params])
        return prepare_url(f"{self._base}/xrpc/{nsid}?{query}")

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
    def reparse(url: str, entry: TileEntry) -> Outcome:
        """A cached tile parsed again, for a search (no lock held). It parsed once, so it cannot fail; if it
        somehow does, the tile failed to decode."""
        try:
            return entry, entry.records()
        except MemoryError:
            raise
        except Exception as e:
            return TileFailure(url=url, kind=TileFailureKind.DECODE, status=None, error=e)

    @staticmethod
    def tile_request(url: str) -> str | TileFailure:
        """The URL a tile is fetched from, or, for one that is not absolute http(s) or carries credentials,
        a refused tile, with no request made."""
        if not is_http_url(url):
            error = ValueError(f"tile URL {url!r} is not an absolute http or https URL without credentials")
            return TileFailure(url=url, kind=TileFailureKind.REFUSED, status=None, error=error)
        return prepare_url(url)

    @staticmethod
    def check_response(response: object) -> HttpResponse:
        """The transport's answer as an HttpResponse of int and bytes. An int subclass (HTTPStatus) and a
        bytes-like body (bytearray, memoryview) are taken and normalised; anything else is the transport's
        fault: called inside the drivers' transport guard, it makes a transport failure rather than an
        error in the status check or the decoder."""
        # By attribute, as body_of reads getTiles and getPlace answers: a duck-typed response is taken.
        try:
            status, body = response.status, response.body  # type: ignore[attr-defined]
        except AttributeError:
            raise TypeError(f"transport returned {type(response).__name__}, not an HttpResponse") from None
        if not isinstance(status, int) or isinstance(status, bool):
            raise TypeError(f"transport returned a status of type {type(status).__name__}, not int")
        if not isinstance(body, (bytes, bytearray, memoryview)):
            raise TypeError(f"transport returned a body of type {type(body).__name__}, not bytes")
        if type(response) is HttpResponse and type(status) is int and type(body) is bytes:
            return response
        return HttpResponse(int(status), bytes(body))

    @staticmethod
    def transport_failure(url: str, error: Exception) -> TileFailure:
        """A tile whose transport raised. The driver catches; MemoryError it lets through."""
        return TileFailure(url=url, kind=TileFailureKind.TRANSPORT, status=None, error=error)

    def load_tile(self, url: str, response: HttpResponse) -> Outcome:
        """A fetched tile, decoded and cached, or the failure its status or body made it. Only the status
        check and decoding are guarded: an error caching the tile fails the search."""
        if not 200 <= response.status < 300:
            error = error_from(response.status, response.body)
            return TileFailure(url=url, kind=TileFailureKind.STATUS, status=response.status, error=error)
        try:
            records = read_tile_payload(parse_body(response.body))
        except MemoryError:
            raise
        except Exception as e:  # whatever the parser raises: its input is the server's
            return TileFailure(url=url, kind=TileFailureKind.DECODE, status=None, error=e)
        entry = TileEntry(response.body, record_keys(records))
        with self._lock:
            self._tiles.set(url, entry)
        return entry, records

    def assemble(
        self, plan: SearchPlan, urls: Sequence[str], outcomes: Sequence[Outcome], overview: bool
    ) -> SearchResult:
        """§4.5 to §4.9 over the tiles that loaded; ``outcomes[i]`` is what ``urls[i]`` came to."""
        used: dict[str, Tile] = {}
        failures: list[TileFailure] = []
        for url, outcome in zip(urls, outcomes, strict=True):
            if isinstance(outcome, TileFailure):
                failures.append(outcome)
            else:
                used[url] = outcome
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
            complete=not failures,
            overview=overview,
            tiles=TileCounts(fetched=len(used), failed=len(failures)),
            tile_failures=tuple(failures),
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


def returned(result: SearchResult) -> SearchResult:
    """A search's result, its failed tiles logged. The drivers call it as the search returns, never sooner:
    a search that fails or is cancelled reports no failed tiles (contract §4.4)."""
    # The entry's fields and nothing else, never the text or the region (§12), and never above DEBUG (§14).
    # The tile URL reveals the coarse area searched; stdlib logging has no notion of a private field to mark.
    for f in result.tile_failures:
        # %r for the URL too: the server chose it, and a newline in it must not forge a log line.
        _log.debug("tile failed: %r (%s, status %s): %r", f.url, f.kind.value, f.status, f.error)
    return result
