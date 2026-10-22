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

Bodies are decoded the way `JSON.parse(await response.text())` would: a leading byte-order mark is dropped, invalid UTF-8 becomes U+FFFD, and the literals `NaN`, `Infinity` and `-Infinity`, which `json.loads` accepts by default, fail the body as `JSON.parse` does. Three margins remain, all from malformed input:

- Nesting deeper than the parser's recursion limit raises `RecursionError` (about 1,000 levels on CPython 3.11, 10,000 on 3.13, 40,000 on 3.14, 6,000 on PyPy 3.11); `JSON.parse` goes deeper. The clients' own threads have 8 MiB stacks, so this is an error, never a crash.
- An integer literal of more than 4,300 digits raises `ValueError` (CPython's limit on integer string conversion); `JSON.parse` reads it as Infinity.
- A JSON `-0` integer becomes `0`, where JavaScript keeps negative zero. Nothing the client compares can tell them apart.

In a tile, any of these makes the tile fail; in an XRPC answer, the call raises `AtgeoDecodingError`.

## Content encodings

The default transports ask for gzip and accept only gzip or no encoding. A body in an encoding they did not ask for (`br`, `deflate`, `zstd`) fails, as a failed tile or a `BodyError`; the reference's `fetch` would decode it.

## URLs

Every URL is prepared before a transport sees it: tab, LF and CR are dropped, and space, other control characters and non-ASCII are percent-encoded as UTF-8, which is what the reference's WHATWG URL parsing does to those characters. The other WHATWG rules are not reproduced: a backslash is not read as `/`, and `.` and `..` path segments are not resolved. urllib3 sends a lowercase escape uppercased (`%2c` as `%2C`), which decodes the same.

A tile URL that is not an absolute `http` or `https` URL is a failed tile, as it is under Node's `fetch`. A redirect to any other scheme is a `RedirectError`, and so is an eleventh redirect.

## Ordering a NaN distance (contract §8)

The reference orders by subtracting (`a.distance - b.distance`), so a NaN distance compares as a tie with everything, and the resulting order depends on the engine's sort. Only out-of-range coordinates in place data (a latitude past ±90, which the contract keeps as parsed) can produce one. This port sorts a NaN after every number, as the Kotlin port does, and ties `-0` with `0` exactly as subtraction does.

Such coordinates are also where Python's math library and JavaScript's part ways: with a latitude past 90, haversine's intermediate can go negative, where `math.sqrt` raises and `Math.sqrt` returns NaN. This port follows JavaScript there, so the distance is NaN rather than the search failing.

## Final sigma (contract §7)

`str.lower()` applies Unicode's Final_Sigma condition and matched V8 on every case tried, including the fixture's abbreviations and `𝚺𝚨𝚺`. No difference is known; Python's Unicode tables travel with the interpreter (14.0 on 3.11, 16.0 on 3.14), and the fixture runs on every supported version in CI.

The number table, the JSON margins and the NaN ordering are pinned by tests in `test_wire.py`, `test_ranking.py` and `test_distance.py`.
