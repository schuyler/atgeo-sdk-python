# atgeo for Python

A Python client for the atgeo gazetteer served by [garganorn](https://github.com/schuyler/garganorn), which searches places without telling the server what you searched for.

It's a port of [@atgeo/client](https://github.com/schuyler/atgeo-sdk-js) and returns the same places in the same order for the same server answers. There's a blocking client for scripts, notebooks and WSGI apps, and an asyncio client for async servers, with the same methods and results. It needs Python 3.11 or later, and installs urllib3 and truststore; the async client needs aiohttp as well.

## Synopsis

```python
from atgeo import AtgeoClient, Circle

with AtgeoClient() as atgeo:
    result = atgeo.search_places(
        Circle(lat=39.9526, lon=-75.1652, radius=1000),  # metres
        dataset="overturemaps.org",
        collection="place",
        text="Starbux",
    )

    for hit in result.places:
        print(hit.name, round(hit.distance), f"{hit.score:.2f}", hit.origin)

    for credit in result.attribution:  # whose data these places are, and on what terms
        print(credit.dataset, credit.collection, credit.licenses)
```

Misspellings are fine; matching is fuzzy (names whose words start with every query word rank first), and that finds the Starbucks around Philadelphia's City Hall. The public server at places.atgeo.org has a limited dataset for now, mostly the east coast, so a search elsewhere may come back empty. `dataset` is required; there's no default, so a search always says whose places it wants. If `result.complete` is `False`, some tiles failed to load and the results are partial; `result.tile_failures` says which, and why. If `result.overview` is `True`, the region was too big to search in full and you got only its most important places.

The async version is the same with `await`:

```python
import asyncio
from atgeo import AsyncAtgeoClient, Circle


async def main() -> None:
    async with AsyncAtgeoClient() as atgeo:
        result = await atgeo.search_places(
            Circle(39.9526, -75.1652, 1000), dataset="overturemaps.org", collection="place", text="Starbux"
        )
        print(len(result.places))


asyncio.run(main())
```

## Installation

This isn't on PyPI yet; it stays unpublished until beta testing ends. Install it from GitHub, pinned to a commit:

```sh
pip install "atgeo @ git+https://github.com/schuyler/atgeo-sdk-python@<commit-sha>"
pip install "atgeo[async] @ git+https://github.com/schuyler/atgeo-sdk-python@<commit-sha>"   # with the async client
```

`uv add` takes the same URL. Pin a full commit SHA rather than a branch: the API is still changing, and breaking changes land on `main`.

The plain install brings urllib3 and truststore and nothing else. The `async` extra adds aiohttp and the eight or nine packages it needs, several of them compiled, which is why it's an extra. `import atgeo` never imports aiohttp; `AsyncAtgeoClient()` without it raises `ImportError` telling you to install `atgeo[async]`.

## Constructing a client

```python
atgeo = AtgeoClient(
    "http://localhost:8000",  # default: https://places.atgeo.org
    transport=my_transport,  # default: Urllib3Transport()
    cache_budget_bytes=10 * 1024 * 1024,  # default: 50 MiB
)
```

Make one per server and share it: `AtgeoClient` is safe to call from many threads at once, `AsyncAtgeoClient` from many tasks, and their caches are what make repeat searches free. Both default transports pool connections, so use the client in a `with` (`async with`) block or call `close()` (`await aclose()`) when you're done. A client closes only a transport it made. An `AsyncAtgeoClient` that has closed its own transport raises `RuntimeError` on any later call, rather than opening an aiohttp session that nothing would close.

`cache_budget_bytes` must be a non-negative integer (an `int`, or an int-like such as `numpy.int64`); anything else is a `TypeError` or `ValueError` when the client is made. `server` must be an absolute `http` or `https` URL without credentials in it; anything else is a `ValueError`. Without the check, urllib3 would fetch `places.atgeo.org/...` over cleartext HTTP with nothing but a warning, and a `user:password@` would be dropped by one default transport and sent by the other. A tile URL with credentials is a failed tile, as it is under the reference's `fetch`.

The async client's default transport holds an aiohttp session, and aiohttp sessions belong to the event loop they were first used in. So a client using it belongs to the loop of its first call, and using it from another raises a `RuntimeError` that says so. A script that calls `asyncio.run` more than once should make a client inside each.

### Transports

A transport is one method, `get(url) -> HttpResponse(status, body)`, blocking for `AtgeoClient` and a coroutine for `AsyncAtgeoClient`. It returns a response for every status; the client turns a non-2xx into an error itself.

The defaults, `Urllib3Transport` and `AiohttpTransport`, both:

- follow redirects themselves, one hop at a time, without reading a redirect's body, refusing anything but `http` and `https` and giving up after ten;
- ask for gzip and decode it themselves, refusing a body larger than `max_body_bytes` (32 MiB) once decoded, because the server chooses the tile URLs and a small gzip body can decode to gigabytes;
- send each request once, keep no cookies and no cache, and never read `~/.netrc`;
- take proxies from `http_proxy`, `https_proxy`, `all_proxy` and `no_proxy`, read when the transport is made (`no_proxy` is checked on every request), or on macOS and Windows from the system's proxy settings when those are unset, since that's what `urllib.request.getproxies()` does. SOCKS proxies aren't supported and are refused when the transport is made, and CIDR entries in `no_proxy` (`10.0.0.0/8`) aren't understood, because `urllib.request.proxy_bypass` doesn't understand them;
- verify TLS against the operating system's certificate store, through truststore. Pass `ssl_context=` for your own certificate authority.

Their timeouts are per socket operation: 10 seconds to connect, 30 seconds between bytes. `Urllib3Transport` also gives the whole call 120 seconds (`total_timeout`), redirects and headers included, because a blocking call has no other way to give up on a server that sends one byte every 29 seconds. It raises `TimeoutError` when it runs out.

If your app already has a configured `requests.Session` or uses httpx, copy [`examples/requests_transport.py`](examples/requests_transport.py) or [`examples/httpx_transport.py`](examples/httpx_transport.py). They're short, CI runs the transport tests against both, and they do what the defaults do, except that `total_timeout` doesn't cover headers trickled a byte at a time. That takes more care than it looks: `requests.Session.send` reads a redirect's whole body into memory even with `allow_redirects=False`, and would send the session's own auth, headers and cookies to every host the server names, so the requests adapter borrows only the session's adapters (its TLS settings and connection pools) and leaves the session itself alone. Whatever transport you use, don't give it a disk cache, or the client's promise to keep nothing on disk goes with it.

## API

The methods are the same on both clients; on `AsyncAtgeoClient` they're coroutines.

### `search_places(region, *, dataset, text=None, collection=None, sort="fused") -> SearchResult`

| Argument | Meaning |
|---|---|
| `region` | Required. `Circle(lat, lon, radius)` with the radius in metres, or `Bbox(west, south, east, north)` in degrees. A circle is latitude first and a box longitude first (GeoJSON's order), so name the arguments. A bbox with `west > east` crosses the antimeridian. |
| `dataset` | Required, such as `overturemaps.org`. There is no default. |
| `text` | Optional search text, matched fuzzily against each place's `name` and its `variants[].name` after lowercasing and stripping accents and extra whitespace. Places scoring below 0.6 are dropped. The rest rank in two tiers, word-prefix matches first; see below. |
| `collection` | Optional, such as `way`. Without it you search every collection of the dataset together. |
| `sort` | `SortMode.FUSED` (default), `DISTANCE` or `SIMILARITY`, or the same as strings. |

Everything after `region` is keyword-only, so `search_places(region, "Starbux")` is an error rather than a guess.

A missing `dataset`, or one that isn't a `str` (`None`, a number, bytes), is a `TypeError` raised before any request and before the region is looked at; a `collection` without a `dataset` is the same error. An empty string is a dataset, and goes to the server as given. On the async client a missing argument fails at the call and a wrong-typed one when you `await` it.

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class SearchResult:
    places: list[SearchHit]
    complete: bool  # False if any tile failed to load
    overview: bool  # True if the server answered with its coarse overview
    tiles: TileCounts  # fetched and failed
    tile_failures: tuple[TileFailure, ...]  # each failed tile, in tile URL order; () when complete
    attribution: tuple[Attribution, ...]  # who to credit for the tiles used, one entry per collection; () if none


@dataclass(frozen=True, slots=True, kw_only=True)
class Attribution:
    dataset: str
    collection: str
    release: str | None  # the upstream's version string
    source: str | None  # URL of the upstream dataset
    licenses: tuple[str, ...] | None  # URLs of the licence texts
    attribution: str | None  # URL of the upstream's attribution page


@dataclass(frozen=True, slots=True, kw_only=True)
class CollectionKey:
    dataset: str
    collection: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TileFailure:
    url: str  # the tile URL as the server's tile list gave it
    kind: TileFailureKind  # REFUSED, STATUS, DECODE or TRANSPORT
    status: int | None  # the HTTP status for STATUS, else None
    error: Exception  # what was raised, traceback and all; not part of ==


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchHit:
    origin: str | None  # place["origin"], else the tile record's uri, else None
    place: Mapping[str, Any]  # the record value, exactly as served; a read-only view of the cached dict
    importance: int | None  # relative to its tileset; None if it has none
    distance: float  # metres from the region's centre to the nearest point of the place; 0 if it contains the centre
    score: float | None  # 0 to 1 text similarity; None without text
    collection: CollectionKey | None  # the collection origin names, per the tile's attribution; None if none matches

    name: str | None  # property: place["name"] if it's a string
    location: PlaceLocation  # property: point and/or box, parsed as the client parsed them for distance
```

`result.attribution` covers every tile the search used, including tiles whose records were all filtered out, and the reference collections a tileset lists for places that mention them (a hit's containing division, say), so show it wherever you show the places. It has one entry per `(dataset, collection)`, taken from the lowest tile URL when tiles disagree, sorted by dataset and then collection, and it's empty when no tile was used. A failed tile adds nothing. Entries are frozen and `licenses` is a tuple, so there's nothing to copy and nothing you can change in the cache. `hit.collection` is the first entry, in the tile's served order, whose `urn:atgeo:{dataset}:{collection}:` prefix starts the hit's `origin`, as a new `CollectionKey` with just those two fields. It's `None` when nothing matches, when the tile has no attribution, or when the hit is keyed by its `uri` instead of an `origin`.

`error` is a live exception, so `dataclasses.asdict(result)` copies it along with everything else, and `json.dumps` of that fails once a tile has failed. To serialize a result, take `url`, `kind` and `status` from each entry of `tile_failures` and leave `error` out, as `==` does.

`place` is the gazetteer's record as `json.loads` made it, every key in served order and every integer exact, behind a read-only mapping. **Don't change what's inside it.** The client keeps the latest search's parsed tiles so that searching the same region with new text parses nothing, and `place` is a view of the dict in that memo, not a copy, so a change would show up in the next search. Copying every place by default would cost three times what parsing does, so the view is what you get. `hit.place["name"] = "x"` and `del hit.place["name"]` raise `TypeError` at the offending line, `hit.place.setdefault(...)`, `.update(...)` and `.pop(...)` raise `AttributeError` (the view has no such methods), and a type checker objects too, since the field is annotated `Mapping`. `get_place` returns the same kind of view, whether the place came from a cached tile or from the server.

Only the top level is guarded. The lists and dicts inside `place` are still the cache's own, so `hit.place["locations"].append(...)` goes through. To change anything, copy first with `copy.deepcopy(hit.place)`, which gives you a plain, mutable `dict`. `hit.place.copy()` (the fast way to a mutable dict), `dict(hit.place)` and `copy.copy(hit.place)` copy only the top level, so their nested lists and dicts are still the cache's.

The view is not a `dict`, and these are the differences:

- `type(hit.place) is dict` and `isinstance(hit.place, dict)` are false (`isinstance(hit.place, Mapping)` is true).
- It has no `__setitem__`, `__delitem__`, `setdefault`, `update`, `pop`, `popitem` or `clear`. Item assignment and deletion raise `TypeError`, the methods `AttributeError`.
- `hit.place | other` and `reversed(hit.place)` aren't supported.
- `json.dumps(hit.place)` raises `TypeError: Object of type _ReadOnlyMapping is not JSON serializable`. Write `json.dumps(hit.place.copy())`; the nested values are plain dicts and lists, so a top-level copy is all `json` needs.

What it shares with a dict: `==` in both directions (`hit.place == some_dict` and `some_dict == hit.place` compare contents), `get`, `in`, `len`, `bool`, iteration in served order, and `keys()`, `items()` and `values()` (the wrapped dict's own read-only views). `hit.place.copy()` and `copy.copy(hit.place)` give a mutable top-level `dict`, and `copy.deepcopy(hit.place)` a deep, plain one. `pickle` and `dataclasses.asdict` work on a hit, a place record or a whole result, so a result crosses a process boundary and `json.dumps(dataclasses.asdict(result))` writes its places (the `error` caveat above still applies). `asdict` and `deepcopy` turn each place into a plain dict; a pickle round-trips to the view.

`name` and `location` save you the two reads every app needs: `location.point` is the first usable `geo` entry of `place["locations"]`, `location.bbox` the first usable `bbox` entry (`west > east` crosses the antimeridian), coordinates already parsed from the strings they arrive as.

With `text`, places that clear 0.6 rank in two tiers, whatever the sort mode. A place with a name (its own or a variant) that has every word of the query as the start of one of its words comes before one that matches only fuzzily. Apostrophes are ignored and other punctuation splits words, so `mcdonalds` finds `McDonald's` and `st louis` finds `St. Louis`. Within a tier, the sort mode decides. On an overview answer (`result.overview` is `True`), a search with `text` keeps only the first tier: fuzzy matches are dropped, since the coarse set can't hold the place you meant.

`FUSED` orders by tier, then similarity, breaking near-ties by importance, then distance. `DISTANCE` orders by tier, then nearest first, then the smaller place, then importance, then similarity. `SIMILARITY` orders by tier, then similarity, then distance, and needs `text`. Scores are compared to three decimals and remaining ties break on `origin`, so the order is deterministic.

The [JavaScript README](https://github.com/schuyler/atgeo-sdk-js#readme) has the long version: overview answers, places that appear in several tiles, and how ties are broken. All of it applies here unchanged.

The result types are frozen, keyword-only dataclasses. You can construct them for your own tests, and fields added later won't break code that does.

### `get_place(origin) -> PlaceRecord`

Returns `PlaceRecord(origin, place, importance, attribution)` for the `origin` of a hit, with the same `name`, `location` and read-only `place`. If a cached tile holds the place, no request is made; otherwise the client asks the server directly. `attribution` is a tuple of `Attribution`, in the order it was served and not deduplicated or sorted: from the cache, the one the lowest-URL tile holding the place carried (empty if it has none), and from the server, the answer's own. It's empty, never `None`. The server's `getPlace` answer has to carry a `value`; the older `record`-only shape raises `AtgeoDecodingError`.

### `describe_service() -> ServiceDescription`

The server's DID, its `max_tiles` (the most regular tiles it will list before falling back to overview tiles; informational, since tile zooms adapt) and the tilesets it serves. Cached for an hour.

### `describe_tileset(slug) -> Tileset`

A tileset's collections, attribution and extent, and its `overview` (`TilesetOverview(records=...)`, `None` if it has no overview band). Not cached. The describe types are read leniently: a field that is missing or of the wrong type comes back `None`, never as an exception. Their field names are snake_case (`record_count`, `published_at`) and their lists are tuples.

### Errors

- `AtgeoError` has the server's error `code` (`PlaceNotFound`, `BboxTooLarge`, `DatasetNotFound` and so on), or `HttpError` when the server didn't send one, a `message`, and the HTTP `status`. Use `status` to tell a request the server refused (4xx) from one it failed to answer (5xx); codes are an open list and new ones may appear.
- `search_places` raises `TypeError` for a missing or non-string `dataset`, checked first, and then for a region that isn't a `Circle` or a `Bbox`; and `ValueError` for an invalid region, an unknown `sort` or `SIMILARITY` without text. All of them come before any request.
- A failed `getTiles` fails the search. A failed tile does not: it's skipped, listed in `tile_failures` and counted in `tiles.failed`, and `complete` is `False`. A tile's `kind` says where it went wrong: `REFUSED` is a tile URL that isn't absolute `http` or `https` or has credentials in it, and no request was made; `STATUS` a non-2xx, with its `status` and the `AtgeoError`; `DECODE` a 2xx whose body isn't a tile, with whatever the parser raised; `TRANSPORT` anything the transport raised, a timeout, a dropped connection, a body over the cap, or a bug in a transport of your own, which is why that one is a failed tile and not an exception. The kind, URL and status are the same in every atgeo SDK; `error` is Python's own, so two results compare equal without it.
- An error in the client's own work after a tile has decoded, such as storing it in the cache, fails the search rather than passing for a bad tile. So does `MemoryError`, wherever it comes from.
- Each failed tile is also logged on the `atgeo` logger at `DEBUG`, with its URL, kind, status and error and nothing else, never your search text or region. The tile URL does say roughly where you searched, about as precisely as the server already knows, so treat debug logs that way. The library adds no handler, as libraries shouldn't, so you see nothing until you ask: `logging.getLogger("atgeo").setLevel(logging.DEBUG)` with a handler configured, or `logging.basicConfig(level=logging.DEBUG)`. It never logs above `DEBUG`, so a search on every keystroke over a bad connection won't fill your logs, and it sends nothing anywhere.
- A 2xx body that isn't JSON, or has nothing usable in it, raises `AtgeoDecodingError`, with the parser's error as its `__cause__`. It isn't a `ValueError`, so an `except ValueError` meant for a bad region won't swallow it. Retrying rarely helps; the server sends the same body again.
- When no answer arrives at all, you get whatever the transport raised. Catch `(urllib3.exceptions.HTTPError, OSError)` with `Urllib3Transport` and `(aiohttp.ClientError, OSError)` with `AiohttpTransport`. Before a response, that's the library's own exception, or `TimeoutError` when `total_timeout` runs out (urllib3's `MaxRetryError`, with the real cause in `.reason`, isn't an `OSError`); after one starts, a body that doesn't arrive whole is `atgeo.BodyError`, and a bad or endless redirect is `atgeo.RedirectError`, both `OSError`s. With the httpx adapter, add `httpx.HTTPError`.
- `AsyncAtgeoClient` with its default transport raises `RuntimeError` when called from an event loop other than its first call's, before making any request.
- Cancelling the task running an async call raises `asyncio.CancelledError`. A blocking search can be interrupted with Ctrl-C: it stops claiming tiles at once and raises `KeyboardInterrupt`, though the requests already in flight run on, on daemon threads, until they finish or time out.

## Privacy

Your search text never leaves the device. Neither does your precise location.

A search asks the server which tiles cover a bounding box snapped outward to a 0.01° grid (roughly 1 km), downloads those tiles, and does the matching, filtering and ranking locally. The server learns the cell, the dataset and collection, and which tiles you fetched that weren't already cached. It also sees your IP address and the transport's own headers. The default transports send `User-Agent: atgeo-client-python` instead of one naming the urllib3, aiohttp and Python versions, and no `Accept-Language`. A transport of your own sends whatever its HTTP stack does. The [JavaScript README](https://github.com/schuyler/atgeo-sdk-js#privacy) lists exactly what goes on the wire; it's the same here, and the test suite checks it against a request-capturing fake server.

## Caching

Tile lists and the service description are cached for an hour, tiles in memory up to `cache_budget_bytes`, least recently used out first. A repeat search over a warm region makes no requests at all. Nothing is persisted; a new client starts cold.

The budget counts the tiles' decoded response bodies, and that is what the cache keeps. Parsed, a tile takes about 3.5 times its body size, so only the latest search's tiles stay parsed, outside the budget. For the kilometre around Philadelphia's City Hall (47 tiles, 40 MB of bodies), that's 138 MB on top of the cache while the client holds them.

## Speed

It's pure Python, and a dense region shows it. Over the 47 tiles (40 MB) places.atgeo.org serves for a kilometre around Philadelphia's City Hall, on CPython 3.13, a cold search without text takes about a second, nearly all of it parsing JSON. Searching the same region again parses nothing, and scoring takes 0.36 s for `Starbux` and 0.77 s for `Reading Terminal Market`. That's fine for a script, a notebook or a server answering a request, and slow for a search per keystroke. The async client does its parsing and scoring on threads of its own, so the event loop keeps running in the meantime, and free-threaded CPython overlaps them properly.

## Notes

The behaviour is defined once for every port in [docs/port-contract.md](https://github.com/schuyler/atgeo-sdk-js/blob/main/docs/port-contract.md), and the Python-specific decisions are in [docs/python-sdk-spec.md](https://github.com/schuyler/atgeo-sdk-js/blob/main/docs/python-sdk-spec.md). Where this port reads malformed input differently from the reference (`" 1.5"` is no coordinate here and 1.5 there), [CONFORMANCE.md](CONFORMANCE.md) lists it. A compliant server never sends any of it.

Most of this port was written by Claude, working from the spec and the reference, with the reference's test suite ported one to one and run against the shared fixtures on both clients. It was also compared against the reference on live data: the same server answers for six searches around Philadelphia, replayed to both, gave the same 25,830 hits in the same order, with distances and scores agreeing to within 3e-16.

## Development

```sh
uv sync --group examples
uv run pytest
uv run ruff check && uv run ruff format --check
uv run mypy
ATGEO_SERVER=http://localhost:8000 uv run python scripts/e2e.py
```

The end-to-end script runs both clients, and needs a server that answers `getPlace` with `value` and `attribution` ([schuyler/garganorn#27](https://github.com/schuyler/garganorn/issues/27)); against an older garganorn it fails at `get_place`, which is the point. It takes the same `ATGEO_SERVER`, `ATGEO_DATASET`, `ATGEO_COLLECTION` and `ATGEO_REGION` variables as the JavaScript one. `ATGEO_NETWORK_TESTS=1 uv run pytest -m network` runs the TLS tests against places.atgeo.org and badssl.com.

`tests/fixtures/` holds the shared fixtures, copied from atgeo-sdk-js at the commit named in `SOURCE`, and the garganorn lexicons, with their own `SOURCE`.

## License

BSD 3-Clause; see [LICENSE](LICENSE).
