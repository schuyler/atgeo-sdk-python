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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from ._cache import ParsedTiles, TileCache, TileEntry, TtlCache
from ._locations import dedupe, in_region, locate, record_key
from ._rank import collection_of, rank
from ._region import circle_box, keyword_bbox, query_bboxes, validate_keyword_region, validate_region
from ._text import match_tier, normalize, qualifies, utf16
from ._urls import is_http_url, prepare_url
from ._wire import (
    TileList,
    TilePayload,
    TileRecord,
    decode,
    error_from,
    parse_body,
    read_place,
    read_search_places,
    read_service_description,
    read_tile_list,
    read_tile_payload,
    read_tileset,
    record_keys,
)
from .errors import AtgeoError
from .transport import HttpResponse
from .types import (
    Attribution,
    Bbox,
    Disclosure,
    FallbackSearchResult,
    KeywordHit,
    KeywordSearchResult,
    NearbyHit,
    NearbySearchResult,
    PlaceRecord,
    Region,
    ServiceDescription,
    SortMode,
    TileCounts,
    TileFailure,
    TileFailureKind,
    Tileset,
    _ReadOnlyMapping,
)

DEFAULT_SERVER = "https://places.atgeo.org"
DEFAULT_CACHE_BUDGET_BYTES = 50 * 1024 * 1024
MAX_TILES_IN_FLIGHT = 8

# A used tile: the cache entry its records came from, and its records and attribution.
Tile = tuple[TileEntry, TilePayload]
# What a tile came to: used, or failed.
Outcome = Tile | TileFailure

# The library convention: no handler of our own, so a record reaches the app's handlers, or at DEBUG nowhere.
_log = logging.getLogger("atgeo")


@dataclass(frozen=True, slots=True)
class Sourced:
    """A record with the attribution of the tile it came from, so that dedupe keeps the kept copy's own."""

    record: TileRecord
    attribution: tuple[Attribution, ...]

    @property
    def value(self) -> Mapping[str, Any]:
        return self.record.value

    @property
    def uri(self) -> str | None:
        return self.record.uri


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


@dataclass(frozen=True, slots=True)
class KeywordPlan:
    url: str
    region: bool  # whether a region goes with the keyword


def union_attribution(per_tile: Iterable[tuple[Attribution, ...]]) -> tuple[Attribution, ...]:
    """The tiles' attribution in tile URL order, the first entry for each (dataset, collection) pair kept whole,
    then sorted by dataset and collection in UTF-16 code unit order (§4.10). The pair is a tuple, never a
    joined string."""
    first: dict[tuple[str, str], Attribution] = {}
    for entries in per_tile:
        for e in entries:
            first.setdefault((e.dataset, e.collection), e)
    return tuple(sorted(first.values(), key=lambda e: (utf16(e.dataset), utf16(e.collection))))


def check_server(server: str) -> str:
    """The server with trailing slashes removed. Without a scheme urllib3 would fetch it over cleartext."""
    if type(server) is not str or not is_http_url(server):
        raise ValueError(f"server must be an absolute http or https URL with a host, not {server!r}")
    return server.rstrip("/")


def _scalar_values(s: str) -> str:
    """Lone surrogates as U+FFFD, as URLSearchParams sends them; urlencode would raise. A pair of surrogate
    code points becomes the character it encodes, as in JavaScript's UTF-16 strings."""
    return s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def _status_code(status: object) -> int | None:
    """A response status as an int: an int-like or a float with no fractional part. Not a bool."""
    if isinstance(status, bool):
        return None
    if isinstance(status, float):
        return int(status) if status.is_integer() else None
    try:
        return operator.index(status)  # type: ignore[arg-type]
    except TypeError:
        return None


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

    # search_nearby.

    @staticmethod
    def check_dataset(dataset: object, method: str) -> None:
        """A dataset that is not a string is a missing one (§4.1), and a missing one is a TypeError, which
        Python raises itself for the absent argument. A str subclass is a string; "" is a dataset."""
        if not isinstance(dataset, str):
            raise TypeError(f"{method}() dataset must be a str, not {type(dataset).__name__}")

    @staticmethod
    def check_keyword(keyword: object, method: str) -> None:
        if not isinstance(keyword, str):
            raise TypeError(f"{method}() keyword must be a str, not {type(keyword).__name__}")

    def plan(
        self,
        region: Region,
        text: str | None,
        dataset: str,
        collection: str | None,
        sort: SortMode | str,
    ) -> SearchPlan:
        """Validate, in the contract's order (§3), before anything is looked up or fetched."""
        self.check_dataset(dataset, "search_nearby")
        validate_region(region)
        try:
            mode = SortMode(sort)
        except ValueError:
            raise ValueError(f"unknown sort {sort!r}; expected one of {[m.value for m in SortMode]}") from None
        query = normalize(text if text is not None else "")
        if mode is SortMode.SIMILARITY and not query:
            raise ValueError("sort 'similarity' requires text")
        ds, coll = dataset, collection
        lists = []
        for bbox in query_bboxes(region):
            # In the reference's order, and collection only when the search has one (§4.1).
            params = [("dataset", ds), *([] if coll is None else [("collection", coll)]), ("bbox", bbox)]
            lists.append(TileListRequest(f"{ds}|{coll or ''}|{bbox}", self.xrpc_url("org.atgeo.getTiles", params)))
        return SearchPlan(region, query, mode, ds, coll, lists)

    # search_by_keyword, search_nearby_then_by_keyword, autocomplete_by_keyword.

    def plan_keyword(
        self, keyword: str, dataset: str, collection: str | None, region: Bbox | None, method: str = "search_by_keyword"
    ) -> KeywordPlan:
        """Validate, in the contract's order (§3): the dataset, the keyword, then the region. A keyword that cannot
        qualify is not an error here: the server answers KeywordRequired (§16)."""
        self.check_dataset(dataset, method)
        self.check_keyword(keyword, method)
        validate_keyword_region(region)
        # dataset, collection only if given, q exactly as passed, bbox only if a region was given.
        params = [("dataset", dataset), *([] if collection is None else [("collection", collection)]), ("q", keyword)]
        if region is not None:
            params.append(("bbox", keyword_bbox(region)))
        return KeywordPlan(self.xrpc_url("org.atgeo.searchPlaces", params), region is not None)

    @staticmethod
    def keyword_result(plan: KeywordPlan, body: object) -> KeywordSearchResult:
        """A searchPlaces answer as hits in the server's order: no dedupe, no region filter, no ranking (§16)."""
        found = read_search_places(body).places
        return KeywordSearchResult(
            places=[
                KeywordHit(
                    via="keyword",
                    origin=p.origin,
                    place=_ReadOnlyMapping(p.value),
                    importance=p.importance,
                    collection=collection_of(p.value, p.attribution),
                )
                for p in found
            ],
            attribution=union_attribution(p.attribution for p in found),
            disclosed=Disclosure(keyword=True, region=1.0 if plan.region else None),
        )

    def read_keyword(self, plan: KeywordPlan, response: HttpResponse) -> KeywordSearchResult:
        return self.keyword_result(plan, self.body_of(response))

    @staticmethod
    def check_fallback(dataset: object, keyword: object, keyword_region: object) -> None:
        """The fallback's arguments before the region's, in the order of §3: the dataset, the keyword, then
        ``keyword_region``, which has no default."""
        Engine.check_dataset(dataset, "search_nearby_then_by_keyword")
        Engine.check_keyword(keyword, "search_nearby_then_by_keyword")
        if not (isinstance(keyword_region, str) and keyword_region in {"none", "degree"}):
            raise ValueError(f"keyword_region must be 'none' or 'degree', not {keyword_region!r}")

    @staticmethod
    def fallback_wants_keyword(keyword: str, nearby: NearbySearchResult) -> bool:
        """Whether the fallback asks the server (§18, steps 3 and 4): no nearby hit is tier 1, and the keyword
        can qualify."""
        query = normalize(keyword)
        if any(match_tier(query, hit.place) == 1 for hit in nearby.places):
            return False
        return qualifies(keyword)

    @staticmethod
    def keyword_region(region: Region, keyword_region: Literal["none", "degree"]) -> Bbox | None:
        """The region the fallback's keyword request carries: the nearby region as a box, or none (§18, step 5)."""
        if keyword_region == "none":
            return None
        return region if isinstance(region, Bbox) else Bbox(*circle_box(region.lat, region.lon, region.radius))

    @staticmethod
    def is_keyword_required(error: BaseException) -> bool:
        """The server's refusal of a keyword the qualifying check let through (§18 step 8, §19 step 4)."""
        return isinstance(error, AtgeoError) and error.code == "KeywordRequired"

    @staticmethod
    def fallback_result(
        nearby: NearbySearchResult, found: KeywordSearchResult | None, disclosed: Disclosure | None = None
    ) -> FallbackSearchResult:
        """The nearby result with the keyword answer merged in (§18, step 6): the keyword hits in the server's order,
        then the nearby hits, leaving out any whose origin a keyword hit has. A nearby hit with no origin is never
        left out. Without an answer, the nearby result as it is. ``disclosed`` replaces the nearby's own."""
        places: list[NearbyHit | KeywordHit] = list(nearby.places)
        attribution = nearby.attribution
        if found is not None:
            origins = {hit.origin for hit in found.places}
            places = [*found.places, *(h for h in nearby.places if h.origin is None or h.origin not in origins)]
            attribution = union_attribution([nearby.attribution, found.attribution])  # the nearby entry wins a tie
            if disclosed is None:
                near = nearby.disclosed.region
                disclosed = Disclosure(keyword=True, region=near if near is not None else found.disclosed.region)
        return FallbackSearchResult(
            places=places,
            complete=nearby.complete,
            overview=nearby.overview,
            tiles=nearby.tiles,
            disclosed=disclosed if disclosed is not None else nearby.disclosed,
            tile_failures=nearby.tile_failures,
            attribution=attribution,
        )

    @staticmethod
    def refused_keyword(nearby: NearbySearchResult, keyword_region: Literal["none", "degree"]) -> FallbackSearchResult:
        """The server refused the keyword (§18, step 8): the nearby result, but the keyword went out, with the
        region as the finest sent."""
        near = nearby.disclosed.region
        region = near if near is not None else 1.0 if keyword_region == "degree" else None
        return Engine.fallback_result(nearby, None, Disclosure(keyword=True, region=region))

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

    def memo(self, url: str, entry: TileEntry) -> TilePayload | None:
        """A cached tile's parsed payload, while the latest search's memo holds this very entry."""
        with self._lock:
            return self._parsed.get(url, entry)

    def parsed(self, url: str, entry: TileEntry) -> Tile:
        """A cached tile's records and attribution, from the memo, else parsed again (no lock held)."""
        payload = self.memo(url, entry)
        return entry, payload if payload is not None else entry.payload()

    @staticmethod
    def reparse(url: str, entry: TileEntry) -> Outcome:
        """A cached tile parsed again, for a search (no lock held). It parsed once, so it cannot fail; if it
        somehow does, the tile failed to decode."""
        try:
            return entry, entry.payload()
        except MemoryError:
            raise
        except Exception as e:
            return TileFailure(url=url, kind=TileFailureKind.DECODE, status=None, error=e)

    @staticmethod
    def tile_request(url: str) -> str | TileFailure:
        """The URL a tile is fetched from, or, for one that is not absolute http(s) or carries credentials,
        a refused tile, with no request made."""
        if not is_http_url(url):
            # The URL is the entry's own field; not repeated here, so a credential in it is not logged twice.
            error = ValueError("tile URL is not an absolute http or https URL without credentials")
            return TileFailure(url=url, kind=TileFailureKind.REFUSED, status=None, error=error)
        return prepare_url(url)

    @staticmethod
    def check_response(response: object) -> HttpResponse:
        """The transport's answer as an HttpResponse of int and bytes. A status that is an int-like (HTTPStatus,
        numpy.int64) or an integral float (200.0), and a bytes-like body (bytearray, memoryview), are taken and
        normalised; anything else is the transport's
        fault: called inside the drivers' transport guard, it makes a transport failure rather than an
        error in the status check or the decoder."""
        # By attribute, as body_of reads getTiles and getPlace answers: a duck-typed response is taken.
        try:
            status, body = response.status, response.body  # type: ignore[attr-defined]
        except AttributeError:
            raise TypeError(f"transport returned {type(response).__name__}, not an HttpResponse") from None
        code = _status_code(status)
        if code is None:
            raise TypeError(f"transport returned a status of type {type(status).__name__}, not int: {status!r}")
        if not isinstance(body, (bytes, bytearray, memoryview)):
            raise TypeError(f"transport returned a body of type {type(body).__name__}, not bytes")
        if type(response) is HttpResponse and type(status) is int and type(body) is bytes:
            return response
        return HttpResponse(code, bytes(body))

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
            payload = read_tile_payload(parse_body(response.body))
        except MemoryError:
            raise
        except Exception as e:  # whatever the parser raises: its input is the server's
            return TileFailure(url=url, kind=TileFailureKind.DECODE, status=None, error=e)
        entry = TileEntry(response.body, record_keys(payload.records))
        with self._lock:
            self._tiles.set(url, entry)
        return entry, payload

    def assemble(
        self, plan: SearchPlan, urls: Sequence[str], outcomes: Sequence[Outcome], overview: bool, sent: bool
    ) -> NearbySearchResult:
        """§4.5 to §4.9 over the tiles that loaded; ``outcomes[i]`` is what ``urls[i]`` came to. ``sent`` is whether
        the call asked the server for a tile list or attempted a tile fetch (§4.11)."""
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
        sourced = dedupe(
            Sourced(r, used[url][1].attribution or ()) for url in urls if url in used for r in used[url][1].records
        )
        if plan.collection is not None:
            prefix = f"urn:atgeo:{plan.dataset}:{plan.collection}:"
            sourced = [s for s in sourced if type(o := s.value.get("origin")) is str and o.startswith(prefix)]
        sourced = [s for s in sourced if in_region(locate(s.value), plan.region)]
        return NearbySearchResult(
            places=rank(
                [s.record for s in sourced],
                plan.region,
                plan.query,
                plan.sort,
                [s.attribution for s in sourced],
                overview=overview,
            ),
            complete=not failures,
            overview=overview,
            tiles=TileCounts(fetched=len(used), failed=len(failures)),
            tile_failures=tuple(failures),
            attribution=union_attribution(used[url][1].attribution or () for url in urls if url in used),
            disclosed=Disclosure(keyword=False, region=0.01 if sent else None),
        )

    # getPlace, describeService, describeTileset.

    def cached_place(self, origin: str) -> PlaceRecord | None:
        """The record from the lowest cached tile URL holding this key, without touching recency (§9, §10)."""
        with self._lock:
            found = self._tiles.find(origin)
        if found is None:
            return None
        _, payload = self.parsed(*found)
        for r in payload.records:
            if record_key(r.value, r.uri) == origin:
                return PlaceRecord(
                    origin=origin,
                    place=_ReadOnlyMapping(r.value),
                    importance=r.importance,
                    attribution=payload.attribution or (),
                )
        return None  # unreachable: the entry's key set came from these records

    def place_url(self, origin: str) -> str:
        return self.xrpc_url("org.atgeo.getPlace", [("uri", origin)])

    def read_place(self, origin: str, response: HttpResponse) -> PlaceRecord:
        body = read_place(self.body_of(response))
        # The caller's origin, not uri. Read-only like every other place handed out, though this dict is the
        # caller's alone: a place is the same kind of value whichever way it arrived. The attribution is as served.
        return PlaceRecord(
            origin=origin, place=_ReadOnlyMapping(body.value), importance=body.importance, attribution=body.attribution
        )

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


def returned(result: NearbySearchResult) -> NearbySearchResult:
    """A search's result, its failed tiles logged. The drivers call it as the search returns, never sooner:
    a search that fails or is cancelled reports no failed tiles (contract §4.4)."""
    # The entry's fields and nothing else, never the text or the region (§12), and never above DEBUG (§14).
    # The tile URL reveals the coarse area searched; stdlib logging has no notion of a private field to mark.
    for f in result.tile_failures:
        # %r for the URL too: the server chose it, and a newline in it must not forge a log line.
        _log.debug("tile failed: %r (%s, status %s): %r", f.url, f.kind.value, f.status, f.error)
    return result
