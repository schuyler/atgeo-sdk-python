"""attribution.test.ts, ported: fixtures/attribution-scenarios.json (contract §13), run against both clients.

The tile bodies need not validate against the lexicons, so the server here is raw: it answers every getTiles
call with the scenario's tile URLs in listed order, and each tile URL with its body. A fresh client is made
for each scenario, since scenarios reuse tile URLs with different bodies and the tile-list cache is keyed by
dataset, collection and bbox.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import pytest

from atgeo import Attribution, CollectionKey, SearchResult
from atgeo.transport import HttpResponse, prepare_url

from .conftest import Make
from .fake_server import DEFAULT_SERVER, FakeServer
from .helpers import attribution, collection_key, fixture, region_of

SCENARIOS: list[dict[str, Any]] = fixture("attribution-scenarios.json")
GET_TILES = f"{DEFAULT_SERVER}/xrpc/org.atgeo.getTiles"
UPSTREAM_ERROR = {"error": "InternalServerError", "message": "upstream error"}
MULTI = [s for s in SCENARIOS if len(s["tiles"]) > 1]


class Raw:
    """A server for one scenario. `arrivals` collects the tile URLs in the order the server answers them."""

    def __init__(self, s: dict[str, Any], delays: dict[str, float] | None = None) -> None:
        # Keyed by the URL as listed and as the clients send it (a non-ASCII URL goes out percent-encoded).
        self.tiles = {u: t for t in s["tiles"] for u in (t["url"], prepare_url(t["url"]))}
        self.listing = HttpResponse(200, json.dumps({"tiles": [t["url"] for t in s["tiles"]]}).encode())
        self.arrivals: list[str] = []
        self.fake = FakeServer(answer=self.answer, delays=delays)

    def answer(self, url: str) -> HttpResponse | None:
        if url == GET_TILES or url.startswith(f"{GET_TILES}?"):
            return self.listing
        tile = self.tiles.get(url)
        if tile is None:
            return HttpResponse(404, b"Not found")
        self.arrivals.append(url)
        # A tile with a status and no body gets the default error body; a 200 with it is a decode failure.
        body = tile.get("body")
        return HttpResponse(tile.get("status", 200), json.dumps(UPSTREAM_ERROR if body is None else body).encode())


def search(make: Make, s: dict[str, Any], server: Raw) -> SearchResult:
    kwargs: dict[str, Any] = {"dataset": s["dataset"]}
    if "collection" in s:
        kwargs["collection"] = s["collection"]
    return make(server.fake).search_places(region_of(s["region"]), **kwargs)


def check(result: SearchResult, s: dict[str, Any]) -> None:
    # Deep equality, in order: a field the scenario leaves out is None in the entry, and an entry is the
    # Attribution of §1, with a tuple of licenses.
    assert isinstance(result.attribution, tuple)
    assert result.attribution == tuple(attribution(e) for e in s["expect"]["attribution"])
    assert all(type(e) is Attribution for e in result.attribution)
    # The hits are a multiset of (origin, collection) pairs, order-insensitive. Each hit carries a collection:
    # a CollectionKey holding exactly the pair, or None, never the whole entry.
    for hit in result.places:
        assert hasattr(hit, "collection")
        assert hit.collection is None or type(hit.collection) is CollectionKey
    got = Counter((h.origin, h.collection) for h in result.places)
    assert got == Counter((h["origin"], collection_key(h["collection"])) for h in s["expect"]["hits"])


class TestAttributionScenarios:
    def test_has_the_scenarios_the_contract_describes(self) -> None:
        assert len(SCENARIOS) == 12

    @pytest.mark.parametrize("s", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
    def test_scenario(self, make: Make, s: dict[str, Any]) -> None:
        check(search(make, s, Raw(s)), s)


class TestTilesArrivingInReverseUrlOrder:
    """The union and the dedupe follow tile URL order whatever order the tiles arrive in (contract §4.10,
    §13): the lowest URL's response is delayed longest, so the highest arrives first."""

    def test_has_scenarios_with_more_than_one_tile(self) -> None:
        assert len(MULTI) == 4

    @pytest.mark.parametrize("s", MULTI, ids=[s["name"] for s in MULTI])
    def test_scenario(self, make: Make, s: dict[str, Any]) -> None:
        urls = sorted(t["url"] for t in s["tiles"])
        server = Raw(s, {u: (len(urls) - 1 - i) * 0.05 for i, u in enumerate(urls)})
        result = search(make, s, server)
        assert server.arrivals == urls[::-1]  # the delays took effect: the test is not vacuous
        check(result, s)


ASTRAL, BMP = "\U00010000", "\uff61"  # UTF-16 puts the astral one first (D800 < FF61); code point order does not


def payload(entries: list[dict[str, str]], records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"records": records or [], "attribution": entries}


def scenario(*tiles: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    """A dataset-only search of "d", over tiles given as (url, body)."""
    region = {"bbox": [-122.42, 37.76, -122.41, 37.78]}
    return {"dataset": "d", "region": region, "tiles": [{"url": u, "body": b} for u, b in tiles]}


class TestOrdinalOrderAboveTheBmp:
    """Contract §13, "Ordinal order above U+FFFF": the fixtures have no name outside the BMP, so nothing there
    fails a port that sorts by code point, which Python does by default. §4.3's comparator is UTF-16 code units."""

    def test_sorts_attribution_by_utf16_code_units_not_code_points(self, make: Make) -> None:
        entries = [{"dataset": BMP, "collection": "c"}, {"dataset": ASTRAL, "collection": "c"}]
        s = scenario(("https://tiles.example/a.json", payload(entries)))
        result = search(make, s, Raw(s))
        assert [e.dataset for e in result.attribution] == [ASTRAL, BMP]

    def test_sorts_by_collection_in_the_same_order(self, make: Make) -> None:
        entries = [{"dataset": "d", "collection": BMP}, {"dataset": "d", "collection": ASTRAL}]
        s = scenario(("https://tiles.example/a.json", payload(entries)))
        assert [e.collection for e in search(make, s, Raw(s)).attribution] == [ASTRAL, BMP]

    def test_decides_which_tile_wins_by_utf16_url_order_too(self, make: Make) -> None:
        lowest, highest = f"https://tiles.example/{ASTRAL}.json", f"https://tiles.example/{BMP}.json"
        pair = {"dataset": "d", "collection": "c"}
        s = scenario(
            (highest, payload([{**pair, "release": "bmp"}])),  # listed first: code point order says it is lowest
            (lowest, payload([{**pair, "release": "astral"}])),
        )
        result = search(make, s, Raw(s))
        assert [e.release for e in result.attribution] == ["astral"]
