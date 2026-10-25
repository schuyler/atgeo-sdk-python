"""client.test.ts, ported. The two source-scan suites (retired wire names, universal runtime) are
test_imports.py's job here."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import urllib.parse
from enum import StrEnum
from typing import Any, ClassVar

import pytest

from atgeo import (
    AsyncAtgeoClient,
    AtgeoClient,
    AtgeoError,
    Attribution,
    Bbox,
    Circle,
    CollectionKey,
    PlaceRecord,
    Region,
    SortMode,
)
from atgeo.types import (
    CategoryCount,
    CollectionAttribution,
    CollectionInfo,
    CollectionRef,
    ServedTileset,
    ServiceDescription,
    Tileset,
)

from .conftest import Make
from .fake_server import DEFAULT_SERVER, FakeServer, FakeTile, FakeTileset, overture
from .helpers import (
    OVERTURE,
    assert_immutable,
    attribution,
    bbox_location,
    collection_key,
    place,
    place_origin,
    point,
    record,
    tile_url,
)

REGION = Bbox(-122.42, 37.77, -122.41, 37.78)


def nsid(url: str) -> str:
    return urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]


def query(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


class TestGetPlace:
    value = place(
        "r1",
        "Ferry Building",
        [point(37.7955, -122.3937)],
        {"attributes": {"kind": "landmark"}, "futureKey": {"nested": [1, 2]}},
    )
    origin = place_origin("r1")
    no_importance = place("r2", "No Importance", [point(37.7955, -122.3937)])

    def fake(self) -> FakeServer:
        return FakeServer(
            tilesets=[overture([FakeTile(tile_url("a"), [record(self.value, 80), record(self.no_importance)])])]
        )

    def test_sends_one_get_place_request_with_the_origin_as_uri_on_a_cold_client_and_no_describe_service(
        self, make: Make
    ) -> None:
        fake = self.fake()
        make(fake).get_place(self.origin)
        assert len(fake.requests) == 1
        u = urllib.parse.urlsplit(fake.requests[0])
        assert f"{u.scheme}://{u.netloc}{u.path}" == f"{DEFAULT_SERVER}/xrpc/org.atgeo.getPlace"
        assert query(fake.requests[0])["uri"] == self.origin
        assert "org.atgeo.describeService" not in [nsid(r) for r in fake.requests]

    def test_returns_the_value_unmodified_with_importance_beside_it(self, make: Make) -> None:
        assert make(self.fake()).get_place(self.origin) == PlaceRecord(
            origin=self.origin,
            place=self.value,
            importance=80,
            attribution=(Attribution(dataset="overturemaps.org", collection="place"),),
        )

    def test_returns_a_null_importance_when_the_server_gives_none(self, make: Make) -> None:
        assert make(self.fake()).get_place(place_origin("r2")).importance is None

    def test_returns_the_same_answer_from_the_cache_and_the_network(self, make: Make) -> None:
        fake = self.fake()
        network = make(fake).get_place(self.origin)
        fresh = make(fake)
        fresh.search_places(Bbox(-122.40, 37.79, -122.39, 37.80), **OVERTURE)
        before = len(fake.requests)
        assert fresh.get_place(self.origin) == network
        assert len(fake.requests) == before

    def test_makes_no_request_for_the_origin_of_a_hit_from_an_earlier_search(self, make: Make) -> None:
        fake = self.fake()
        client = make(fake)
        places = client.search_places(Bbox(-122.40, 37.79, -122.39, 37.80), **OVERTURE).places
        before = len(fake.requests)
        origin = places[0].origin
        assert origin is not None
        client.get_place(origin)
        assert len(fake.requests) == before

    def test_resolves_a_hit_identified_by_a_record_uri_from_the_cache(self, make: Make) -> None:
        uri_only = place("u", "By uri", [point(37.775, -122.415)], {"origin": None})
        fake = FakeServer(
            tilesets=[overture([FakeTile(tile_url("a"), [record(uri_only, 4, "https://example.org/r/u")])])]
        )
        client = make(fake)
        places = client.search_places(REGION, dataset="overturemaps.org").places
        assert places[0].origin == "https://example.org/r/u"
        fake.go_offline()
        assert client.get_place("https://example.org/r/u") == PlaceRecord(
            origin="https://example.org/r/u",
            place=uri_only,
            importance=4,
            attribution=(Attribution(dataset="overturemaps.org", collection="place"),),
        )

    # getPlace carries attribution as served (contract §9): from the cache, the attribution of the tile that
    # held the record; from the network, the response's.
    references: ClassVar[list[dict[str, Any]]] = [
        {"dataset": "z.example", "collection": "c"},
        {"dataset": "a.example", "collection": "c", "release": "1"},
        {"dataset": "a.example", "collection": "c", "release": "2"},
    ]
    served = (
        attribution(
            {"dataset": "overturemaps.org", "collection": "place", "licenses": ["https://l.example/1"], "release": "r0"}
        ),
        *[attribution(r) for r in references],
    )

    def with_references(self) -> FakeServer:
        collection = {
            "dataset": "overturemaps.org",
            "collection": "place",
            "licenses": ["https://l.example/1"],
            "release": "r0",
        }
        tileset = FakeTileset(
            "overture-place",
            [collection],
            [FakeTile(tile_url("a"), [record(self.value, 80)])],
            references=self.references,
        )
        return FakeServer(tilesets=[tileset])

    def test_returns_the_tiles_attribution_from_the_cache_in_served_order_duplicates_included_unsorted(
        self, make: Make
    ) -> None:
        fake = self.with_references()
        client = make(fake)
        client.search_places(Bbox(-122.40, 37.79, -122.39, 37.80), **OVERTURE)
        fake.go_offline()
        answer = client.get_place(self.origin)
        assert isinstance(answer.attribution, tuple)
        assert answer.attribution == self.served

    def test_returns_the_responses_attribution_from_the_network_in_served_order_duplicates_included_unsorted(
        self, make: Make
    ) -> None:
        fake = self.with_references()
        answer = make(fake).get_place(self.origin)
        assert any("org.atgeo.getPlace" in r for r in fake.requests)
        assert isinstance(answer.attribution, tuple)
        assert answer.attribution == self.served

    def test_changing_a_get_place_answers_attribution_changes_nothing_cached(self, make: Make) -> None:
        fake = self.with_references()
        client = make(fake)
        region = Bbox(-122.40, 37.79, -122.39, 37.80)
        original = client.search_places(region, **OVERTURE).attribution
        fake.go_offline()
        answer = client.get_place(self.origin)
        assert_immutable(answer.attribution)
        assert client.search_places(region, **OVERTURE).attribution == original
        assert client.get_place(self.origin).attribution == self.served

    def test_changing_a_network_answers_attribution_changes_nothing_cached(self, make: Make) -> None:
        client = make(self.with_references())
        answer = client.get_place(self.origin)
        assert_immutable(answer.attribution)
        assert client.get_place(self.origin).attribution == self.served

    @pytest.mark.parametrize("unknown", [place_origin("nope"), "https://example.org/not-an-origin"])
    def test_rejects_with_place_not_found(self, make: Make, unknown: str) -> None:
        with pytest.raises(AtgeoError) as e:
            make(self.fake()).get_place(unknown)
        assert e.value.code == "PlaceNotFound"


OSM_DESCRIBE = FakeTileset(
    "osm",
    [
        {
            "dataset": "openstreetmap.org",
            "collection": "way",
            "recordCount": 3,
            "categories": [{"value": "cafe", "count": 2}],
        }
    ],
    [],
    references=[
        {
            "dataset": "openstreetmap.org",
            "collection": "node",
            "licenses": ["https://opendatacommons.org/licenses/odbl/"],
        }
    ],
)


class TestDescribeServiceAndDescribeTileset:
    @staticmethod
    def fake() -> FakeServer:
        return FakeServer(tilesets=[overture([]), OSM_DESCRIBE])

    def test_describe_service_returns_the_did_and_the_configured_tilesets(self, make: Make) -> None:
        service = make(self.fake()).describe_service()
        assert service == ServiceDescription(
            did="did:web:places.atgeo.org",
            max_tiles=50,
            tilesets=(
                ServedTileset(
                    slug="overture-place", collections=(CollectionRef(dataset="overturemaps.org", collection="place"),)
                ),
                ServedTileset(slug="osm", collections=(CollectionRef(dataset="openstreetmap.org", collection="way"),)),
            ),
        )
        assert [t.slug for t in service.tilesets] == ["overture-place", "osm"]

    def test_describe_tileset_returns_the_descriptor(self, make: Make) -> None:
        assert make(self.fake()).describe_tileset("osm") == Tileset(
            slug="osm",
            collections=(
                CollectionInfo(
                    dataset="openstreetmap.org",
                    collection="way",
                    record_count=3,
                    categories=(CategoryCount(value="cafe", count=2),),
                ),
            ),
            references=(
                CollectionAttribution(
                    dataset="openstreetmap.org",
                    collection="node",
                    licenses=("https://opendatacommons.org/licenses/odbl/",),
                ),
            ),
        )

    def test_describe_tileset_passes_the_overview_through(self, make: Make) -> None:
        with_overview = FakeTileset("osm", OSM_DESCRIBE.collections, [], references=OSM_DESCRIBE.references)
        with_overview.overview = {"records": 1234}
        tileset = make(FakeServer(tilesets=[with_overview])).describe_tileset("osm")
        assert tileset.overview is not None
        assert tileset.overview.records == 1234
        assert tileset == dataclasses.replace(make(self.fake()).describe_tileset("osm"), overview=tileset.overview)

    def test_describe_tileset_has_no_overview_when_the_tileset_has_none(self, make: Make) -> None:
        assert make(self.fake()).describe_tileset("osm").overview is None

    def test_describe_tileset_asks_the_server_each_time(self, make: Make) -> None:
        fake = self.fake()
        client = make(fake)
        client.describe_tileset("osm")
        client.describe_tileset("osm")
        assert len(fake.requests) == 2

    def test_describe_tileset_rejects_an_unknown_slug_with_tileset_not_found(self, make: Make) -> None:
        with pytest.raises(AtgeoError) as e:
            make(self.fake()).describe_tileset("nope")
        assert e.value.code == "TilesetNotFound"


class TestSearchPlacesResults:
    records: ClassVar[list[dict[str, Any]]] = [
        record(
            place("cafe", "Zanzibar Cafe", [point(0.001, 0)], {"futureKey": {"a": 1}, "variants": [{"name": "ZC"}]}), 2
        ),
        record(place("shop", "Corner Shop", [point(0.002, 0)]), 9),
        record(place("division", "Some Division", [bbox_location(0.01, -0.01, 0.01, -0.01)]), 1),
    ]
    circle = Circle(0, 0, 2000)

    def client(self, make: Make) -> Any:
        return make(FakeServer(tilesets=[overture([FakeTile(tile_url("a"), self.records)])]))

    def test_returns_origin_the_wire_place_importance_meters_distance_and_null_score_without_text(
        self, make: Make
    ) -> None:
        result = self.client(make).search_places(self.circle, **OVERTURE)
        cafe = next(h for h in result.places if h.origin == place_origin("cafe"))
        assert cafe.place == self.records[0]["value"]
        assert "importance" not in cafe.place
        assert not hasattr(cafe, "uri")
        assert cafe.importance == 2
        assert abs(cafe.distance - 111.195) < 0.05
        assert all(h.score is None for h in result.places)

    def test_gives_distance_0_to_a_bbox_containing_the_center(self, make: Make) -> None:
        result = self.client(make).search_places(self.circle, **OVERTURE)
        assert next(h for h in result.places if h.origin == place_origin("division")).distance == 0

    def test_returns_a_numeric_score_with_text(self, make: Make) -> None:
        result = self.client(make).search_places(self.circle, text="Zanzibar Cafe", **OVERTURE)
        cafe = next(h for h in result.places if h.origin == place_origin("cafe"))
        assert cafe.score is not None
        assert abs(cafe.score - 1) < 0.005
        assert all(type(h.score) is float for h in result.places)

    def test_treats_text_that_normalizes_to_empty_as_no_text(self, make: Make) -> None:
        client = self.client(make)
        none = client.search_places(self.circle, **OVERTURE)
        blank = client.search_places(self.circle, text="   ", **OVERTURE)
        assert len(blank.places) > 0
        assert blank == none
        assert all(h.score is None for h in blank.places)

    def test_reads_name_and_location_from_the_place(self, make: Make) -> None:
        # Contract §14's accessors: computed from the place, so they agree with distance.
        result = self.client(make).search_places(self.circle, **OVERTURE)
        cafe = next(h for h in result.places if h.origin == place_origin("cafe"))
        assert cafe.name == "Zanzibar Cafe"
        assert cafe.location.point is not None
        assert (cafe.location.point.lat, cafe.location.point.lon) == (0.001, 0.0)
        assert cafe.location.bbox is None
        division = next(h for h in result.places if h.origin == place_origin("division"))
        assert division.location.bbox is not None
        assert division.location.bbox.west == -0.01


class TestOrderingAmongPlacesContainingTheCenter:
    @staticmethod
    def ids(make: Make, values: list[tuple[dict[str, Any], int]], region: Region, **search: Any) -> list[str]:
        fake = FakeServer(tilesets=[overture([FakeTile(tile_url("a"), [record(v, i) for v, i in values])])])
        places = make(fake).search_places(region, **search, **OVERTURE).places
        return [str(h.origin)[len(place_origin("")) :] for h in places]

    def test_puts_the_smaller_box_first_when_the_center_sits_on_the_antimeridian_seam(self, make: Make) -> None:
        # Centre is exactly (45, 180). The small box reaches it as lon -180 / 180.
        small = place("small", "Small", [bbox_location(50, 40, -170, -180)])
        large = place("large", "Large", [bbox_location(55, 35, -160, 160)])
        order = self.ids(make, [(small, 1), (large, 9)], Bbox(170, 40, -170, 50), sort="distance")
        assert order == ["small", "large"]

    @pytest.mark.parametrize("sort", [SortMode.FUSED, SortMode.SIMILARITY])
    def test_leaves_ties_to_the_origin_under_sort_whatever_the_sizes(self, make: Make, sort: SortMode) -> None:
        large = place("a-large", "Same", [bbox_location(50, 40, 15, 5)])
        small = place("b-small", "Same", [bbox_location(46, 44, 11, 9)])
        order = self.ids(make, [(small, 5), (large, 5)], Circle(45, 10, 1000), text="Same", sort=sort)
        assert order == ["a-large", "b-small"]

    def test_puts_a_point_at_the_center_before_a_more_important_containing_box(self, make: Make) -> None:
        values = [(place("box", "Box", [bbox_location(46, 44, 11, 9)]), 9), (place("pin", "Pin", [point(45, 10)]), 1)]
        assert self.ids(make, values, Circle(45, 10, 1000), sort="distance") == ["pin", "box"]


def osm(kind: str, id: str, lat: float) -> dict[str, Any]:
    return place(id, f"{kind} {id}", [point(lat, -122.415)], {"origin": f"urn:atgeo:openstreetmap.org:{kind}:{id}"})


OSM = FakeTileset(
    "osm",
    [{"dataset": "openstreetmap.org", "collection": c} for c in ("node", "way", "relation")],
    [
        FakeTile(
            tile_url("osm"),
            [
                record(osm("node", "1", 37.771)),
                record(osm("way", "2", 37.772)),
                record(osm("relation", "3", 37.773)),
                record(
                    place("u", "By uri", [point(37.774, -122.415)], {"origin": None}), None, "https://example.org/r/u"
                ),
            ],
        )
    ],
)


class TestDatasetsAndCollections:
    @staticmethod
    def setup(make: Make) -> tuple[FakeServer, Any]:
        fake = FakeServer(tilesets=[overture([]), OSM])
        return fake, make(fake)

    @staticmethod
    def tile_list_requests(fake: FakeServer) -> list[dict[str, str]]:
        return [query(r) for r in fake.requests if "org.atgeo.getTiles" in r]

    def test_keys_the_tile_list_on_dataset_and_collection(self, make: Make) -> None:
        fake, client = self.setup(make)
        client.search_places(REGION, dataset="openstreetmap.org", collection="way")
        client.search_places(REGION, dataset="openstreetmap.org")
        client.search_places(REGION, dataset="overturemaps.org")
        assert [f"{p['dataset']}|{p.get('collection')}" for p in self.tile_list_requests(fake)] == [
            "openstreetmap.org|way",
            "openstreetmap.org|None",
            "overturemaps.org|None",
        ]

    def test_matches_the_collection_not_a_collection_sharing_its_prefix(self, make: Make) -> None:
        fake = FakeServer(
            tilesets=[
                FakeTileset(
                    "osm",
                    [{"dataset": "openstreetmap.org", "collection": c} for c in ("way", "wayside")],
                    [FakeTile(tile_url("osm"), [record(osm("way", "2", 37.772)), record(osm("wayside", "9", 37.773))])],
                )
            ]
        )
        places = make(fake).search_places(REGION, dataset="openstreetmap.org", collection="way").places
        assert [h.origin for h in places] == ["urn:atgeo:openstreetmap.org:way:2"]

    def test_searches_a_whole_dataset_when_only_the_dataset_is_named(self, make: Make) -> None:
        fake, client = self.setup(make)
        places = client.search_places(REGION, dataset="openstreetmap.org").places
        assert sorted(str(h.origin) for h in places) == [
            "https://example.org/r/u",
            "urn:atgeo:openstreetmap.org:node:1",
            "urn:atgeo:openstreetmap.org:relation:3",
            "urn:atgeo:openstreetmap.org:way:2",
        ]
        assert query(fake.requests[0])["dataset"] == "openstreetmap.org"
        assert "collection" not in query(fake.requests[0])

    def test_filters_to_one_collection_dropping_records_with_no_origin(self, make: Make) -> None:
        fake, client = self.setup(make)
        places = client.search_places(REGION, dataset="openstreetmap.org", collection="way").places
        assert [h.origin for h in places] == ["urn:atgeo:openstreetmap.org:way:2"]
        assert query(fake.requests[0])["collection"] == "way"

    def test_rejects_a_search_with_no_dataset_before_any_request_with_a_dataset_message(self, make: Make) -> None:
        fake, client = self.setup(make)
        with pytest.raises(TypeError, match="dataset"):
            client.search_places(REGION)
        assert fake.requests == []

    def test_rejects_a_collection_named_without_a_dataset(self, make: Make) -> None:
        fake, client = self.setup(make)
        with pytest.raises(TypeError, match="dataset"):
            client.search_places(REGION, collection="division")
        assert fake.requests == []

    def test_requires_dataset_in_the_signature_of_both_clients(self) -> None:
        for cls in (AtgeoClient, AsyncAtgeoClient):
            parameter = inspect.signature(cls.search_places).parameters["dataset"]
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
            assert parameter.default is inspect.Parameter.empty

    def test_requires_dataset_in_the_type(self) -> None:
        # mypy reports an unused ignore here if dataset ever becomes optional again.
        client = AtgeoClient(transport=FakeServer().transport)
        with pytest.raises(TypeError, match="dataset"):
            client.search_places(REGION)  # type: ignore[call-arg]

    def test_raises_for_a_missing_dataset_when_the_async_client_is_called_not_awaited(self) -> None:
        fake = FakeServer()
        search: Any = AsyncAtgeoClient(transport=fake.async_transport).search_places
        error: TypeError | None = None
        try:
            search(REGION).close()  # a coroutine, if the call did not raise; closed so that none is left unawaited
        except TypeError as e:  # raised by the call itself, as a missing argument is
            error = e
        assert error is not None
        assert "dataset" in str(error)
        assert fake.requests == []

    def test_reports_a_missing_dataset_before_an_invalid_region_or_sort(self, make: Make) -> None:
        fake, client = self.setup(make)
        for region, kwargs in (
            (Bbox(0, 10, 1, 5), {}),  # south > north
            (Circle(0, 0, -1), {}),
            (REGION, {"sort": "similarity"}),  # no text
            (REGION, {"sort": "nearest"}),
            ((0, 0, 1, 1), {}),  # not a region at all
        ):
            for dataset in ({}, {"dataset": None}):
                with pytest.raises(TypeError, match="dataset"):
                    client.search_places(region, **dataset, **kwargs)
        assert fake.requests == []

    @pytest.mark.parametrize("dataset", [None, 5, 1.5, True, ["overturemaps.org"], b"overturemaps.org", {}])
    @pytest.mark.parametrize("with_collection", [False, True])
    def test_treats_a_dataset_that_is_not_a_string_as_missing(
        self, make: Make, dataset: object, with_collection: bool
    ) -> None:
        fake, client = self.setup(make)
        extra = {"collection": "place"} if with_collection else {}
        with pytest.raises(TypeError, match="dataset"):
            client.search_places(REGION, dataset=dataset, **extra)
        assert fake.requests == []

    def test_takes_a_str_subclass_for_a_dataset(self, make: Make) -> None:
        class Dataset(StrEnum):
            OSM = "openstreetmap.org"

        fake, client = self.setup(make)
        places = client.search_places(REGION, dataset=Dataset.OSM).places
        assert len(places) == 4
        assert query(fake.requests[0])["dataset"] == "openstreetmap.org"

    # Guard: an empty dataset is a string and goes to the server, which is the authority on what exists.
    def test_sends_an_empty_dataset_to_the_server(self, make: Make) -> None:
        fake, client = self.setup(make)
        with pytest.raises(AtgeoError):
            client.search_places(REGION, dataset="")
        lists = [r for r in fake.requests if "org.atgeo.getTiles" in r]
        assert len(lists) == 1
        assert urllib.parse.parse_qs(urllib.parse.urlsplit(lists[0]).query, keep_blank_values=True)["dataset"] == [""]

    def test_gives_each_hit_the_collection_its_origin_names_by_prefix_and_none_to_a_hit_keyed_by_uri(
        self, make: Make
    ) -> None:
        _, client = self.setup(make)
        places = client.search_places(REGION, dataset="openstreetmap.org").places
        by_origin = {h.origin: h.collection for h in places}
        assert by_origin == {
            "urn:atgeo:openstreetmap.org:node:1": CollectionKey(dataset="openstreetmap.org", collection="node"),
            "urn:atgeo:openstreetmap.org:way:2": CollectionKey(dataset="openstreetmap.org", collection="way"),
            "urn:atgeo:openstreetmap.org:relation:3": CollectionKey(dataset="openstreetmap.org", collection="relation"),
            "https://example.org/r/u": None,
        }
        assert all(type(c) is CollectionKey for c in by_origin.values() if c is not None)
        assert all(hasattr(h, "collection") for h in places)

    def test_gives_way_and_wayside_hits_their_own_collections(self, make: Make) -> None:
        fake = FakeServer(
            tilesets=[
                FakeTileset(
                    "osm",
                    [{"dataset": "openstreetmap.org", "collection": c} for c in ("way", "wayside")],
                    [FakeTile(tile_url("osm"), [record(osm("way", "2", 37.772)), record(osm("wayside", "9", 37.773))])],
                )
            ]
        )
        places = make(fake).search_places(REGION, dataset="openstreetmap.org").places
        assert {h.origin: h.collection for h in places} == {
            "urn:atgeo:openstreetmap.org:way:2": collection_key({"dataset": "openstreetmap.org", "collection": "way"}),
            "urn:atgeo:openstreetmap.org:wayside:9": collection_key(
                {"dataset": "openstreetmap.org", "collection": "wayside"}
            ),
        }

    # Both the §4.7 filter and a hit's collection test value.origin with startswith and nothing else (contract §8).
    @staticmethod
    def osm_tileset(*records: dict[str, Any]) -> FakeServer:
        collections = [{"dataset": "openstreetmap.org", "collection": c} for c in ("node", "way")]
        return FakeServer(tilesets=[FakeTileset("osm", collections, [FakeTile(tile_url("osm"), list(records))])])

    def test_filters_on_an_origin_that_starts_with_the_prefix_not_one_that_contains_it(self, make: Make) -> None:
        inside = osm("way", "1", 37.771)
        mid = osm("way", "2", 37.772) | {"origin": "x:urn:atgeo:openstreetmap.org:way:2"}
        fake = self.osm_tileset(record(inside), record(mid))
        places = make(fake).search_places(REGION, dataset="openstreetmap.org", collection="way").places
        assert [h.origin for h in places] == ["urn:atgeo:openstreetmap.org:way:1"]

    def test_gives_an_origin_that_merely_contains_the_prefix_no_collection(self, make: Make) -> None:
        mid = osm("way", "2", 37.772) | {"origin": "x:urn:atgeo:openstreetmap.org:way:2"}
        places = make(self.osm_tileset(record(mid))).search_places(REGION, dataset="openstreetmap.org").places
        assert [(h.origin, h.collection) for h in places] == [("x:urn:atgeo:openstreetmap.org:way:2", None)]

    def test_never_reads_a_collection_from_the_uri_fallback(self, make: Make) -> None:
        # No origin, and a uri that would match the prefix: the record is keyed by the uri, and has no collection,
        # and a collection filter drops it, as it drops every record with no origin.
        by_uri = place("u", "By uri", [point(37.774, -122.415)], {"origin": None})
        uri = "urn:atgeo:openstreetmap.org:way:9"
        fake = self.osm_tileset(record(by_uri, None, uri))
        client = make(fake)
        places = client.search_places(REGION, dataset="openstreetmap.org").places
        assert [(h.origin, h.collection) for h in places] == [(uri, None)]
        assert client.search_places(REGION, dataset="openstreetmap.org", collection="way").places == []

    def test_gives_a_hit_a_fresh_pair_not_the_attribution_entry(self, make: Make) -> None:
        fake = self.osm_tileset(record(osm("way", "1", 37.771)))
        (hit,) = make(fake).search_places(REGION, dataset="openstreetmap.org").places
        assert hit.collection == CollectionKey(dataset="openstreetmap.org", collection="way")
        assert type(hit.collection) is CollectionKey  # the entry, with its release and licenses, is another type
        assert {f.name for f in dataclasses.fields(hit.collection)} == {"dataset", "collection"}

    def test_returns_the_union_of_the_tiles_attribution_references_included_sorted(self, make: Make) -> None:
        odbl = "https://opendatacommons.org/licenses/odbl/"
        fake = FakeServer(
            tilesets=[
                FakeTileset(
                    "osm",
                    [
                        {
                            "dataset": "openstreetmap.org",
                            "collection": "way",
                            "release": "2026-01-01",
                            "licenses": [odbl],
                        },
                        {"dataset": "openstreetmap.org", "collection": "relation"},
                    ],
                    [FakeTile(tile_url("osm"), [record(osm("way", "2", 37.772))])],
                    references=[
                        {
                            "dataset": "openstreetmap.org",
                            "collection": "node",
                            "source": "https://www.openstreetmap.org",
                        }
                    ],
                )
            ]
        )
        result = make(fake).search_places(REGION, dataset="openstreetmap.org", collection="way")
        assert result.attribution == (
            Attribution(dataset="openstreetmap.org", collection="node", source="https://www.openstreetmap.org"),
            Attribution(dataset="openstreetmap.org", collection="relation"),
            Attribution(dataset="openstreetmap.org", collection="way", release="2026-01-01", licenses=(odbl,)),
        )

    def test_gives_an_empty_attribution_tuple_when_no_tile_was_used(self, make: Make) -> None:
        _, client = self.setup(make)
        result = client.search_places(REGION, dataset="overturemaps.org")  # a tileset with no tiles
        assert result.tiles.fetched == 0
        assert result.attribution == ()
        assert isinstance(result.attribution, tuple)


class TestConstruction:
    """No reference counterpart: what the Python constructors check."""

    @pytest.mark.parametrize(
        "server", ["places.atgeo.org", "127.0.0.1:8000/ok", "ftp://places.atgeo.org", "https://", ""]
    )
    def test_refuses_a_server_that_is_not_an_absolute_http_url(self, make: Make, server: str) -> None:
        with pytest.raises(ValueError, match="absolute http or https URL"):
            make(FakeServer(), server=server)

    def test_takes_an_int_like_cache_budget(self, make: Make) -> None:
        class IntLike:
            def __index__(self) -> int:
                return 1024

        assert make(FakeServer(), cache_budget_bytes=IntLike()).search_places(REGION, **OVERTURE).complete

    @pytest.mark.parametrize("budget", [None, "52428800", 1.5, True])
    def test_refuses_a_cache_budget_that_is_not_an_int(self, make: Make, budget: object) -> None:
        # Unchecked, it failed inside every tile store: once each tile counted as failed and every search came
        # back empty; now every search would fail with an error from deep in the cache.
        with pytest.raises(TypeError, match="cache_budget_bytes"):
            make(FakeServer(), cache_budget_bytes=budget)

    def test_refuses_a_negative_cache_budget(self, make: Make) -> None:
        with pytest.raises(ValueError, match="cache_budget_bytes"):
            make(FakeServer(), cache_budget_bytes=-1)

    def test_takes_a_zero_cache_budget(self, make: Make) -> None:
        result = make(FakeServer(), cache_budget_bytes=0).search_places(REGION, **OVERTURE)
        assert result.complete

    def test_strips_trailing_slashes_from_the_server(self, make: Make) -> None:
        fake = FakeServer(server="http://localhost:8000")
        make(fake, server="http://localhost:8000///").describe_service()
        assert fake.requests == ["http://localhost:8000/xrpc/org.atgeo.describeService?"]

    def test_takes_arguments_after_the_region_by_keyword_only(self, make: Make) -> None:
        client = make(FakeServer())
        with pytest.raises(TypeError):
            client.inner.search_places(REGION, "Starbux", **OVERTURE)  # type: ignore[call-arg]


SMALL_STACK = """
import asyncio, sys, threading
from atgeo import AsyncAtgeoClient, AtgeoClient, AtgeoDecodingError, HttpResponse

body = b"[" * 8000 + b"]" * 8000

class Sync:
    def get(self, url):
        return HttpResponse(int(sys.argv[2]), body)

class Async:
    async def get(self, url):
        return HttpResponse(int(sys.argv[2]), body)

def run():
    calls = {
        "get_place": lambda c: c.get_place("urn:x:y:z:1"),
        "describe_service": lambda c: c.describe_service(),
        "describe_tileset": lambda c: c.describe_tileset("s"),
    }
    for name, call in calls.items():
        try:
            if sys.argv[1] == "sync":
                call(AtgeoClient(transport=Sync()))
            else:
                asyncio.run(call(AsyncAtgeoClient(transport=Async())))
        except Exception as e:
            print(name, type(e).__name__)

threading.stack_size(1 << 20)  # the official Alpine images' default
t = threading.Thread(target=run)
t.start()
t.join()
"""


class TestResponsesParsedOffTheCallersThread:
    """A deeply nested body can crash CPython 3.12 and 3.13 on a small stack, so the clients parse every body
    on threads of their own, with 8 MiB stacks; here the app's thread has 1 MiB. Run in a child process, so
    that a regression crashes the child, not the test session."""

    @pytest.mark.parametrize("status", [200, 500])
    @pytest.mark.parametrize("kind", ["sync", "async"])
    def test_raises_for_deep_json_instead_of_crashing(self, kind: str, status: int) -> None:
        import subprocess  # noqa: PLC0415
        import sys  # noqa: PLC0415

        out = subprocess.run(
            [sys.executable, "-c", SMALL_STACK, kind, str(status)],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert out.returncode == 0, out.stderr
        error = "AtgeoDecodingError" if status == 200 else "AtgeoError"
        assert out.stdout.split() == [
            w for name in ("get_place", "describe_service", "describe_tileset") for w in (name, error)
        ]


class TestClosing:
    """No reference counterpart: a closed async client, or its transport, refuses every call."""

    def test_refuses_every_call_after_aclose_with_the_default_transport(self) -> None:

        from atgeo import AsyncAtgeoClient  # noqa: PLC0415

        async def main() -> None:
            client = AsyncAtgeoClient("http://127.0.0.1:9")
            await client.aclose()
            await client.aclose()  # closing twice is harmless
            for call in (
                lambda: client.search_places(REGION, **OVERTURE),
                lambda: client.get_place("urn:x"),
                client.describe_service,
                lambda: client.describe_tileset("s"),
            ):
                with pytest.raises(RuntimeError, match="is closed"):
                    await call()
            # The transport made no session that nobody would close.
            assert client._transport._session is None  # type: ignore[attr-defined]

        asyncio.run(main())

    def test_refuses_a_search_whose_tile_lists_are_cached_after_aclose(self) -> None:
        # Such a search sends only tile requests: without the client's own check, the closed transport would
        # fail each one, and the search would return no places, complete=False, and no error.
        from .loopback import Loopback, Request, Writer  # noqa: PLC0415

        base = ""

        def handle(r: Request, w: Writer) -> None:
            if "getTiles" in r.target:
                w.respond(200, json.dumps({"tiles": [f"{base}/tiles/a.json"]}).encode())
            else:
                w.respond(200, b'{"records":[]}')

        with Loopback(handler=handle) as server:
            base = server.url("")

            async def main() -> None:
                client = AsyncAtgeoClient(base)
                assert (await client.search_places(REGION, **OVERTURE)).complete
                await client.aclose()
                with pytest.raises(RuntimeError, match="is closed"):
                    await client.search_places(REGION, **OVERTURE)

            asyncio.run(main())
            assert sum("getTiles" in r.target for r in server.requests) == 1

    def test_a_closed_aiohttp_transport_refuses_requests(self) -> None:

        from atgeo import AiohttpTransport  # noqa: PLC0415

        async def main() -> None:
            transport = AiohttpTransport()
            await transport.aclose()
            with pytest.raises(RuntimeError, match="is closed"):
                await transport.get("http://127.0.0.1:9/")

        asyncio.run(main())

    def test_leaves_a_client_with_its_own_transport_usable_after_aclose(self, make: Make) -> None:
        client = make(FakeServer())
        if client.kind == "sync":
            client.inner.close()  # type: ignore[union-attr]
        else:
            asyncio.run(client.inner.aclose())  # type: ignore[union-attr]
        assert client.search_places(REGION, **OVERTURE).complete


class TestEventLoopAffinity:
    def test_refuses_a_second_event_loop_before_any_request_with_the_default_transport(self) -> None:

        from atgeo import AsyncAtgeoClient  # noqa: PLC0415

        from .loopback import Loopback, Request, Writer  # noqa: PLC0415

        def handle(_r: Request, w: Writer) -> None:
            w.respond(404, b"", [("Connection", "close")])
            w.close()

        with Loopback(handler=handle) as server:
            client = AsyncAtgeoClient(server.url(""))
            with pytest.raises(AtgeoError):
                asyncio.run(client.describe_tileset("s"))
            for call in (
                lambda: client.search_places(REGION, **OVERTURE),
                lambda: client.get_place("urn:x"),
                client.describe_service,
            ):
                with pytest.raises(RuntimeError, match="one client per event loop"):
                    asyncio.run(call())
            assert len(server.requests) == 1
            asyncio.run(client.aclose())
