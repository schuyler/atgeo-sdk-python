"""cache.test.ts, ported. LRU eviction and the budget, which the reference does not test, are in
test_wire.py."""

from __future__ import annotations

import collections
import concurrent.futures
import copy
import dataclasses
import datetime as dt
import json
import multiprocessing
import operator
import pickle
import types
import urllib.parse
from collections.abc import Mapping
from typing import Any

import pytest

from atgeo import Attribution, Bbox, CollectionKey, PlaceRecord, SearchHit, SearchResult
from atgeo.transport import HttpResponse
from atgeo.types import _ReadOnlyMapping

from .conftest import Client, Make
from .fake_server import FakeServer, FakeTile, FakeTileset, overture
from .helpers import OVERTURE, assert_immutable, attribution, place, place_origin, point, record, tile_url, xrpc

REGION = Bbox(-122.42, 37.77, -122.41, 37.78)
OTHER_ORIGIN = "urn:atgeo:example.org:thing:shared"
GET_TILES = xrpc(
    "org.atgeo.getTiles",
    [("dataset", "overturemaps.org"), ("collection", "place"), ("bbox", "-122.42,37.77,-122.41,37.78")],
)


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
        first = client.search_places(REGION, text="Alpha", **OVERTURE)
        fake.go_offline()
        second = client.search_places(REGION, text="Alpha", **OVERTURE)
        assert second == first
        assert len(first.places) > 0

    def test_reuses_tiles_for_a_different_text_over_the_same_region(self, make: Make) -> None:
        fake, client = setup(make)
        client.search_places(REGION, **OVERTURE)
        before = len(fake.requests)
        client.search_places(REGION, text="Alpha", **OVERTURE)
        assert len(fake.requests) == before

    def test_makes_no_tile_request_for_a_repeated_search(self, make: Make) -> None:
        fake, client = setup(make)
        client.search_places(REGION, **OVERTURE)
        client.search_places(REGION, **OVERTURE)
        assert fake.requests.count(tile_url("a")) == 1


class TestTileListExpiry:
    def test_expires_after_3600_s_and_then_requires_network(self, make: Make) -> None:
        clock = Clock("2026-01-01T00:00:00+00:00")
        fake, client = setup(make, clock)
        client.search_places(REGION, **OVERTURE)
        base = len(fake.requests)

        clock.set("2026-01-01T00:59:59+00:00")
        client.search_places(REGION, **OVERTURE)
        assert len(fake.requests) == base

        clock.set("2026-01-01T01:00:01+00:00")
        client.search_places(REGION, **OVERTURE)
        assert tile_list_count(fake) == 2
        assert fake.requests.count(tile_url("a")) == 1

    def test_fails_offline_once_the_tile_list_has_expired(self, make: Make) -> None:
        clock = Clock("2026-01-01T00:00:00+00:00")
        fake, client = setup(make, clock)
        client.search_places(REGION, **OVERTURE)
        fake.go_offline()
        clock.set("2026-01-01T01:00:01+00:00")
        with pytest.raises(ConnectionError):
            client.search_places(REGION, **OVERTURE)

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
        client.search_places(REGION, **OVERTURE)
        before = len(fake.requests)
        found = client.get_place(place_origin("a"))
        assert len(fake.requests) == before
        assert found == PlaceRecord(
            origin=place_origin("a"),
            place=place("a", "Alpha", [point(37.772, -122.412)]),
            importance=3,
            attribution=(Attribution(dataset="overturemaps.org", collection="place"),),
        )

    def test_answers_offline_for_a_cached_origin(self, make: Make) -> None:
        fake, client = setup(make)
        client.search_places(REGION, **OVERTURE)
        fake.go_offline()
        assert client.get_place(place_origin("shared")).place["name"] == "Shared in A"

    def test_matches_the_whole_origin_not_a_shared_id(self, make: Make) -> None:
        fake, client = setup(make)
        client.search_places(REGION, **OVERTURE)
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
        client.search_places(Bbox(-100.5, 10.5, -100.4, 10.6), **OVERTURE)
        assert client.get_place(place_origin("x")).place["name"] == "Copy B"
        client.search_places(REGION, **OVERTURE)
        fake.go_offline()
        assert client.get_place(place_origin("x")).place["name"] == "Copy A"


class TestGetPlaceScanRecency:
    def test_leaves_the_eviction_order_alone(self, make: Make) -> None:
        # Two tiles fill the budget; a scan that found the older one must not save it from eviction.
        def tile(n: str) -> FakeTile:
            return FakeTile(tile_url(n), [record(place(n, n.upper(), [point(37.775, -122.415)]))])

        fake = FakeServer(
            tilesets=[overture([tile("a"), tile("b"), tile("c")])],
            tiles_for=lambda bbox: [tile_url({"-122.42": "a", "-100.50": "b"}.get(bbox[:7], "c"))],
        )
        size = len(fake.handle(tile_url("a")).body)
        client = make(fake, cache_budget_bytes=2 * size + 10)
        client.search_places(REGION, **OVERTURE)  # caches a
        client.search_places(Bbox(-100.5, 10.5, -100.4, 10.6), **OVERTURE)  # caches b; a is now least recently used
        client.get_place(place_origin("a"))  # answered from the cache, touching nothing
        client.search_places(Bbox(10.5, 10.5, 10.6, 10.6), **OVERTURE)  # caches c, which evicts a, not b
        fake.go_offline()
        assert client.get_place(place_origin("b")).name == "B"
        with pytest.raises(ConnectionError):
            client.get_place(place_origin("a"))


class TestAttributionInTheTileCache:
    def test_gives_a_warm_offline_search_the_same_non_empty_attribution_and_hit_collections(self, make: Make) -> None:
        fake, client = setup(make)
        first = client.search_places(REGION, **OVERTURE)
        assert len(first.attribution) > 0
        assert len(first.places) > 0
        for hit in first.places:
            assert hit.collection == CollectionKey(dataset="overturemaps.org", collection="place")
        fake.go_offline()
        second = client.search_places(REGION, **OVERTURE)
        assert second.attribution == first.attribution
        assert [h.collection for h in second.places] == [h.collection for h in first.places]

    def test_gives_a_warm_search_the_same_attribution_after_another_search_replaced_the_parsed_memo(
        self, make: Make
    ) -> None:
        # The memo holds only the latest search's tiles; a tile it no longer holds is parsed again from its body,
        # and the attribution is part of the body (contract §10).
        fake, client = setup(make)
        first = client.search_places(REGION, **OVERTURE)
        client.search_places(Bbox(10.5, 10.5, 10.6, 10.6), dataset="example.org")
        fake.go_offline()
        again = client.search_places(REGION, **OVERTURE)
        assert again.attribution == first.attribution
        assert [(h.origin, h.collection) for h in again.places] == [(h.origin, h.collection) for h in first.places]
        assert client.get_place(place_origin("a")).attribution == first.attribution

    def test_builds_a_searchs_attribution_only_from_its_own_tiles_though_another_tile_is_still_cached(
        self, make: Make
    ) -> None:
        entry_a = {"dataset": "ref-a.example", "collection": "c"}
        entry_b = {"dataset": "ref-b.example", "collection": "c"}
        fake = FakeServer(
            tilesets=[
                dataclasses.replace(
                    overture([FakeTile(tile_url("a"), [record(place("pa", "A", [point(37.775, -122.415)]))])]),
                    references=[entry_a],
                ),
                FakeTileset(
                    "overture-place-b",
                    [{"dataset": "overturemaps.org", "collection": "place"}],
                    [FakeTile(tile_url("b"), [record(place("pb", "B", [point(10.55, -100.45)]))])],
                    references=[entry_b],
                ),
            ],
            tiles_for=lambda bbox: [tile_url("a") if bbox.startswith("-122") else tile_url("b")],
        )
        client = make(fake)
        place_entry = attribution({"dataset": "overturemaps.org", "collection": "place"})
        a = client.search_places(REGION, **OVERTURE)
        b = client.search_places(Bbox(-100.5, 10.5, -100.4, 10.6), **OVERTURE)
        assert a.attribution == (place_entry, attribution(entry_a))
        assert b.attribution == (place_entry, attribution(entry_b))


class TestGetPlaceAttributionFromTheCache:
    def test_takes_the_attribution_of_the_lowest_url_tile_as_served_though_higher_ones_were_cached_before_and_after_it(
        self, make: Make
    ) -> None:
        references = [
            {"dataset": "z.example", "collection": "c"},
            {"dataset": "a.example", "collection": "c", "release": "1"},
            {"dataset": "a.example", "collection": "c", "release": "2"},
        ]

        def copy() -> dict[str, object]:
            return record(place("x", "Copy", [point(37.775, -122.415)]))

        def tileset(slug: str, n: str, refs: list[dict[str, Any]]) -> FakeTileset:
            return FakeTileset(
                slug,
                [{"dataset": "overturemaps.org", "collection": "place"}],
                [FakeTile(tile_url(n), [copy()])],
                references=refs,
            )

        fake = FakeServer(
            tilesets=[
                tileset("overture-place", "a", references),
                tileset("overture-place-b", "b", [{"dataset": "b.example", "collection": "c"}]),
                tileset("overture-place-c", "c", [{"dataset": "c.example", "collection": "c"}]),
            ],
            tiles_for=lambda bbox: [
                tile_url("a" if bbox.startswith("-122") else "b" if bbox.startswith("-100") else "c")
            ],
        )
        client = make(fake)
        # b, then a, then c: the lowest URL is neither the first nor the last cached.
        client.search_places(Bbox(-100.5, 10.5, -100.4, 10.6), **OVERTURE)
        client.search_places(REGION, **OVERTURE)
        client.search_places(Bbox(-90.5, 10.5, -90.4, 10.6), **OVERTURE)
        fake.go_offline()
        answer = client.get_place(place_origin("x"))
        expected = (attribution({"dataset": "overturemaps.org", "collection": "place"}), *map(attribution, references))
        assert answer.attribution == expected

    @pytest.mark.parametrize(
        "attribution_field",
        [
            pytest.param({}, id="no attribution"),
            pytest.param({"attribution": {}}, id="an attribution that is an object"),
            pytest.param({"attribution": None}, id="an attribution that is null"),
            pytest.param({"attribution": "x"}, id="an attribution that is a string"),
        ],
    )
    def test_gives_an_empty_attribution_and_no_hit_collection_from_a_tile_with_none(
        self, make: Make, attribution_field: dict[str, Any]
    ) -> None:
        value = place("x", "Plain", [point(37.775, -122.415)])
        listing = HttpResponse(200, json.dumps({"tiles": [tile_url("raw")]}).encode())
        body = HttpResponse(
            200, json.dumps({"records": [{"value": value, "importance": 3}], **attribution_field}).encode()
        )
        fake = FakeServer(responses={GET_TILES: listing, tile_url("raw"): body})
        client = make(fake)
        (hit,) = client.search_places(REGION, **OVERTURE).places
        assert hit.collection is None
        before = len(fake.requests)
        assert client.get_place(place_origin("x")) == PlaceRecord(
            origin=place_origin("x"), place=value, importance=3, attribution=()
        )
        assert len(fake.requests) == before


class TestAttributionIsTheAppsOwn:
    """frozenPlace.test.ts: what an app can and can't change in an attribution list it was handed."""

    def test_cannot_change_a_search_results_attribution_or_the_next_searchs(self, make: Make) -> None:
        licenses = ["https://l.example/1"]
        fake = FakeServer(
            tilesets=[
                FakeTileset(
                    "overture-place",
                    [
                        {
                            "dataset": "overturemaps.org",
                            "collection": "place",
                            "release": "2026-01-01",
                            "licenses": licenses,
                        }
                    ],
                    [FakeTile(tile_url("a"), [record(place("a", "Alpha", [point(37.772, -122.412)]), 5)])],
                )
            ]
        )
        client = make(fake)
        r1 = client.search_places(REGION, **OVERTURE)
        original = copy.deepcopy(r1.attribution)
        assert original[0].licenses == tuple(licenses)
        assert_immutable(r1.attribution)
        r2 = client.search_places(REGION, **OVERTURE)
        assert r2.attribution == original

    def test_hands_out_a_hit_collection_that_cannot_change_either(self, make: Make) -> None:
        _, client = setup(make)
        hit = client.search_places(REGION, **OVERTURE).places[0]
        assert hit.collection is not None
        with pytest.raises(dataclasses.FrozenInstanceError):
            hit.collection.collection = "changed"  # type: ignore[misc]
        again = client.search_places(REGION, **OVERTURE).places[0]
        assert again.collection == CollectionKey(dataset="overturemaps.org", collection="place")


class TestParsedTileMemo:
    """No reference counterpart: the memo of the latest search's parsed tiles (contract §10's freedom)."""

    def test_serves_a_repeat_search_the_same_place_objects(self, make: Make) -> None:
        _, client = setup(make)
        first = client.search_places(REGION, **OVERTURE).places
        second = client.search_places(REGION, text="alpha", **OVERTURE).places
        # Each hit wraps the place in its own read-only view, over the memo's dict: its contents are shared.
        assert any(a.place["locations"] is b.place["locations"] for a in first for b in second)

    def test_does_not_answer_for_a_tile_the_cache_evicted(self, make: Make) -> None:
        # A budget below one tile evicts it at once: the memo holds it, but must not stand in for the cache.
        fake = setup(make)[0]
        client = make(fake, cache_budget_bytes=10)
        client.search_places(REGION, **OVERTURE)
        client.search_places(REGION, **OVERTURE)
        assert fake.requests.count(tile_url("a")) == 2


class TestReadOnlyPlace:
    """No reference counterpart: a place is a read-only view of the cache's own dict, so a mutation fails at
    the line that made it instead of changing what the next search returns."""

    @staticmethod
    def assert_read_only(value: Mapping[str, Any]) -> None:
        assert isinstance(value, Mapping)
        assert type(value) is not dict
        with pytest.raises(TypeError):
            value["name"] = "Changed"  # type: ignore[index]
        with pytest.raises(TypeError):
            value["added"] = 1  # type: ignore[index]
        with pytest.raises(TypeError):
            del value["name"]  # type: ignore[attr-defined]
        assert not hasattr(value, "setdefault")
        assert not hasattr(value, "update")

    def test_rejects_a_top_level_assignment_to_a_hit(self, make: Make) -> None:
        _, client = setup(make)
        hits = client.search_places(REGION, **OVERTURE).places
        assert hits
        for hit in hits:
            self.assert_read_only(hit.place)

    def test_leaves_a_later_search_over_the_same_tile_unchanged(self, make: Make) -> None:
        _, client = setup(make)
        before = {h.origin: copy.deepcopy(dict(h.place)) for h in client.search_places(REGION, **OVERTURE).places}
        assert before
        for hit in client.search_places(REGION, **OVERTURE).places:
            with pytest.raises(TypeError):
                hit.place["name"] = "Changed"  # type: ignore[index]
            with pytest.raises(TypeError):
                del hit.place["name"]  # type: ignore[attr-defined]
        after = client.search_places(REGION, text="Alpha", **OVERTURE).places
        assert after
        assert all(dict(h.place) == before[h.origin] for h in after)
        assert {h.origin: dict(h.place) for h in client.search_places(REGION, **OVERTURE).places} == before

    def test_shares_the_cached_dict_rather_than_copying_it(self, make: Make) -> None:
        _, client = setup(make)
        first = client.search_places(REGION, **OVERTURE).places
        second = client.search_places(REGION, text="alpha", **OVERTURE).places
        # Each hit has its own view, over the one dict the memo holds, so what is inside is the same objects.
        assert any(a.place["locations"] is b.place["locations"] for a in first for b in second)

    def test_returns_a_read_only_place_from_get_place_in_the_cache(self, make: Make) -> None:
        fake, client = setup(make)
        client.search_places(REGION, **OVERTURE)
        before = len(fake.requests)
        found = client.get_place(place_origin("a"))
        assert len(fake.requests) == before  # answered from the cache
        self.assert_read_only(found.place)
        assert client.get_place(place_origin("a")).place["name"] == "Alpha"

    def test_returns_a_read_only_place_from_get_place_on_the_network(self, make: Make) -> None:
        fake, _ = setup(make)
        found = make(fake).get_place(place_origin("a"))  # a cold client: nothing cached
        assert "org.atgeo.getPlace" in fake.requests[-1]
        self.assert_read_only(found.place)
        assert found.place["name"] == "Alpha"

    def test_serialises_through_a_dict_copy_only(self, make: Make) -> None:
        _, client = setup(make)
        hit = client.search_places(REGION, **OVERTURE).places[0]
        with pytest.raises(TypeError):
            json.dumps(hit.place)
        assert json.loads(json.dumps(dict(hit.place))) == hit.place

    def test_compares_equal_to_a_plain_dict_both_ways(self, make: Make) -> None:
        _, client = setup(make)
        hit = client.search_places(REGION, **OVERTURE).places[0]
        plain = dict(hit.place)
        assert hit.place == plain
        assert plain == hit.place
        assert operator.ne(hit.place, plain) is False
        assert operator.ne(plain, hit.place) is False
        assert hit.place != {**plain, "name": "Other"}
        assert {**plain, "name": "Other"} != hit.place
        assert repr(hit.place) == repr(plain)


class TestPlaceAsMapping:
    """What a place does as a dict does, and what it hands back as a plain one."""

    def test_reads_like_a_dict(self, make: Make) -> None:
        _, client = setup(make)
        hit = client.search_places(REGION, **OVERTURE).places[0]
        plain = dict(hit.place)
        assert hit.place.get("name") == plain["name"]
        assert hit.place.get("missing") is None
        assert hit.place.get("missing", 7) == 7
        assert "name" in hit.place
        assert "missing" not in hit.place
        assert 3 not in hit.place  # type: ignore[comparison-overlap]
        assert len(hit.place) == len(plain)
        assert list(hit.place) == list(plain)
        assert list(hit.place.items()) == list(plain.items())
        assert bool(hit.place)

    def test_is_falsy_when_empty(self) -> None:
        empty = _ReadOnlyMapping({})
        assert not empty
        assert len(empty) == 0
        assert empty == {}
        assert empty == {}

    @pytest.mark.parametrize(
        "make_other",
        [types.MappingProxyType, collections.UserDict, collections.OrderedDict, lambda d: collections.ChainMap(d, {})],
        ids=["MappingProxyType", "UserDict", "OrderedDict", "ChainMap"],
    )
    def test_compares_by_contents_with_any_mapping(self, make_other: Any) -> None:
        value = place("a", "Alpha", [point(37.772, -122.412)])
        view = _ReadOnlyMapping(value)
        same, different = make_other(dict(value)), make_other({**value, "name": "Other"})
        assert view == same
        assert same == view
        assert operator.ne(view, same) is False
        assert operator.ne(same, view) is False
        assert view != different
        assert different != view

    def test_compares_by_contents_with_a_view_over_a_non_dict_mapping(self) -> None:
        value = place("a", "Alpha", [point(37.772, -122.412)])
        over_proxy = _ReadOnlyMapping(types.MappingProxyType(value))
        assert over_proxy == value
        assert value == over_proxy
        assert over_proxy == _ReadOnlyMapping(value)
        assert _ReadOnlyMapping(value) == over_proxy
        assert over_proxy != _ReadOnlyMapping({})
        assert over_proxy.get("name") == "Alpha"
        assert "name" in over_proxy
        assert list(over_proxy.items()) == list(value.items())

    @pytest.mark.parametrize("other", [None, [], ["name"], 3, "name"], ids=repr)
    def test_is_not_equal_to_a_non_mapping(self, other: object) -> None:
        view = _ReadOnlyMapping({"name": "x"})
        assert operator.eq(view, other) is False
        assert operator.eq(other, view) is False
        assert operator.ne(view, other) is True
        assert operator.ne(other, view) is True

    def test_is_unhashable(self) -> None:
        with pytest.raises(TypeError):
            hash(_ReadOnlyMapping({"name": "x"}))

    def test_keys_items_and_values_are_the_dicts_own_views(self) -> None:
        value = {"a": 1, "b": [2]}
        view = _ReadOnlyMapping(value)
        assert view.keys() == value.keys()
        assert view.items() == value.items()
        assert list(view.values()) == list(value.values())
        assert type(view.keys()) is type(value.keys())

    def test_copy_methods_give_a_mutable_plain_dict_that_leaves_the_cache_alone(self, make: Make) -> None:
        _, client = setup(make)
        hit = client.search_places(REGION, **OVERTURE).places[0]
        before = copy.deepcopy(dict(hit.place))
        for copied in (hit.place.copy(), copy.copy(hit.place)):  # type: ignore[attr-defined]
            assert type(copied) is dict
            assert copied == hit.place
            copied["name"] = "Changed"
            copied["added"] = 1
            del copied["origin"]
        assert dict(client.search_places(REGION, **OVERTURE).places[0].place) == before
        assert hit.place == before

    def test_a_user_built_hit_or_record_holding_a_plain_dict_works(self) -> None:
        value = place("a", "Alpha", [point(37.772, -122.412)])
        hit = SearchHit(origin=place_origin("a"), place=value, importance=1, distance=0.0, score=None)
        found = PlaceRecord(origin=place_origin("a"), place=value, importance=1)
        for held in (hit, found):
            assert held.name == "Alpha"
            assert held.location.point is not None
            assert held.place is value
            assert pickle.loads(pickle.dumps(held)) == held
            assert copy.deepcopy(held) == held
            assert dataclasses.asdict(held)["place"] == value
        assert hit == SearchHit(
            origin=place_origin("a"), place=_ReadOnlyMapping(value), importance=1, distance=0.0, score=None
        )


def roundtrips(make: Make) -> tuple[SearchResult, PlaceRecord]:
    _, client = setup(make)
    result = client.search_places(REGION, **OVERTURE)
    assert result.places
    return result, client.get_place(place_origin("a"))


class TestPlaceCopies:
    """A place is read-only, but a hit, a place record and a result still copy and pickle like plain data."""

    def test_as_dict_yields_plain_dicts_that_json_can_write(self, make: Make) -> None:
        result, record = roundtrips(make)
        as_dict = dataclasses.asdict(result)
        assert all(type(h["place"]) is dict for h in as_dict["places"])
        assert [h["place"] for h in as_dict["places"]] == [dict(h.place) for h in result.places]
        assert json.loads(json.dumps(as_dict))["places"][0]["place"] == dict(result.places[0].place)
        assert type(dataclasses.asdict(record)["place"]) is dict

    def test_deepcopy_is_equal_and_independent(self, make: Make) -> None:
        result, record = roundtrips(make)
        for original in (result, result.places[0], record):
            copied = copy.deepcopy(original)
            assert copied == original
        hit = result.places[0]
        plain = copy.deepcopy(hit.place)
        assert type(plain) is dict
        plain["locations"].append("changed")
        plain["name"] = "Changed"
        assert "changed" not in hit.place["locations"]
        assert hit.place["name"] != "Changed"
        assert copy.deepcopy(record.place) == record.place

    def test_pickle_round_trips_a_result_a_hit_and_a_record(self, make: Make) -> None:
        result, record = roundtrips(make)
        for original in (result, result.places[0], record):
            for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
                loaded = pickle.loads(pickle.dumps(original, protocol))
                assert loaded == original
        loaded_hit = pickle.loads(pickle.dumps(result.places[0]))
        with pytest.raises(TypeError):
            loaded_hit.place["name"] = "Changed"

    def test_crosses_a_process_boundary(self, make: Make) -> None:
        result, record = roundtrips(make)
        context = multiprocessing.get_context("spawn")
        with concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=context) as pool:
            assert pool.submit(copy.deepcopy, result).result(timeout=60) == result
            assert pool.submit(pickle.loads, pickle.dumps(record)).result(timeout=60) == record
