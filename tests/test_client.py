"""client.test.ts, ported. The two source-scan suites (retired wire names, universal runtime) are
test_imports.py's job here."""

from __future__ import annotations

import urllib.parse
from typing import Any, ClassVar

import pytest

from atgeo import AtgeoError, Bbox, Circle, PlaceRecord, Region, SortMode
from atgeo.types import CollectionRef, ServedTileset, ServiceDescription

from .conftest import Make
from .fake_server import DEFAULT_SERVER, FakeServer, FakeTile, FakeTileset, overture
from .helpers import bbox_location, place, place_origin, point, record, tile_url

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
            origin=self.origin, place=self.value, importance=80
        )

    def test_returns_a_null_importance_when_the_server_gives_none(self, make: Make) -> None:
        assert make(self.fake()).get_place(place_origin("r2")).importance is None

    def test_returns_the_same_answer_from_the_cache_and_the_network(self, make: Make) -> None:
        fake = self.fake()
        network = make(fake).get_place(self.origin)
        fresh = make(fake)
        fresh.search_places(Bbox(-122.40, 37.79, -122.39, 37.80))
        before = len(fake.requests)
        assert fresh.get_place(self.origin) == network
        assert len(fake.requests) == before

    def test_makes_no_request_for_the_origin_of_a_hit_from_an_earlier_search(self, make: Make) -> None:
        fake = self.fake()
        client = make(fake)
        places = client.search_places(Bbox(-122.40, 37.79, -122.39, 37.80)).places
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
            origin="https://example.org/r/u", place=uri_only, importance=4
        )

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
            tilesets=(
                ServedTileset(
                    slug="overture-place", collections=(CollectionRef(dataset="overturemaps.org", collection="place"),)
                ),
                ServedTileset(slug="osm", collections=(CollectionRef(dataset="openstreetmap.org", collection="way"),)),
            ),
        )
        assert [t.slug for t in service.tilesets] == ["overture-place", "osm"]

    def test_describe_tileset_returns_the_descriptor(self, make: Make) -> None:
        tileset = make(self.fake()).describe_tileset("osm")
        assert tileset.slug == "osm"
        (info,) = tileset.collections
        assert (info.dataset, info.collection, info.record_count) == ("openstreetmap.org", "way", 3)
        assert info.categories is not None
        assert [(c.value, c.count) for c in info.categories] == [("cafe", 2)]
        assert tileset.references is not None
        (ref,) = tileset.references
        assert (ref.dataset, ref.collection, ref.licenses) == (
            "openstreetmap.org",
            "node",
            ("https://opendatacommons.org/licenses/odbl/",),
        )

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
        result = self.client(make).search_places(self.circle)
        cafe = next(h for h in result.places if h.origin == place_origin("cafe"))
        assert cafe.place == self.records[0]["value"]
        assert "importance" not in cafe.place
        assert not hasattr(cafe, "uri")
        assert cafe.importance == 2
        assert abs(cafe.distance - 111.195) < 0.05
        assert all(h.score is None for h in result.places)

    def test_gives_distance_0_to_a_bbox_containing_the_center(self, make: Make) -> None:
        result = self.client(make).search_places(self.circle)
        assert next(h for h in result.places if h.origin == place_origin("division")).distance == 0

    def test_returns_a_numeric_score_with_text(self, make: Make) -> None:
        result = self.client(make).search_places(self.circle, text="Zanzibar Cafe")
        cafe = next(h for h in result.places if h.origin == place_origin("cafe"))
        assert cafe.score is not None
        assert abs(cafe.score - 1) < 0.005
        assert all(type(h.score) is float for h in result.places)

    def test_treats_text_that_normalizes_to_empty_as_no_text(self, make: Make) -> None:
        client = self.client(make)
        none = client.search_places(self.circle)
        blank = client.search_places(self.circle, text="   ")
        assert len(blank.places) > 0
        assert blank == none
        assert all(h.score is None for h in blank.places)

    def test_reads_name_and_location_from_the_place(self, make: Make) -> None:
        # Contract §14's accessors: computed from the place, so they agree with distance.
        result = self.client(make).search_places(self.circle)
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
        places = make(fake).search_places(region, **search).places
        return [str(h.origin)[len(place_origin("")) :] for h in places]

    def test_puts_the_smaller_box_first_when_the_center_sits_on_the_antimeridian_seam(self, make: Make) -> None:
        # Centre is exactly (45, 180). The small box reaches it as lon -180 / 180.
        small = place("small", "Small", [bbox_location(50, 40, -170, -180)])
        large = place("large", "Large", [bbox_location(55, 35, -160, 160)])
        order = self.ids(make, [(small, 1), (large, 9)], Bbox(170, 40, -170, 50), sort="distance")
        assert order == ["small", "large"]

    @pytest.mark.parametrize("sort", [SortMode.FUSED, SortMode.SIMILARITY])
    def test_leaves_ties_to_the_origin_whatever_the_sizes(self, make: Make, sort: SortMode) -> None:
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

    def test_defaults_to_overturemaps_org_place_when_nothing_is_named(self, make: Make) -> None:
        fake, client = self.setup(make)
        client.search_places(REGION)
        assert query(fake.requests[0])["dataset"] == "overturemaps.org"
        assert query(fake.requests[0])["collection"] == "place"

    def test_takes_overturemaps_org_for_a_collection_named_alone(self, make: Make) -> None:
        fake, client = self.setup(make)
        with pytest.raises(AtgeoError):
            client.search_places(REGION, collection="division")
        assert query(fake.requests[0])["dataset"] == "overturemaps.org"
        assert query(fake.requests[0])["collection"] == "division"


class TestConstruction:
    """No reference counterpart: what the Python constructors check."""

    @pytest.mark.parametrize(
        "server", ["places.atgeo.org", "127.0.0.1:8000/ok", "ftp://places.atgeo.org", "https://", ""]
    )
    def test_refuses_a_server_that_is_not_an_absolute_http_url(self, make: Make, server: str) -> None:
        with pytest.raises(ValueError, match="absolute http or https URL"):
            make(FakeServer(), server=server)

    def test_strips_trailing_slashes_from_the_server(self, make: Make) -> None:
        fake = FakeServer(server="http://localhost:8000")
        make(fake, server="http://localhost:8000///").describe_service()
        assert fake.requests == ["http://localhost:8000/xrpc/org.atgeo.describeService?"]

    def test_takes_arguments_after_the_region_by_keyword_only(self, make: Make) -> None:
        client = make(FakeServer())
        with pytest.raises(TypeError):
            client.inner.search_places(REGION, "Starbux")  # type: ignore[call-arg]
