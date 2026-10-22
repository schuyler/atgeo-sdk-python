"""cache.test.ts, ported. LRU eviction and the budget, which the reference does not test, are in
test_wire.py."""

from __future__ import annotations

import datetime as dt
import urllib.parse

import pytest

from atgeo import Bbox, PlaceRecord

from .conftest import Client, Make
from .fake_server import FakeServer, FakeTile, FakeTileset, overture
from .helpers import place, place_origin, point, record, tile_url

REGION = Bbox(-122.42, 37.77, -122.41, 37.78)
OTHER_ORIGIN = "urn:atgeo:example.org:thing:shared"


class Clock:
    def __init__(self, at: str) -> None:
        self.set(at)

    def set(self, at: str) -> None:
        self.now = dt.datetime.fromisoformat(at).timestamp()

    def __call__(self) -> float:
        return self.now


def setup(make: Make, clock: Clock | None = None) -> tuple[FakeServer, Client]:
    fake = FakeServer(
        tilesets=[
            overture(
                [
                    FakeTile(
                        tile_url("a"),
                        [
                            record(place("a", "Alpha", [point(37.772, -122.412)]), 3),
                            record(place("shared", "Shared in A", [point(37.773, -122.413)]), 2),
                        ],
                    )
                ]
            ),
            FakeTileset(
                "example-thing",
                [{"dataset": "example.org", "collection": "thing"}],
                [
                    FakeTile(
                        tile_url("o"),
                        [record(place("shared", "Shared in other", [point(10, 10)], {"origin": OTHER_ORIGIN}))],
                    )
                ],
            ),
        ]
    )
    return fake, make(fake, clock=clock) if clock else make(fake)


def tile_list_count(fake: FakeServer) -> int:
    return sum("org.atgeo.getTiles" in r for r in fake.requests)


class TestWarmRegions:
    def test_answers_an_identical_second_search_offline(self, make: Make) -> None:
        fake, client = setup(make)
        first = client.search_places(REGION, text="Alpha")
        fake.go_offline()
        second = client.search_places(REGION, text="Alpha")
        assert second == first
        assert len(first.places) > 0

    def test_reuses_tiles_for_a_different_text_over_the_same_region(self, make: Make) -> None:
        fake, client = setup(make)
        client.search_places(REGION)
        before = len(fake.requests)
        client.search_places(REGION, text="Alpha")
        assert len(fake.requests) == before

    def test_makes_no_tile_request_for_a_repeated_search(self, make: Make) -> None:
        fake, client = setup(make)
        client.search_places(REGION)
        client.search_places(REGION)
        assert fake.requests.count(tile_url("a")) == 1


class TestTileListExpiry:
    def test_expires_after_3600_s_and_then_requires_network(self, make: Make) -> None:
        clock = Clock("2026-01-01T00:00:00+00:00")
        fake, client = setup(make, clock)
        client.search_places(REGION)
        base = len(fake.requests)

        clock.set("2026-01-01T00:59:59+00:00")
        client.search_places(REGION)
        assert len(fake.requests) == base

        clock.set("2026-01-01T01:00:01+00:00")
        client.search_places(REGION)
        assert tile_list_count(fake) == 2
        assert fake.requests.count(tile_url("a")) == 1

    def test_fails_offline_once_the_tile_list_has_expired(self, make: Make) -> None:
        clock = Clock("2026-01-01T00:00:00+00:00")
        fake, client = setup(make, clock)
        client.search_places(REGION)
        fake.go_offline()
        clock.set("2026-01-01T01:00:01+00:00")
        with pytest.raises(ConnectionError):
            client.search_places(REGION)

    def test_caches_describe_service_for_3600_s(self, make: Make) -> None:
        clock = Clock("2026-01-01T00:00:00+00:00")
        fake, client = setup(make, clock)
        client.describe_service()
        clock.set("2026-01-01T00:59:59+00:00")
        client.describe_service()
        assert len(fake.requests) == 1
        clock.set("2026-01-01T01:00:01+00:00")
        client.describe_service()
        assert len(fake.requests) == 2


class TestGetPlaceCacheScan:
    def test_answers_from_an_already_fetched_tile_without_a_request(self, make: Make) -> None:
        fake, client = setup(make)
        client.search_places(REGION)
        before = len(fake.requests)
        found = client.get_place(place_origin("a"))
        assert len(fake.requests) == before
        assert found == PlaceRecord(
            origin=place_origin("a"), place=place("a", "Alpha", [point(37.772, -122.412)]), importance=3
        )

    def test_answers_offline_for_a_cached_origin(self, make: Make) -> None:
        fake, client = setup(make)
        client.search_places(REGION)
        fake.go_offline()
        assert client.get_place(place_origin("shared")).place["name"] == "Shared in A"

    def test_matches_the_whole_origin_not_a_shared_id(self, make: Make) -> None:
        fake, client = setup(make)
        client.search_places(REGION)
        before = len(fake.requests)
        found = client.get_place(OTHER_ORIGIN)
        get_places = [r for r in fake.requests[before:] if urllib.parse.urlsplit(r).path == "/xrpc/org.atgeo.getPlace"]
        assert len(get_places) == 1
        assert dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(get_places[0]).query))["uri"] == OTHER_ORIGIN
        assert found.place["name"] == "Shared in other"

    def test_returns_the_copy_from_the_lowest_tile_url_although_a_higher_one_was_cached_first(self, make: Make) -> None:
        def copy(name: str) -> dict[str, object]:
            return record(place("x", name, [point(37.775, -122.415)]))

        fake = FakeServer(
            tilesets=[overture([FakeTile(tile_url("a"), [copy("Copy A")]), FakeTile(tile_url("b"), [copy("Copy B")])])],
            tiles_for=lambda bbox: [tile_url("a") if bbox.startswith("-122") else tile_url("b")],
        )
        client = make(fake)
        client.search_places(Bbox(-100.5, 10.5, -100.4, 10.6))
        assert client.get_place(place_origin("x")).place["name"] == "Copy B"
        client.search_places(REGION)
        fake.go_offline()
        assert client.get_place(place_origin("x")).place["name"] == "Copy A"


class TestParsedTileMemo:
    """No reference counterpart: the memo of the latest search's parsed tiles (contract §10's freedom)."""

    def test_serves_a_repeat_search_the_same_place_objects(self, make: Make) -> None:
        _, client = setup(make)
        first = client.search_places(REGION).places
        second = client.search_places(REGION, text="alpha").places
        assert any(a.place is b.place for a in first for b in second)

    def test_does_not_answer_for_a_tile_the_cache_evicted(self, make: Make) -> None:
        # A budget below one tile evicts it at once: the memo holds it, but must not stand in for the cache.
        fake = setup(make)[0]
        client = make(fake, cache_budget_bytes=10)
        client.search_places(REGION)
        client.search_places(REGION)
        assert fake.requests.count(tile_url("a")) == 2
