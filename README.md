# atgeo for Python

A Python client for the atgeo gazetteer served by [garganorn](https://github.com/schuyler/garganorn), which searches places without telling the server what you searched for.

It's a port of [@atgeo/client](https://github.com/schuyler/atgeo-sdk-js) and returns the same places in the same order for the same server answers. There's a blocking client for scripts, notebooks and WSGI apps, and an asyncio client for async servers, with the same methods and results. It needs Python 3.11 or later, and installs urllib3 and truststore; the async client needs aiohttp as well.

## Synopsis

```python
from atgeo import AtgeoClient, Circle

with AtgeoClient() as atgeo:
    result = atgeo.search_places(Circle(lat=39.9526, lon=-75.1652, radius=1000), text="Starbux")  # metres

    for hit in result.places:
        print(hit.name, round(hit.distance), f"{hit.score:.2f}", hit.origin)
```

Misspellings are fine; matching is fuzzy, and that finds the Starbucks around Philadelphia's City Hall. The public server at places.atgeo.org has a limited dataset for now, mostly the east coast, so a search elsewhere may come back empty. If `result.complete` is `False`, some tiles failed to load and the results are partial; `result.tile_failures` says which, and why. If `result.overview` is `True`, the region was too big to search in full and you got only its most important places.

The async version is the same with `await`:

```python
import asyncio
from atgeo import AsyncAtgeoClient, Circle


async def main() -> None:
    async with AsyncAtgeoClient() as atgeo:
        result = await atgeo.search_places(Circle(39.9526, -75.1652, 1000), text="Starbux")
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

### `search_places(region, *, text=None, dataset=None, collection=None, sort="fused") -> SearchResult`

| Argument | Meaning |
|---|---|
| `region` | Required. `Circle(lat, lon, radius)` with the radius in metres, or `Bbox(min_lon, min_lat, max_lon, max_lat)` in degrees. Note the order: a circle is latitude first, a box longitude first (GeoJSON's order), so name the arguments. A bbox with `min_lon > max_lon` crosses the antimeridian. |
| `text` | Optional search text, matched fuzzily against each place's `name` and its `variants[].name` after lowercasing and stripping accents and extra whitespace. Places scoring below 0.6 are dropped. |
| `dataset` | Optional, such as `openstreetmap.org`. Defaults to `overturemaps.org`. |
| `collection` | Optional, such as `way`. |
| `sort` | `SortMode.FUSED` (default), `DISTANCE` or `SIMILARITY`, or the same as strings. |

Everything after `region` is keyword-only, so `search_places(region, "Starbux")` is an error rather than a guess.

With neither `dataset` nor `collection` you search `overturemaps.org` / `place`; with only `collection`, that collection of `overturemaps.org`; with only `dataset`, every collection of that dataset together; with both, that collection.

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class SearchResult:
    places: list[SearchHit]
    complete: bool  # False if any tile failed to load
    overview: bool  # True if the server answered with its coarse overview
    tiles: TileCounts  # fetched and failed
    tile_failures: tuple[TileFailure, ...]  # each failed tile, in tile URL order; () when complete


@dataclass(frozen=True, slots=True, kw_only=True)
class TileFailure:
    url: str  # the tile URL as the server's tile list gave it
    kind: TileFailureKind  # REFUSED, STATUS, DECODE or TRANSPORT
    status: int | None  # the HTTP status for STATUS, else None
    error: Exception  # what was raised, traceback and all; not part of ==


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchHit:
    origin: str | None  # place["origin"], else the tile record's uri, else None
    place: Mapping[str, Any]  # the record value, exactly as served
    importance: int | None  # relative to its tileset; None if it has none
    distance: float  # metres from the region's centre to the nearest point of the place; 0 if it contains the centre
    score: float | None  # 0 to 1 text similarity; None without text

    name: str | None  # property: place["name"] if it's a string
    location: PlaceLocation  # property: point and/or box, parsed as the client parsed them for distance
```

`place` is the dict `json.loads` made of the gazetteer's record, every key in served order and every integer exact. **Don't change it.** The client keeps the latest search's parsed tiles so that searching the same region with new text parses nothing, and `place` is the dict in that memo, so a change would show up in the next search. Copy it first with `copy.deepcopy(hit.place)` if you need to (`dict(hit.place)` copies only the top level, so its nested lists and dicts are still the cache's); copying every place by default would cost three times what parsing does. It's annotated `Mapping`, so a type checker catches the mistake.

`name` and `location` save you the two reads every app needs: `location.point` is the first usable `geo` entry of `place["locations"]`, `location.bbox` the first usable `bbox` entry (`west > east` crosses the antimeridian), coordinates already parsed from the strings they arrive as.

`FUSED` orders by similarity, breaking near-ties by importance, then distance. `DISTANCE` orders nearest first, then the smaller place, then importance, then similarity. `SIMILARITY` orders by similarity, then distance, and needs `text`. Scores are compared to three decimals and remaining ties break on `origin`, so the order is deterministic.

The [JavaScript README](https://github.com/schuyler/atgeo-sdk-js#readme) has the long version: overview answers, places that appear in several tiles, and how ties are broken. All of it applies here unchanged.

The result types are frozen, keyword-only dataclasses. You can construct them for your own tests, and fields added later won't break code that does.

### `get_place(origin) -> PlaceRecord`

Returns `PlaceRecord(origin, place, importance)` for the `origin` of a hit, with the same `name` and `location`. If a cached tile holds the place, no request is made; otherwise the client asks the server directly.

### `describe_service() -> ServiceDescription`

The server's DID and the tilesets it serves. Cached for an hour.

### `describe_tileset(slug) -> Tileset`

A tileset's collections, attribution and extent. Not cached. The describe types are read leniently: a field that is missing or of the wrong type comes back `None`, never as an exception. Their field names are snake_case (`record_count`, `published_at`) and their lists are tuples.

### Errors

- `AtgeoError` has the server's error `code` (`PlaceNotFound`, `BboxTooLarge`, `DatasetNotFound` and so on), or `HttpError` when the server didn't send one, a `message`, and the HTTP `status`. Use `status` to tell a request the server refused (4xx) from one it failed to answer (5xx); codes are an open list and new ones may appear.
- `search_places` raises `ValueError` for an invalid region, an unknown `sort` or `SIMILARITY` without text, and `TypeError` for a region that isn't a `Circle` or a `Bbox`, before any request.
- A failed `getTiles` fails the search. A failed tile does not: it's skipped, listed in `tile_failures` and counted in `tiles.failed`, and `complete` is `False`. A tile's `kind` says where it went wrong: `REFUSED` is a tile URL that isn't absolute `http` or `https` or has credentials in it, and no request was made; `STATUS` a non-2xx, with its `status` and the `AtgeoError`; `DECODE` a 2xx whose body isn't a tile, with whatever the parser raised; `TRANSPORT` anything the transport raised, a timeout, a dropped connection, a body over the cap, or a bug in a transport of your own, which is why that one is a failed tile and not an exception. The kind, URL and status are the same in every atgeo SDK; `error` is Python's own, so two results compare equal without it.
- An error in the client's own work after a tile has decoded, such as storing it in the cache, fails the search rather than passing for a bad tile. So does `MemoryError`, wherever it comes from.
- Each failed tile is also logged on the `atgeo` logger at `DEBUG`, with its URL, kind, status and error and nothing else, never your search text or region. The library adds no handler, as libraries shouldn't, so you see nothing until you ask: `logging.getLogger("atgeo").setLevel(logging.DEBUG)` with a handler configured, or `logging.basicConfig(level=logging.DEBUG)`. It never logs above `DEBUG`, so a search on every keystroke over a bad connection won't fill your logs, and it sends nothing anywhere.
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

The end-to-end script runs both clients and takes the same `ATGEO_SERVER`, `ATGEO_DATASET`, `ATGEO_COLLECTION` and `ATGEO_REGION` variables as the JavaScript one. `ATGEO_NETWORK_TESTS=1 uv run pytest -m network` runs the TLS tests against places.atgeo.org and badssl.com.

`tests/fixtures/` holds the shared fixtures, copied from atgeo-sdk-js at the commit named in `SOURCE`, and the garganorn lexicons, with their own `SOURCE`.

## License

BSD 3-Clause; see [LICENSE](LICENSE).
