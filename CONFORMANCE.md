# Conformance notes

Where this port reads input differently from the TypeScript reference, within the freedoms the [port contract](https://github.com/schuyler/atgeo-sdk-js/blob/main/docs/port-contract.md) grants (§5, §14). A compliant server never sends any of these; they matter only for malformed data.

This port is conformant to atgeo-sdk-js [`fc56331`](https://github.com/schuyler/atgeo-sdk-js/commit/fc5633103e5ef195c1f9e6e96213e33ce56058ad), the contract pinned to the squash merge of atgeo-sdk-js#55 (`97c776b`: `search_nearby` and the keyword methods, `disclosed`, the tier-1 and tier-2 thresholds, `KeywordRequired`, aborted requests), with the garganorn lexicons at [`59d21b1`](https://github.com/schuyler/garganorn/commit/59d21b117f4a01f9f459fc83c2624b53c6cf503a), which add `searchPlaces`. `tests/fixtures/` is vendored from there byte for byte and CI checks it against its `SOURCE` files.

## Fixtures and the tests the fixtures can't be (contract §13)

- `normalize.json`, `jaro-winkler.json` and `ranking-scenarios.json` (22 scenarios, which name the dataset and collection of each search) run as before. A scenario with `"overview": true` has the fake server answer `getTiles` with an overview, so the fuzzy drop runs.
- `match-tiers.json` (36 cases) runs in `tests/test_ranking.py::TestMatchTiers`, on the normalized query and the case's place, through the same `match_tier` ranking uses.
- `match-tiers-lone-surrogates.json` (4 cases) runs in the same class: a Python string can hold a lone surrogate, so this port runs it, as the contract asks of JavaScript, Kotlin and Python.
- `qualifying-keywords.json` (23 cases) runs in `tests/test_qualifying.py`, against `qualifies`.
- `keyword-regions.json` (72 cases) runs in `tests/test_keyword.py::TestKeywordRegions`, against both clients and a fake that refuses what the real server refuses (`BboxTooPrecise`, `InvalidBbox`); each `sent` is also checked to be plain integers.
- `keyword-fallback.json` (7 scenarios) runs in `tests/test_keyword_fallback.py`, against both clients, with a fresh client per scenario and a raw request-capturing server. The origins are compared in order, with `disclosed`, the number of `searchPlaces` requests and their exact parameters.
- `lenient-readers.json` runs in `tests/test_wire.py::TestLenientReaders`, against the six readers (the `searchPlaces` reader's `origin` is a JSON null where the others drop a `None`). The readers return dataclasses, which the runner turns into JSON terms (camelCase keys, `None` as a missing key, tuples as lists) and compares strictly, so `True` is not `1`.
- `attribution-scenarios.json` runs in `tests/test_attribution.py`, against both clients, with a fresh client per scenario and a raw server: every `getTiles` answers the scenario's tile URLs in listed order, and a tile with a status and no body gets the default error body.
- Tile arrival order, which a fixture can't express: the same file reruns every scenario with more than one tile with the lowest URL's response delayed longest, and checks that the server did see the tiles in reverse URL order.
- Ordinal order above U+FFFF, which no fixture reaches: `TestOrdinalOrderAboveTheBmp` sorts attribution entries and picks a dedupe winner between tiles whose names differ only at U+10000 and U+FF61. Python compares strings by code point, which puts them the other way round, so attribution is sorted with the same UTF-16 key as tile URLs.
- Warm searches, `get_place` from the cache and the copy rule are in `test_cache.py` and `test_client.py`.

## Attribution and hit collections (contract §4.10, §8, §9)

Where the reference copies the attribution it hands out, this port hands out immutable values: `NearbySearchResult.attribution`, `KeywordSearchResult.attribution` and `PlaceRecord.attribution` are tuples of frozen `Attribution` entries whose `licenses` is a tuple, and `NearbyHit.collection` and `KeywordHit.collection` are frozen `CollectionKey`s. Nothing an app does to them can reach the cache. The entry's `dataset` and `collection` are `str`, not optional, because the reader keeps an entry only when both are strings.

A tile's attribution is read again from its body when a tile is parsed again, as its records are (§10), so a tile's size stays the length of its body.

## A dataset that is not a string (contract §4.1)

`dataset` is a required keyword-only argument, so leaving it out is Python's own `TypeError`, raised by the call itself. One that isn't a `str` is treated as missing and raises `TypeError` too, on the first line of the search and so before the region is checked. A `str` subclass such as a `StrEnum` member is a string. On the async client a missing argument fails when the method is called, and a wrong-typed one when the coroutine is awaited.

## Number parsing (contract §5)

A coordinate that arrives as a string must be a decimal number and nothing else (`[+-]?(digits[.digits]|.digits)([eE][+-]?digits)?`, ASCII digits only) before it is parsed. Python's `float()` would accept far more: surrounding whitespace, `1_000`, Arabic-Indic digits, `inf`. The reference uses `parseFloat`, which reads a number off the front of whatever it gets.

| Input | Reference (`parseFloat`) | This port |
|---|---|---|
| ` 1.5`, `1.5 ` (surrounding whitespace) | 1.5 | no location |
| `1.5abc` | 1.5 | no location |
| `1e` | 1 | no location |
| `1_000` | 1 | no location |
| `0x10` | 0 | no location |
| `١٢` (non-ASCII digits) | no location | no location |
| `+1.5`, `.5`, `1.`, `-0`, `1e3` | same in both | same in both |
| `Infinity`, `NaN` | non-finite, skipped | non-finite, skipped |

## JSON parsing

Bodies are decoded the way `JSON.parse(await response.text())` would: a leading byte-order mark is dropped, invalid UTF-8 becomes U+FFFD, and the literals `NaN`, `Infinity` and `-Infinity`, which `json.loads` accepts by default, fail the body as `JSON.parse` does. Four margins remain, all from malformed input:

- Nesting deeper than the parser's recursion limit raises `RecursionError` (about 1,000 levels on CPython 3.11, 10,000 on 3.13, 40,000 on 3.14, 6,000 on PyPy 3.11); `JSON.parse` goes deeper. The clients' own threads have 8 MiB stacks, so this is an error, never a crash.
- An integer literal of more than 4,300 digits raises `ValueError` (CPython's limit on integer string conversion); `JSON.parse` reads it as Infinity.
- A JSON `-0` integer becomes `0`, where JavaScript keeps negative zero. Nothing the client compares can tell them apart.
- A body that starts with two byte-order marks fails here; Node 22's `Response.text()` drops both.

In a tile, any of these makes the tile fail, with kind `decode`; in an XRPC answer, the call raises `AtgeoDecodingError`.

## Snapping a circle (contract §3)

The circle's enclosing box is computed with the reference's floating-point expressions, operation for operation, so the snapped bbox strings match it exactly on every worked example and on random inputs. What remains is the C library: `math.sin`, `cos` and `asin` call the platform's libm, which can differ from V8's in the last bit. When a box edge then lands within that bit of the 0.01° grid, the box sent is one cell wider or narrower than the reference's. Over 40,000 circles chosen to put an edge as close to the grid as possible, 28 differed (0.07%). It changes which ~1 km cell is asked for, never what is revealed.

## Content encodings

The default transports ask for gzip and accept only gzip or no encoding. A body in an encoding they did not ask for (`br`, `deflate`, `zstd`) fails, as a failed tile of kind `transport` or a `BodyError`; the reference's `fetch` would decode it.

## URLs

Every URL is prepared before a transport sees it: leading and trailing spaces and control characters are trimmed, tab, LF and CR are dropped, and space, other control characters and non-ASCII are percent-encoded as UTF-8, which is what the reference's WHATWG URL parsing does to those characters. The other WHATWG rules are not reproduced: a backslash is not read as `/`, `.` and `..` path segments are not resolved, and a non-ASCII host is percent-encoded rather than turned into punycode (`bücher.example` is `b%C3%BCcher.example` here and `xn--bcher-kva.example` there), so a tile on such a host fails. urllib3 sends a lowercase escape uppercased (`%2c` as `%2C`), which decodes the same.

A tile URL that is not an absolute `http` or `https` URL, or that has credentials in it, is a failed tile of kind `refused`, as it is under Node's `fetch`, whose `Request` refuses credentials. So is a URL with an empty userinfo (`https://@tiles.example/a.json`, or `https://:@…`): it carries no credentials and the reference fetches it, but the contract lets a port refuse it, and no compliant server sends one. A redirect to any other scheme or to a URL with credentials is a `RedirectError`, and so is an eleventh redirect.

A query value holding a lone surrogate (from a Python string; JSON can carry one) is sent with U+FFFD in its place, as `URLSearchParams` sends it.

## Failed tiles (contract §4.4)

The reference counts failed tiles but does not say which; this port lists each in `tile_failures`. The reference's tile loop also catches every error, its own cache insert's included, so a bug there passes for a run of bad tiles; here an error after a tile has decoded fails the search, as the contract asks. A payload with no `records` list fails its tile in both, here through a check rather than the `TypeError` the reference's `.map` throws. An async transport that cancels its own request, a deadline of its own, while the search itself was not cancelled, is a `transport` failure whose error is `TimeoutError("transport cancelled its own request")` with the `CancelledError` as its `__cause__`: §4.4 lets a port report the error of its choosing, and a `CancelledError` is not an `Exception`, which `error` is typed as.

## Ordering a NaN distance (contract §8)

The reference orders by subtracting (`a.distance - b.distance`), so a NaN distance compares as a tie with everything, and the resulting order depends on the engine's sort. Only out-of-range coordinates in place data (a latitude past ±90, which the contract keeps as parsed) can produce one. This port sorts a NaN after every number, as the Kotlin port does, and ties `-0` with `0` exactly as subtraction does.

Such coordinates are also where Python's math library and JavaScript's part ways, and this port follows JavaScript each time: with a latitude past 90, haversine's intermediate can go negative, where `math.sqrt` raises and `Math.sqrt` returns NaN; `min()` skips a NaN that isn't its first argument, where `Math.min` returns NaN; and `math.fmod` raises on infinity, where `%` gives NaN. So the distance is NaN, as in the reference, rather than a number or a failed search.

## Match tier (contract §7)

The prefix test compares UTF-16 code units, as the contract asks, by encoding each folded word as it is for sorting. A Python `str` can hold a lone surrogate (`json.loads` makes one from `"\ud83d"`), and `str.startswith` compares code points, so a query ending in a lone high surrogate would not prefix a name holding the whole pair; here it does, as in the reference. The fixture's `क` against `कि` passes either way.

Punctuation is `unicodedata.category()` starting with `P`, where the reference uses V8's `\p{P}`. Both follow the Unicode version of their runtime (see below for Python's); no fixture case differs on any supported version.

`NearbyHit.name`, `.location`, `KeywordHit.name`, `.location`, `PlaceRecord.name` and `.location` (contract §8, §9) were already here, computed on access from the place, so they cannot disagree with `distance`.

## Final sigma (contract §7)

`str.lower()` applies Unicode's Final_Sigma condition and matched V8 on every code point up to U+2FFFF, on the fixture's abbreviations, and on `𝚺𝚨𝚺` and `ΑΣ㎏` (where the first lowercase, before NFKD, is what makes the sigma final).

Python's Unicode tables travel with the interpreter: 14.0 on 3.11, 15.0 on 3.12, 15.1 on 3.13, 16.0 on 3.14. Normalizing every code point up to U+2FFFF and comparing with Node 22 (Unicode 16.0): Python 3.14 differs on none, 3.12 and 3.13 on 112 (characters added in Unicode 16, plus U+1171E, whose category changed from Mn to Mc), and 3.11 on 209 (the same, plus characters added in 15.0). The contract accepts this skew, the 41 fixture cases pass on every supported version, and a place name would have to use a character newer than the interpreter to notice.

The number table, the JSON margins, URL preparation and the NaN ordering are pinned by tests in `test_wire.py`, `test_ranking.py`, `test_distance.py` and `test_robustness.py`.


## The keyword methods (contract §16 to §19)

Argument errors follow the contract's table for Python: `TypeError` for a missing or non-string `dataset`, a non-string `keyword` and a region of the wrong type (a `Circle` is one), `ValueError` for a bbox that fails the bbox checks and for a `keyword_region` that isn't `"none"` or `"degree"`, in the order of §3. The reference throws a `TypeError` for the last too; the contract's table gives Python `ValueError` for it, since only a missing or non-string `dataset`, a non-string `keyword` and a wrongly typed region are `TypeError`.

`search_nearby_then_by_keyword` always returns a `FallbackSearchResult`, where the reference returns the nearby result object itself when it skips the keyword request. The two are separate frozen dataclasses here, so the nearby result's fields are copied into a `FallbackSearchResult` (`places` as a new list holding the same hits), and its `disclosed` is the nearby result's own.

The autocompleter exists on the async client only, as the contract allows. Its calls run as `asyncio.Task`s the next call cancels, so a superseded call raises `asyncio.CancelledError` while `task.cancelling()` in its caller is 0. A call whose keyword cannot qualify supersedes the previous call first, then returns the empty result without a task, so it resolves even for a caller that has already been cancelled: no request is made, so there is nothing to stop (§19 step 3, §11). The blocking client has no autocompleter, and no cancellation but `KeyboardInterrupt`, which stops the call before its next request.

`search_by_keyword`'s answer is parsed on the client's own threads, as every other response is, so deeply nested JSON cannot overflow the caller's stack.

## Cancellation before a request (contract §11)

In the async client every request made directly from the caller's task (the keyword request, `get_place`, `describe_service`, `describe_tileset`) first yields to the event loop once, which delivers a cancellation that has already been requested, so that a transport that ignores cancellation, or only looks at it later, is never handed the request. The count `task.cancelling()` is not used as the test: a transport's timeout can leave it raised for good on a task that went on. A search's tile and tile-list requests run in tasks of a task group, which the group does not start once the search's own task has been cancelled. A call answered entirely from the cache still awaits its assembly thread, so from an already-cancelled task it raises `CancelledError` rather than completing, which §14 allows. Nothing between the nearby and the keyword half of the fallback catches `CancelledError`, and a test checks both that and that a cancel arriving between the halves sends no keyword.

## The qualifying keyword (contract §17)

Python counts a `str`'s code points with `len`, which is what the predicate asks for. A string holding a high surrogate followed by a low one as two code points (which JSON cannot produce but Python code can) would count as two, where a JavaScript string reads one character, so the keyword is first read as UTF-16 and the pair becomes the character it encodes. A lone surrogate counts as one character. `normalize` itself does not do this; it never needed to, since the comparisons of §7 are on code units.

## Whole-degree integers (contract §16)

The `bbox` of a keyword search is built from `math.floor` and `math.ceil`, which return `int`s for a `float`, so there is no `.0`, and `ceil(-0.5)` is `0`, never `-0`. `int` inputs (`Bbox(-123, 37, -122, 38)`) are valid and go through the same steps.

## Ranking thresholds (contract §7)

A tier-1 hit is kept at any score, a tier-2 hit needs 0.8, and a query with text whose tokens all fold away (`&`) keeps 0.6. The tier is computed for every record when there is text, which is what the threshold needs; the query's tokens are folded once per search.
