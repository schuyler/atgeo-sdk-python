"""keyword.test.ts and keywordRegions.test.ts, ported: search_by_keyword's request, hits, attribution and
argument checks (contract §16), and the whole-degree regions of fixtures/keyword-regions.json."""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

import pytest

from atgeo import AtgeoError, Bbox, Circle, CollectionKey, Disclosure, KeywordHit, Point

from .conftest import Make
from .fake_server import FakeServer, FakeTile, FakeTileset, overture
from .helpers import OVERTURE, attribution, fixture, place, place_origin, point, record

DIVISION = "urn:atgeo:overturemaps.org:division:d1"


def tilesets() -> list[FakeTileset]:
    return [
        overture(
            [
                FakeTile(
                    "https://tiles.example/p.json",
                    [
                        record(place("starbucks-3", "Starbucks", [point(37.7, -122.4)]), 5),
                        record(
                            place("starbucks-1", "Starbucks", [point(37.775, -122.418)], {"futureKey": {"a": 1}}), 5
                        ),
                        record(place("starbucks-2", "Starbucks Reserve", [point(37.8, -122.3)]), 9),
                        record(place("mcd", "McDonald's", [point(37.78, -122.41)]), 7),
                    ],
                )
            ]
        ),
        FakeTileset(
            "overture-division",
            [{"dataset": "overturemaps.org", "collection": "division"}],
            [
                FakeTile(
                    "https://tiles.example/d.json",
                    [record(place("d1", "Starbucks District", [point(37.9, -122.5)], {"origin": DIVISION}), 1)],
                )
            ],
        ),
    ]


def setup(make: Make) -> Any:
    return make(FakeServer(tilesets=tilesets()))


def params_of(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query, keep_blank_values=True))


class TestSearchByKeyword:
    def test_returns_the_servers_matches_in_the_servers_order_as_keyword_hits(self, make: Make) -> None:
        result = setup(make).search_by_keyword("starb", dataset="overturemaps.org", collection="place")
        assert [h.origin for h in result.places] == [
            place_origin("starbucks-2"),
            place_origin("starbucks-1"),
            place_origin("starbucks-3"),
        ]
        hit = result.places[1]
        assert isinstance(hit, KeywordHit)
        assert (hit.via, hit.name, hit.importance) == ("keyword", "Starbucks", 5)
        assert hit.collection == CollectionKey(dataset="overturemaps.org", collection="place")
        assert hit.location.point == Point(lat=37.775, lon=-122.418)
        assert hit.location.bbox is None
        assert hit.place["futureKey"] == {"a": 1}  # the value as served, unknown fields included
        assert not hasattr(hit, "distance")
        assert not hasattr(hit, "score")

    def test_gives_a_read_only_place(self, make: Make) -> None:
        hit = setup(make).search_by_keyword("starb", dataset="overturemaps.org").places[0]
        with pytest.raises(TypeError):
            hit.place["name"] = "Changed"

    def test_credits_each_hits_own_collection_and_the_union_of_the_places_attribution(self, make: Make) -> None:
        result = setup(make).search_by_keyword("starb", dataset="overturemaps.org")
        assert [h.collection.collection if h.collection else None for h in result.places] == [
            "place",
            "place",
            "place",
            "division",
        ]
        assert result.attribution == (
            attribution({"dataset": "overturemaps.org", "collection": "division"}),
            attribution({"dataset": "overturemaps.org", "collection": "place"}),
        )

    def test_sends_exactly_dataset_and_q_when_given_no_collection_or_region(self, make: Make) -> None:
        client = setup(make)
        client.search_by_keyword("Starb", dataset="overturemaps.org")
        assert len(client.fake.requests) == 1
        assert "/xrpc/org.atgeo.searchPlaces?" in client.fake.requests[0]
        assert params_of(client.fake.requests[0]) == {"dataset": "overturemaps.org", "q": "Starb"}

    def test_sends_the_collection_and_the_snapped_region_when_given(self, make: Make) -> None:
        client = setup(make)
        client.search_by_keyword("Starb", **OVERTURE, region=Bbox(-122.43, 37.77, -122.41, 37.78))
        assert [params_of(r) for r in client.fake.requests] == [{**OVERTURE, "q": "Starb", "bbox": "-123,37,-122,38"}]

    def test_sends_the_keyword_exactly_as_passed_and_the_server_decodes_it_to_the_same_string(self, make: Make) -> None:
        client = setup(make)
        keyword = "  Star&bucks = +100% #1 "
        client.search_by_keyword(keyword, dataset="overturemaps.org")
        assert params_of(client.fake.requests[0])["q"] == keyword
        assert "q=%2B" not in client.fake.requests[0]
        assert "+100" not in client.fake.requests[0]

    def test_sends_a_keyword_the_server_cannot_use_and_raises_the_servers_keyword_required(self, make: Make) -> None:
        client = setup(make)
        with pytest.raises(AtgeoError) as raised:
            client.search_by_keyword("a", dataset="overturemaps.org")
        assert (raised.value.code, raised.value.status) == ("KeywordRequired", 400)
        assert len(client.fake.requests) == 1

    def test_leaves_a_keyword_over_256_bytes_to_the_server(self, make: Make) -> None:
        client = setup(make)
        with pytest.raises(AtgeoError) as raised:
            client.search_by_keyword("é" * 129, dataset="overturemaps.org")  # 258 bytes of UTF-8
        assert raised.value.code == "InvalidRequest"
        assert len(client.fake.requests) == 1

    def test_is_not_cached(self, make: Make) -> None:
        client = setup(make)
        for _ in range(2):
            client.search_by_keyword("Starb", dataset="overturemaps.org")
        assert len(client.fake.requests) == 2

    def test_leaves_the_tile_cache_alone(self, make: Make) -> None:
        client = setup(make)
        client.search_by_keyword("Starb", dataset="overturemaps.org")
        client.fake.go_offline()
        with pytest.raises(ConnectionError):
            client.get_place(place_origin("starbucks-1"))  # nothing of the answer entered any cache

    def test_returns_an_empty_result_for_no_matches(self, make: Make) -> None:
        result = setup(make).search_by_keyword("Zzzzzz", dataset="overturemaps.org")
        assert (result.places, result.attribution) == ([], ())
        assert result.disclosed == Disclosure(keyword=True, region=None)

    @pytest.mark.parametrize("error", ["DatasetNotFound", "CollectionNotFound"])
    def test_passes_the_servers_codes_through(self, make: Make, error: str) -> None:
        kwargs = (
            {"dataset": "nope"} if error == "DatasetNotFound" else {"dataset": "overturemaps.org", "collection": "x"}
        )
        with pytest.raises(AtgeoError) as raised:
            setup(make).search_by_keyword("Starb", **kwargs)
        assert raised.value.code == error


class TestSearchByKeywordArguments:
    """No request is sent for any of these, and the first fault wins (contract §3)."""

    circle = Circle(0, 0, 100)

    def refuse(self, make: Make, error: type[Exception], message: str, *args: Any, **kwargs: Any) -> None:
        client = setup(make)
        with pytest.raises(error, match=message):
            client.search_by_keyword(*args, **kwargs)
        assert client.fake.requests == []

    def test_refuses_a_dataset_that_is_not_a_string_first(self, make: Make) -> None:
        self.refuse(make, TypeError, "dataset", 5, dataset=None, region=self.circle)

    def test_refuses_a_keyword_that_is_not_a_string_before_the_region(self, make: Make) -> None:
        for keyword in (5, None, b"ab", ["ab"]):
            self.refuse(make, TypeError, "keyword", keyword, dataset="overturemaps.org", region=self.circle)

    def test_refuses_a_circle_region(self, make: Make) -> None:
        self.refuse(make, TypeError, "Bbox", "ab", dataset="overturemaps.org", region=self.circle)

    def test_refuses_a_region_of_another_type(self, make: Make) -> None:
        self.refuse(make, TypeError, "Bbox", "ab", dataset="overturemaps.org", region=(0, 0, 1, 1))

    @pytest.mark.parametrize(
        "bbox",
        [
            Bbox(0, 10, 1, 5),
            Bbox(181, 0, 0, 1),
            Bbox(0, -91, 1, 1),
            Bbox(float("nan"), 0, 1, 1),
            Bbox(0, 0, float("inf"), 1),
        ],
    )
    def test_refuses_a_bbox_that_fails_the_bbox_validation(self, make: Make, bbox: Bbox) -> None:
        self.refuse(make, ValueError, "invalid bbox", "ab", dataset="overturemaps.org", region=bbox)

    def test_treats_a_region_of_none_as_not_given(self, make: Make) -> None:
        client = setup(make)
        client.search_by_keyword("ab", dataset="overturemaps.org", region=None)
        assert "bbox" not in params_of(client.fake.requests[0])

    def test_takes_everything_after_the_keyword_by_name(self, make: Make) -> None:
        client = setup(make)
        with pytest.raises(TypeError):
            client.inner.search_by_keyword("ab", "overturemaps.org")
        assert client.fake.requests == []


REGIONS: list[dict[str, Any]] = fixture("keyword-regions.json")
PLAIN = re.compile(r"^(0|-?[1-9]\d*)(,(0|-?[1-9]\d*)){3}$")


class TestKeywordRegions:
    """fixtures/keyword-regions.json: the whole-degree bbox sent for each input box. The fake refuses a bbox with
    decimals and one the real server calls invalid, so a case passes only if the server accepts what was sent,
    whatever the fixture's arithmetic says."""

    def test_has_every_case(self) -> None:
        assert len(REGIONS) == 72

    @pytest.mark.parametrize("case", REGIONS, ids=[c["name"] for c in REGIONS])
    def test_case(self, make: Make, case: dict[str, Any]) -> None:
        client = make(FakeServer(tilesets=[overture([])]))
        client.search_by_keyword("Zanzibar", **OVERTURE, region=Bbox(*case["bbox"]))
        assert len(client.fake.requests) == 1
        assert params_of(client.fake.requests[0])["bbox"] == case["sent"]
        assert PLAIN.match(case["sent"])

    def test_keeps_every_digit_of_a_precise_box_off_the_wire(self, make: Make) -> None:
        client = make(FakeServer(tilesets=[overture([])]))
        client.search_by_keyword(
            "Zanzibar", **OVERTURE, region=Bbox(-122.4194155, 37.7749295, -122.4094155, 37.7849295)
        )
        assert params_of(client.fake.requests[0])["bbox"] == "-123,37,-122,38"
        for needle in ("7749295", "4194155", "4094155", "7849295"):
            assert needle not in client.fake.requests[0]

    def test_never_writes_a_negative_zero_or_a_float(self, make: Make) -> None:
        client = make(FakeServer(tilesets=[overture([])]))
        client.search_by_keyword("Zanzibar", **OVERTURE, region=Bbox(-0.5, -0.0, 0.0, 0.5))
        assert params_of(client.fake.requests[0])["bbox"] == "-1,0,0,1"

    def test_snaps_integer_inputs_too(self, make: Make) -> None:
        client = make(FakeServer(tilesets=[overture([])]))
        client.search_by_keyword("Zanzibar", **OVERTURE, region=Bbox(-123, 37, -122, 38))
        assert params_of(client.fake.requests[0])["bbox"] == "-123,37,-122,38"
