# Conformance notes

Where this port reads input differently from the TypeScript reference, within the freedoms the [port contract](https://github.com/schuyler/atgeo-sdk-js/blob/main/docs/port-contract.md) grants (§5, §14). A compliant server never sends any of these; they matter only for malformed data.

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

## Final sigma (contract §7)

`str.lower()` applies Unicode's Final_Sigma condition and matched V8 on every code point up to U+2FFFF, on the fixture's abbreviations, and on `𝚺𝚨𝚺` and `ΑΣ㎏` (where the first lowercase, before NFKD, is what makes the sigma final).

Python's Unicode tables travel with the interpreter: 14.0 on 3.11, 15.0 on 3.12, 15.1 on 3.13, 16.0 on 3.14. Normalizing every code point up to U+2FFFF and comparing with Node 22 (Unicode 16.0): Python 3.14 differs on none, 3.12 and 3.13 on 112 (characters added in Unicode 16, plus U+1171E, whose category changed from Mn to Mc), and 3.11 on 209 (the same, plus characters added in 15.0). The contract accepts this skew, the 41 fixture cases pass on every supported version, and a place name would have to use a character newer than the interpreter to notice.

The number table, the JSON margins, URL preparation and the NaN ordering are pinned by tests in `test_wire.py`, `test_ranking.py`, `test_distance.py` and `test_robustness.py`.
