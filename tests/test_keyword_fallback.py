"""keywordFallback.test.ts, ported: search_nearby_then_by_keyword (contract §18), fixtures/keyword-fallback.json,
and the rules around it: argument order, the server's KeywordRequired, a nearby hit with no origin, the attribution
tie, and a request whose cancellation already happened (§11)."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import urllib.parse
from typing import Any, ClassVar

import pytest

from atgeo import (
    AsyncAtgeoClient,
    AtgeoClient,
    AtgeoError,
    Bbox,
    Disclosure,
    FallbackSearchResult,
    KeywordHit,
    NearbyHit,
)
from atgeo.transport import HttpResponse

from .conftest import Client, Make
from .fake_server import DEFAULT_SERVER, FakeServer, FakeTile, overture
from .helpers import OVERTURE, fixture, place, point, record, region_of

SCENARIOS: list[dict[str, Any]] = fixture("keyword-fallback.json")
OV: Any = OVERTURE
GET_TILES = f"{DEFAULT_SERVER}/xrpc/org.atgeo.getTiles"
SEARCH = f"{DEFAULT_SERVER}/xrpc/org.atgeo.searchPlaces"
BOX = Bbox(-122.42, 37.77, -122.41, 37.78)


def params_of(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query, keep_blank_values=True))


def searches(fake: FakeServer) -> list[str]:
    return [u for u in fake.requests if u.startswith(SEARCH)]


class Raw:
    """A server for one scenario: getTiles lists the tile URLs in listed order, each tile is served with its body,
    searchPlaces is answered with `answer` (or run through `on_search`). The bodies need not validate against the
    lexicons, so the fake's own routing is bypassed."""

    def __init__(self, tiles: list[dict[str, Any]], answer: Any = None, status: int = 200, error: Any = None) -> None:
        self.tiles = {t["url"]: t["body"] for t in tiles}
        self.listing = HttpResponse(200, json.dumps({"tiles": [t["url"] for t in tiles]}).encode())
        self.search = HttpResponse(status, json.dumps(error if error is not None else answer).encode())
        self.fake = FakeServer(answer=self.respond)

    def respond(self, url: str) -> HttpResponse | None:
        if url.startswith(GET_TILES):
            return self.listing
        if url.startswith(SEARCH):
            return self.search
        body = self.tiles.get(url)
        return HttpResponse(404, b"Not found") if body is None else HttpResponse(200, json.dumps(body).encode())


class TestScenarios:
    def test_has_the_scenarios_the_fixture_holds(self) -> None:
        assert len(SCENARIOS) == 7

    @pytest.mark.parametrize("s", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
    def test_scenario(self, make: Make, s: dict[str, Any]) -> None:
        raw = Raw(s["tiles"], s["keywordAnswer"])
        client = make(raw.fake)  # a fresh client per scenario: scenarios reuse tile URLs
        result = client.search_nearby_then_by_keyword(
            region_of(s["region"]),
            dataset=s["dataset"],
            **({"collection": s["collection"]} if "collection" in s else {}),
            keyword=s["keyword"],
            keyword_region=s["keywordRegion"],
            **({"sort": s["sort"]} if "sort" in s else {}),
        )
        assert [h.origin for h in result.places] == s["expect"]["origins"]
        assert dataclasses.asdict(result.disclosed) == s["expect"]["disclosed"]
        requests = searches(raw.fake)
        assert len(requests) == (1 if s["expect"]["keywordRequested"] else 0)
        if s["expect"]["keywordRequested"]:
            assert params_of(requests[0]) == {
                "dataset": s["dataset"],
                **({"collection": s["collection"]} if "collection" in s else {}),
                "q": s["keyword"],
                **({"bbox": s["expect"]["bbox"]} if "bbox" in s["expect"] else {}),
            }


class TestResults:
    def test_marks_each_hit_with_where_it_came_from(self, make: Make) -> None:
        s = SCENARIOS[2]
        result = make(Raw(s["tiles"], s["keywordAnswer"]).fake).search_nearby_then_by_keyword(
            region_of(s["region"]), dataset=s["dataset"], keyword=s["keyword"], keyword_region=s["keywordRegion"]
        )
        assert isinstance(result, FallbackSearchResult)
        kinds = [(type(h), h.via) for h in result.places]
        assert kinds == sorted(kinds, key=lambda k: k[1] != "keyword")  # keyword hits lead
        assert {k for k, _ in kinds} == {KeywordHit, NearbyHit}

    def test_returns_the_nearby_results_other_fields_whichever_branch_it_takes(self, make: Make) -> None:
        fake = FakeServer(
            tilesets=[
                overture([FakeTile("https://t.example/a", [record(place("a", "Zanzibar", [point(37.772, -122.412)]))])])
            ]
        )
        client = make(fake)
        nearby = client.search_nearby(BOX, text="Zanzibar", **OVERTURE)
        warm = client.search_nearby_then_by_keyword(BOX, **OVERTURE, keyword="Zanzibar", keyword_region="none")
        assert warm.places == nearby.places
        assert warm.attribution == nearby.attribution
        assert (warm.complete, warm.overview, warm.tiles, warm.tile_failures) == (
            nearby.complete,
            nearby.overview,
            nearby.tiles,
            nearby.tile_failures,
        )
        assert warm.disclosed == Disclosure(keyword=False, region=None)  # a warm nearby half sent nothing

    def test_never_leaves_out_a_nearby_hit_with_no_origin_even_when_a_keyword_hit_has_none_either(
        self, make: Make
    ) -> None:
        loc = [point(37.775, -122.415)]
        tile = {"records": [{"value": {"name": "Quokka Lounge", "locations": loc}}], "attribution": []}
        answer = {"places": [{"value": {"name": "Qwokka Bar", "locations": loc}, "attribution": []}]}
        raw = Raw([{"url": "https://places.atgeo.org/tiles/t.json", "body": tile}], answer)
        result = make(raw.fake).search_nearby_then_by_keyword(
            BOX, dataset="overturemaps.org", keyword="Qwokka", keyword_region="none"
        )
        assert [(h.via, h.name) for h in result.places] == [("keyword", "Qwokka Bar"), ("tiles", "Quokka Lounge")]

    def test_keeps_the_nearby_attribution_entry_when_both_halves_carry_the_same_collection(self, make: Make) -> None:
        def entry(text: str) -> dict[str, str]:
            return {"dataset": "overturemaps.org", "collection": "place", "attribution": text}

        tile = {"records": [], "attribution": [entry("from the tiles")]}
        k = {"value": {"origin": "urn:atgeo:overturemaps.org:place:k", "name": "Kumquat", "locations": [point(1, 1)]}}
        answer = {"places": [{**k, "attribution": [entry("from the keyword answer")]}]}
        raw = Raw([{"url": "https://places.atgeo.org/tiles/t.json", "body": tile}], answer)
        result = make(raw.fake).search_nearby_then_by_keyword(
            BOX, dataset="overturemaps.org", keyword="Kumquat", keyword_region="none"
        )
        assert [a.attribution for a in result.attribution] == ["from the tiles"]

    def test_reports_the_region_of_a_cold_degree_fallback_as_the_nearby_grid(self, make: Make) -> None:
        s = SCENARIOS[2]
        raw = Raw(s["tiles"], s["keywordAnswer"])
        result = make(raw.fake).search_nearby_then_by_keyword(
            region_of(s["region"]), dataset=s["dataset"], keyword=s["keyword"], keyword_region="degree"
        )
        assert result.disclosed == Disclosure(keyword=True, region=0.01)


class TestArguments:
    valid: ClassVar[Any] = {"dataset": "overturemaps.org", "keyword": "Starbux", "keyword_region": "none"}
    bad_box = Bbox(0, 10, 1, 5)

    def setup(self, make: Make) -> Client:
        return make(Raw([], {"places": []}).fake)

    def refuse(self, make: Make, error: type[Exception], message: str, region: Any, **kwargs: Any) -> None:
        client = self.setup(make)
        with pytest.raises(error, match=message):
            client.search_nearby_then_by_keyword(region, **{**self.valid, **kwargs})
        assert client.fake.requests == []

    # Each case is wrong in its own way and in every way after it, so the error names the first.
    def test_checks_the_dataset_first(self, make: Make) -> None:
        self.refuse(
            make, TypeError, "dataset", self.bad_box, dataset=None, keyword=5, keyword_region="x", sort="similarity"
        )

    def test_checks_the_keyword_second(self, make: Make) -> None:
        self.refuse(make, TypeError, "keyword must", self.bad_box, keyword=5, keyword_region="x", sort="similarity")

    def test_checks_keyword_region_third_and_wants_none_or_degree(self, make: Make) -> None:
        for bad in ("x", None, "DEGREE", 1):
            self.refuse(
                make, ValueError, "keyword_region", self.bad_box, keyword_region=bad, sort="similarity", keyword="  "
            )

    def test_checks_the_region_fourth(self, make: Make) -> None:
        self.refuse(make, ValueError, "invalid bbox", self.bad_box, sort="similarity", keyword="  ")
        self.refuse(make, TypeError, "Circle or a Bbox", (0, 0, 1, 1), sort="similarity", keyword="  ")

    def test_checks_the_sort_last(self, make: Make) -> None:
        self.refuse(make, ValueError, "similarity", BOX, sort="similarity", keyword="  ")
        self.refuse(make, ValueError, "unknown sort", BOX, sort="nope")

    def test_requires_keyword_region(self, make: Make) -> None:
        client = self.setup(make)
        valid = {k: v for k, v in self.valid.items() if k != "keyword_region"}
        with pytest.raises(TypeError):
            client.inner.search_nearby_then_by_keyword(BOX, **valid)


TILES = [FakeTile("https://tiles.example/p.json", [record(place("zc", "Zanzibar Cafe", [point(37.772, -122.412)]), 5)])]


class TestKeywordRequiredRefusal:
    """Contract §18 step 8: the qualifying predicate can pass a keyword the server refuses (`ﬁ`, which NFKD
    turns into two characters and the server keeps as one). That is no answer, not a failure."""

    def test_returns_the_nearby_result_disclosing_the_keyword_it_sent(self, make: Make) -> None:
        client = make(FakeServer(tilesets=[overture(TILES)]))
        nearby = make(FakeServer(tilesets=[overture(TILES)])).search_nearby(BOX, text="ﬁ", **OVERTURE)
        result = client.search_nearby_then_by_keyword(BOX, **OVERTURE, keyword="ﬁ", keyword_region="degree")
        assert len(searches(client.fake)) == 1  # the keyword went out, and was refused
        assert result.places == nearby.places
        assert result.attribution == nearby.attribution
        assert result.disclosed == Disclosure(keyword=True, region=0.01)

    def test_reports_a_degree_region_when_the_nearby_half_sent_nothing(self, make: Make) -> None:
        client = make(FakeServer(tilesets=[overture(TILES)]))
        client.search_nearby(BOX, **OVERTURE)  # warm
        for keyword_region, region in (("degree", 1.0), ("none", None)):
            result = client.search_nearby_then_by_keyword(BOX, **OVERTURE, keyword="ﬁ", keyword_region=keyword_region)
            assert result.disclosed == Disclosure(keyword=True, region=region)

    def test_still_raises_for_any_other_refusal_of_the_keyword_request(self, make: Make) -> None:
        fake = FakeServer(tilesets=[overture(TILES)])

        def answer(url: str) -> HttpResponse | None:
            if url.startswith(SEARCH):
                body = {"error": "InvalidRequest", "message": "q is too long"}
                return HttpResponse(400, json.dumps(body).encode())
            return None  # everything else is the fake's own routing

        fake.answer = answer
        with pytest.raises(AtgeoError) as raised:
            make(fake).search_nearby_then_by_keyword(BOX, **OVERTURE, keyword="Quokka", keyword_region="none")
        assert raised.value.code == "InvalidRequest"

    def test_still_raises_for_a_network_failure_of_the_keyword_request(self, make: Make) -> None:
        fake = FakeServer(tilesets=[overture(TILES)])
        client = make(fake)
        client.search_nearby(BOX, **OVERTURE)  # warm, so only the keyword request is left to fail
        fake.network_errors.add(f"{SEARCH}?dataset=overturemaps.org&collection=place&q=Quokka")
        with pytest.raises(ConnectionError):
            client.search_nearby_then_by_keyword(BOX, **OVERTURE, keyword="Quokka", keyword_region="none")


class TestFailureAfterTheNearbyHalf:
    """A failure of the keyword request loses the nearby results, and carries no `disclosed`, though the keyword
    may have reached the server (documented in the README and the methods' docstrings)."""

    def test_has_no_disclosed_on_the_error(self, make: Make) -> None:
        raw = Raw(
            [{"url": "https://places.atgeo.org/t.json", "body": {"records": []}}],
            error={"error": "InternalServerError", "message": "boom"},
            status=500,
        )
        with pytest.raises(AtgeoError) as raised:
            make(raw.fake).search_nearby_then_by_keyword(BOX, **OVERTURE, keyword="Quokka", keyword_region="none")
        assert len(searches(raw.fake)) == 1
        assert not hasattr(raised.value, "disclosed")

    @pytest.mark.parametrize("method", ["search_nearby_then_by_keyword", "search_by_keyword"])
    def test_documents_that_the_keyword_may_have_been_sent(self, method: str) -> None:
        for cls in (AtgeoClient, AsyncAtgeoClient):
            doc = getattr(cls, method).__doc__ or ""
            assert "keyword" in doc
            assert "may have reached the server" in doc or method == "search_by_keyword"


class TestAnAlreadyCancelledRequest:
    """Contract §11: a request whose cancellation has already happened never reaches the transport, or a transport
    that ignores cancellation would send it (atgeo-sdk-js#59). The fake records a request the moment it is called,
    which is before any await inside it, so it stands for such a transport."""

    @staticmethod
    def fake() -> FakeServer:
        return FakeServer(tilesets=[overture(TILES)])

    @staticmethod
    def own_task_cancelled(coro_fn: Any) -> Any:
        """Run the call in a task that has already requested its own cancellation."""

        async def main() -> None:
            asyncio.current_task().cancel()  # type: ignore[union-attr]
            await coro_fn()

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(main())

    def test_search_by_keyword_sends_nothing(self) -> None:
        fake = self.fake()
        client = AsyncAtgeoClient(transport=fake.async_transport)
        self.own_task_cancelled(lambda: client.search_by_keyword("Quokka", **OV))
        assert fake.requests == []

    def test_the_fallback_keeps_its_keyword_off_the_wire_when_the_caller_cancelled_before_the_keyword_half(
        self,
    ) -> None:
        fake = self.fake()
        client = AsyncAtgeoClient(transport=fake.async_transport)
        asyncio.run(client.search_nearby(BOX, **OVERTURE))  # warm: the nearby half needs no request
        before = len(fake.requests)
        self.own_task_cancelled(
            lambda: client.search_nearby_then_by_keyword(BOX, **OVERTURE, keyword="Quokka", keyword_region="none")
        )
        assert [u for u in fake.requests[before:] if u.startswith(SEARCH)] == []

    def test_a_cancellation_between_the_two_halves_stops_the_keyword_request(self) -> None:
        """The nearby half finishes; the caller is cancelled before the keyword half begins; the call is not
        allowed to catch the CancelledError in between."""
        fake = self.fake()
        client = AsyncAtgeoClient(transport=fake.async_transport)

        async def main() -> None:
            task = asyncio.current_task()
            assert task is not None
            await client.search_nearby(BOX, **OVERTURE)
            inner = client._engine.fallback_wants_keyword

            def cancel_then_want(keyword: str, nearby: Any) -> bool:
                task.cancel()  # arrives after the nearby half, as the keyword half is about to start
                return inner(keyword, nearby)

            client._engine.fallback_wants_keyword = cancel_then_want  # type: ignore[method-assign]
            await client.search_nearby_then_by_keyword(BOX, **OVERTURE, keyword="Quokka", keyword_region="none")

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(main())
        assert [u for u in fake.requests if u.startswith(SEARCH)] == []

    def test_get_place_and_the_describe_calls_send_nothing(self) -> None:
        fake = self.fake()
        client = AsyncAtgeoClient(transport=fake.async_transport)
        for call in (
            lambda: client.get_place("urn:atgeo:overturemaps.org:place:zc"),
            client.describe_service,
            lambda: client.describe_tileset("overture-place"),
        ):
            self.own_task_cancelled(call)
        assert fake.requests == []

    def test_nothing_in_the_fallback_catches_cancelled_error(self) -> None:
        # CancelledError is a BaseException, so only a bare or BaseException handler could swallow it.
        for cls in (AtgeoClient, AsyncAtgeoClient):
            source = inspect.getsource(cls.search_nearby_then_by_keyword)
            assert "CancelledError" not in source
            assert "BaseException" not in source
            assert "except:" not in source
