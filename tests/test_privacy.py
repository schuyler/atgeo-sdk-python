"""Contract §12, items 1 to 4.

The transport seam carries a URL and nothing else, so the fake captures URLs only: every request is a
GET with no body by construction, and headers belong to the real transports (test_transports.py checks
them). The ported tests keep their names and their remaining assertions.
"""

from __future__ import annotations

import urllib.parse

import pytest

from atgeo import Bbox, Circle

from .conftest import Client, Make
from .fake_server import FakeServer, FakeTile, overture
from .helpers import place, point, record

TILE = "https://tiles.example/t1.json"


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
        client.search_places(Circle(37.7749295, -122.4194155, 500), text=text)
        assert len(client.fake.requests) > 0
        normalized = "zanzibar quokka"
        needles = [text.lower(), text.strip().lower(), normalized, *normalized.split(" "), "zanzib", "quokka"]
        encoded = [urllib.parse.quote(text).lower(), urllib.parse.quote_plus(text).lower()]
        for haystack in wire(client.fake.requests):
            for needle in needles + encoded:
                assert needle not in haystack

    def test_issues_only_get_tiles_and_tile_requests(self, make: Make) -> None:
        client = setup(make)
        client.search_places(Circle(37.7749295, -122.4194155, 500), text="Zanzibar Quokka")
        kinds = [
            "tiles" if "/xrpc/org.atgeo.getTiles?" in r else "tile" if r == TILE else r for r in client.fake.requests
        ]
        assert kinds == ["tiles", "tile"]

    def test_sends_no_body(self, make: Make) -> None:
        client = setup(make)
        client.search_places(Bbox(-122.43, 37.77, -122.41, 37.78), text="Zanzibar Quokka")
        assert len(client.fake.requests) > 0


class TestPreciseCoordinatesStayLocal:
    def test_keeps_the_circle_centre_off_the_wire(self, make: Make) -> None:
        client = setup(make)
        client.search_places(Circle(37.7749295, -122.4194155, 123.456789), text="Zanzibar Quokka")
        assert len(client.fake.requests) > 0
        for h in wire(client.fake.requests):
            for needle in ["37.7749295", "122.4194155", "7749295", "4194155", "123.456789"]:
                assert needle not in h

    def test_keeps_full_precision_bbox_corners_off_the_wire(self, make: Make) -> None:
        client = setup(make)
        client.search_places(Bbox(-122.4194155, 37.7749295, -122.4094155, 37.7849295))
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
