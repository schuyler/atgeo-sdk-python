"""Contract §12, items 1 to 4 and 6 to 9.

The transport seam carries a URL and nothing else, so the fake captures URLs only: every request is a
GET with no body by construction, and headers belong to the real transports (test_transports.py checks
them). The ported tests keep their names and their remaining assertions.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from typing import Any

import pytest

from atgeo import Bbox, Circle
from atgeo._cache import TileCache
from atgeo.transport import HttpResponse

from .conftest import Client, Make
from .fake_server import FakeServer, FakeTile, overture
from .helpers import OVERTURE, place, point, record

TILE = "https://tiles.example/t1.json"
OV: Any = OVERTURE


def setup(make: Make) -> Client:
    fake = FakeServer(
        tilesets=[overture([FakeTile(TILE, [record(place("r1", "Zanzibar Quokka Cafe", [point(37.775, -122.418)]))])])]
    )
    return make(fake)


def wire(requests: list[str]) -> list[str]:
    """Every request's URL, raw and percent-decoded, lowercased."""
    out = []
    for url in requests:
        out.append(url.lower())
        out.append(urllib.parse.unquote_plus(url).lower())
    return out


class TestSearchTextStaysLocal:
    @pytest.mark.parametrize("text", ["Zanzibar Quokka", "  ZANZIBÁR   quokka ", "Zanzibar"])
    def test_does_not_put_the_text_on_the_wire(self, make: Make, text: str) -> None:
        client = setup(make)
        client.search_nearby(Circle(37.7749295, -122.4194155, 500), text=text, **OVERTURE)
        assert len(client.fake.requests) > 0
        normalized = "zanzibar quokka"
        needles = [text.lower(), text.strip().lower(), normalized, *normalized.split(" "), "zanzib", "quokka"]
        encoded = [urllib.parse.quote(text).lower(), urllib.parse.quote_plus(text).lower()]
        for haystack in wire(client.fake.requests):
            for needle in needles + encoded:
                assert needle not in haystack

    def test_issues_only_get_tiles_and_tile_requests(self, make: Make) -> None:
        client = setup(make)
        client.search_nearby(Circle(37.7749295, -122.4194155, 500), text="Zanzibar Quokka", **OVERTURE)
        kinds = [
            "tiles" if "/xrpc/org.atgeo.getTiles?" in r else "tile" if r == TILE else r for r in client.fake.requests
        ]
        assert kinds == ["tiles", "tile"]

    def test_sends_no_body(self, make: Make) -> None:
        client = setup(make)
        client.search_nearby(Bbox(-122.43, 37.77, -122.41, 37.78), text="Zanzibar Quokka", **OVERTURE)
        assert len(client.fake.requests) > 0


class TestPreciseCoordinatesStayLocal:
    def test_keeps_the_circle_centre_off_the_wire(self, make: Make) -> None:
        client = setup(make)
        client.search_nearby(Circle(37.7749295, -122.4194155, 123.456789), text="Zanzibar Quokka", **OVERTURE)
        assert len(client.fake.requests) > 0
        for h in wire(client.fake.requests):
            for needle in ["37.7749295", "122.4194155", "7749295", "4194155", "123.456789"]:
                assert needle not in h

    def test_keeps_full_precision_bbox_corners_off_the_wire(self, make: Make) -> None:
        client = setup(make)
        client.search_nearby(Bbox(-122.4194155, 37.7749295, -122.4094155, 37.7849295), **OVERTURE)
        assert len(client.fake.requests) > 0
        for h in wire(client.fake.requests):
            for needle in ["7749295", "4194155", "4094155", "7849295"]:
                assert needle not in h


class TestGetPlaceSendsOnlyTheOrigin:
    """Contract §12 item 4, which privacy.test.ts leaves to client.test.ts; pinned here too."""

    def test_sends_the_origin_and_nothing_else(self, make: Make) -> None:
        client = setup(make)
        origin = "urn:atgeo:overturemaps.org:place:r1"
        client.get_place(origin)
        [url] = client.fake.requests
        parts = urllib.parse.urlsplit(url)
        assert parts.path == "/xrpc/org.atgeo.getPlace"
        assert urllib.parse.parse_qsl(parts.query) == [("uri", origin)]


class TestFailedTileLogs:
    """Contract §12 and §14: a failed tile is logged at DEBUG, with its entry's fields and nothing else."""

    def test_logs_a_failed_tile_at_debug_without_the_text_or_the_region(
        self, make: Make, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = setup(make)
        client.fake.fail_tiles.add(TILE)
        caplog.set_level(logging.DEBUG, logger="atgeo")
        result = client.search_nearby(Circle(37.7749295, -122.4194155, 123.456789), text="Zanzibar Quokka", **OVERTURE)
        assert len(result.tile_failures) == 1
        [entry] = [r for r in caplog.records if r.name == "atgeo"]
        assert entry.levelno == logging.DEBUG
        assert TILE in entry.getMessage()
        assert "(status, status 500)" in entry.getMessage()
        needles = ["zanzibar", "quokka", "37.7749295", "122.4194155", "7749295", "4194155", "123.456789", "37.77,"]
        for r in caplog.records:
            text = r.getMessage().lower()
            for needle in needles:
                assert needle not in text

    def test_logs_a_tile_url_with_a_newline_on_one_line(self, make: Make, caplog: pytest.LogCaptureFixture) -> None:
        bad = "https://tiles.example/x\nERROR forged line"
        listing = HttpResponse(200, json.dumps({"tiles": [bad]}).encode())
        fake = FakeServer(answer=lambda url: listing if "getTiles" in url else None)
        caplog.set_level(logging.DEBUG, logger="atgeo")
        assert [
            f.url for f in make(fake).search_nearby(Bbox(-122.42, 37.77, -122.41, 37.78), **OVERTURE).tile_failures
        ] == [bad]
        [entry] = [r for r in caplog.records if r.name == "atgeo"]
        assert "\n" not in entry.getMessage()
        assert repr(bad) in entry.getMessage()

    def test_logs_nothing_for_a_complete_search(self, make: Make, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger="atgeo")
        setup(make).search_nearby(Circle(37.7749295, -122.4194155, 500), text="Zanzibar Quokka", **OVERTURE)
        assert [r for r in caplog.records if r.name.startswith("atgeo")] == []

    def test_logs_nothing_for_a_search_that_fails(
        self, make: Make, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two tiles, one failing; storing the other raises, so the search fails with a failed tile in hand.
        client = setup(make)
        client.fake.tilesets[0].tiles.append(FakeTile("https://tiles.example/t0.json", []))
        client.fake.fail_tiles.add("https://tiles.example/t0.json")

        def broken(*_: object) -> None:
            raise RuntimeError("a bug in the cache")

        monkeypatch.setattr(TileCache, "set", broken)
        caplog.set_level(logging.DEBUG, logger="atgeo")
        with pytest.raises(RuntimeError):
            client.search_nearby(Circle(37.7749295, -122.4194155, 500), text="Zanzibar Quokka", **OVERTURE)
        assert [r for r in caplog.records if r.name.startswith("atgeo")] == []


SEARCH_PLACES = "/xrpc/org.atgeo.searchPlaces?"
PRECISE = Bbox(-122.4194155, 37.7749295, -122.4094155, 37.7849295)
DIGITS = ["7749295", "4194155", "4094155", "7849295"]


def params(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query, keep_blank_values=True))


class TestSearchByKeywordSendsOneSearchPlaces:
    """Item 6."""

    def test_sends_one_get_with_exactly_the_parameters_and_nothing_else(self, make: Make) -> None:
        client = setup(make)
        client.search_by_keyword("Zanzibar", **OVERTURE, region=PRECISE)
        assert len(client.fake.requests) == 1
        assert SEARCH_PLACES in client.fake.requests[0]
        assert params(client.fake.requests[0]) == {**OVERTURE, "q": "Zanzibar", "bbox": "-123,37,-122,38"}

    def test_sends_no_collection_and_no_bbox_when_none_was_given(self, make: Make) -> None:
        client = setup(make)
        client.search_by_keyword("Zanzibar", dataset="overturemaps.org")
        assert params(client.fake.requests[0]) == {"dataset": "overturemaps.org", "q": "Zanzibar"}

    def test_sends_a_bbox_of_four_whole_numbers_and_no_digit_of_a_precise_region(self, make: Make) -> None:
        client = setup(make)
        client.search_by_keyword("Zanzibar", **OVERTURE, region=PRECISE)
        assert re.fullmatch(r"-?\d+,-?\d+,-?\d+,-?\d+", params(client.fake.requests[0])["bbox"])
        for url in client.fake.requests:
            for digits in DIGITS:
                assert digits not in url
                assert digits not in urllib.parse.unquote_plus(url)

    def test_sends_the_keyword_exactly_as_passed(self, make: Make) -> None:
        client = setup(make)
        client.search_by_keyword("  ZANZIBÁR   quokka ", dataset="overturemaps.org")
        assert params(client.fake.requests[0])["q"] == "  ZANZIBÁR   quokka "


class TestTheFallbacksNearbyHalfKeepsTheKeywordLocal:
    """Item 7."""

    def test_puts_the_keyword_in_the_search_places_request_only(self, make: Make) -> None:
        client = setup(make)
        keyword = "Kumquat Lagoon"
        client.search_nearby_then_by_keyword(
            Circle(37.7749295, -122.4194155, 500), **OVERTURE, keyword=keyword, keyword_region="none"
        )
        search = [r for r in client.fake.requests if SEARCH_PLACES in r]
        nearby = [r for r in client.fake.requests if SEARCH_PLACES not in r]
        assert len(search) == 1
        assert nearby
        for haystack in wire(nearby):
            for needle in ["kumquat lagoon", "kumquat", "lagoon", "kumqu"]:
                assert needle not in haystack

    def test_sends_no_keyword_when_a_nearby_hit_matches_word_for_word(self, make: Make) -> None:
        client = setup(make)
        client.search_nearby_then_by_keyword(
            Circle(37.7749295, -122.4194155, 500), **OVERTURE, keyword="Zanzibar Quo", keyword_region="degree"
        )
        assert all(SEARCH_PLACES not in r for r in client.fake.requests)
        for haystack in wire(client.fake.requests):
            for needle in ["zanzibar quo", "zanzibar", "quo"]:
                assert needle not in haystack

    def test_sends_no_keyword_when_the_keyword_cannot_qualify(self, make: Make) -> None:
        client = setup(make)
        client.search_nearby_then_by_keyword(
            Circle(37.7749295, -122.4194155, 500), **OVERTURE, keyword="Q", keyword_region="degree"
        )
        assert all(SEARCH_PLACES not in r for r in client.fake.requests)
        for haystack in wire(client.fake.requests):
            assert "q=" not in haystack

    @pytest.mark.parametrize(("keyword_region", "bbox"), [("degree", "-123,37,-122,38"), ("none", None)])
    def test_sends_the_keyword_as_search_by_keyword_does(
        self, make: Make, keyword_region: str, bbox: str | None
    ) -> None:
        client = setup(make)
        client.search_nearby_then_by_keyword(
            Bbox(-122.43, 37.77, -122.41, 37.78),
            **OVERTURE,
            keyword="Kumquat",
            keyword_region=keyword_region,
        )
        sent = [params(r) for r in client.fake.requests if SEARCH_PLACES in r]
        assert sent == [{**OVERTURE, "q": "Kumquat", **({"bbox": bbox} if bbox else {})}]


class TestAutocompleteSendsWhatSearchByKeywordDoes:
    """Item 8, with test_autocomplete.py's request-for-request comparison."""

    def test_sends_nothing_for_a_keyword_that_cannot_qualify(self) -> None:
        import asyncio  # noqa: PLC0415

        from atgeo import AsyncAtgeoClient  # noqa: PLC0415

        fake = FakeServer(tilesets=[overture([FakeTile(TILE, [])])])
        client = AsyncAtgeoClient(transport=fake.async_transport)

        async def main() -> None:
            typing = client.autocomplete_by_keyword(**OV)
            for keyword in ("", " ", "z", "Z.", "z z", "하"):
                await typing.search_by_keyword(keyword)

        asyncio.run(main())
        assert fake.requests == []


class TestDisclosedAgreesWithTheCapturedRequests:
    """Item 9."""

    def test_keyword_is_true_exactly_when_a_search_places_request_was_made(self, make: Make) -> None:
        client = setup(make)
        region = Bbox(-122.43, 37.77, -122.41, 37.78)
        cases = [
            ("Zanzibar Quo", "none"),  # a nearby word match: nothing sent
            ("Kumquat", "none"),  # the fallback: sent
            ("Q", "degree"),  # cannot qualify: nothing sent
        ]
        for keyword, keyword_region in cases:
            before = len(client.fake.requests)
            result = client.search_nearby_then_by_keyword(
                region,
                **OVERTURE,
                keyword=keyword,
                keyword_region=keyword_region,
            )
            sent = any(SEARCH_PLACES in r for r in client.fake.requests[before:])
            assert result.disclosed.keyword is sent

    def test_region_is_the_finest_grid_among_the_regions_that_went_out(self, make: Make) -> None:
        fake = FakeServer(
            tilesets=[
                overture([FakeTile(TILE, [record(place("r1", "Zanzibar Quokka Cafe", [point(37.775, -122.418)]))])])
            ]
        )
        client = make(fake)
        region = Bbox(-122.43, 37.77, -122.41, 37.78)
        cold = client.search_nearby_then_by_keyword(region, **OVERTURE, keyword="Kumquat", keyword_region="degree")
        assert (cold.disclosed.keyword, cold.disclosed.region) == (True, 0.01)  # getTiles went out, and a 1 degree box
        warm = client.search_nearby_then_by_keyword(region, **OVERTURE, keyword="Kumquat", keyword_region="degree")
        assert (warm.disclosed.keyword, warm.disclosed.region) == (True, 1.0)  # only the keyword's box went out
        none = client.search_nearby_then_by_keyword(region, **OVERTURE, keyword="Kumquat", keyword_region="none")
        assert (none.disclosed.keyword, none.disclosed.region) == (True, None)  # the keyword, and no region at all
