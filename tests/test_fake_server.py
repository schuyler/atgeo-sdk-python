from __future__ import annotations

import json
import urllib.parse
from typing import Any, ClassVar

import pytest

from .fake_server import FakeServer, FakeTile, FakeTileset, overture
from .helpers import place, place_origin, point, record

TILE = "https://tiles.example/a.json"
OK = place("a", "Alpha", [point(37.772, -122.412)])


def build(records: list[Any], slug: Any = "overture-place", **tileset_fields: Any) -> FakeServer:
    tileset = overture([FakeTile(TILE, records)])
    return FakeServer(tilesets=[FakeTileset(slug, tileset.collections, tileset.tiles, **tileset_fields)])


class TestFakeServerConfiguration:
    def test_accepts_a_valid_configuration(self) -> None:
        build([record(OK, 3)])

    def test_rejects_a_record_value_missing_its_name(self) -> None:
        nameless = {k: v for k, v in OK.items() if k != "name"}
        with pytest.raises(ValueError, match="name"):
            build([record(nameless)])

    def test_rejects_a_tile_record_missing_its_value(self) -> None:
        with pytest.raises(ValueError, match='"value"'):
            build([{"importance": 3}])

    def test_rejects_a_float_importance(self) -> None:
        with pytest.raises(ValueError, match="integer"):
            build([record(OK, 0.5)])

    def test_rejects_a_descriptor_missing_its_slug(self) -> None:
        with pytest.raises(ValueError, match="slug"):
            build([], slug=None)


class TestFakeServerRequests:
    @staticmethod
    def get(fake: FakeServer, path: str) -> tuple[int, Any]:
        response = fake.transport.get(f"https://places.atgeo.org{path}")
        return response.status, json.loads(response.body)

    def test_answers_400_invalid_request_to_get_tiles_without_a_dataset(self) -> None:
        status, body = self.get(build([record(OK)]), "/xrpc/org.atgeo.getTiles?bbox=-122.42,37.77,-122.41,37.78")
        assert status == 400
        assert body["error"] == "InvalidRequest"

    def test_answers_400_invalid_request_to_get_place_with_a_non_uri_uri(self) -> None:
        status, _ = self.get(build([record(OK)]), "/xrpc/org.atgeo.getPlace?uri=not%20a%20uri")
        assert status == 400

    def test_serves_tile_bodies_with_published_at_attribution_and_records(self) -> None:
        body = json.loads(build([record(OK, 3)]).transport.get(TILE).body)
        assert sorted(body) == ["attribution", "publishedAt", "records"]
        assert body["records"] == [{"value": OK, "importance": 3}]

    def test_answers_get_place_with_exactly_uri_url_importance_value_and_attribution(self) -> None:
        origin = OK["origin"]
        path = f"/xrpc/org.atgeo.getPlace?uri={urllib.parse.quote(origin, safe='')}"
        _, body = self.get(build([record(OK, 3)]), path)
        assert sorted(body) == ["attribution", "importance", "uri", "url", "value"]
        assert body["uri"] == origin
        assert body["value"] == OK
        assert body["importance"] == 3
        assert body["attribution"] == [{"dataset": "overturemaps.org", "collection": "place"}]
        _, bare = self.get(build([record(OK)]), path)
        assert sorted(bare) == ["attribution", "uri", "url", "value"]

    def test_answers_get_place_with_the_tilesets_collections_then_its_references(self) -> None:
        references = [{"dataset": "z.example", "collection": "c"}, {"dataset": "a.example", "collection": "c"}]
        fake = build([record(OK)], references=references)
        _, body = self.get(fake, f"/xrpc/org.atgeo.getPlace?uri={urllib.parse.quote(OK['origin'], safe='')}")
        assert body["attribution"] == [{"dataset": "overturemaps.org", "collection": "place"}, *references]
        assert json.loads(fake.transport.get(TILE).body)["attribution"] == body["attribution"]

    def test_emits_a_tilesets_overview_in_its_descriptor(self) -> None:
        _, body = self.get(build([], overview={"records": 7}), "/xrpc/org.atgeo.describeTileset?slug=overture-place")
        assert body["overview"] == {"records": 7}
        _, bare = self.get(build([]), "/xrpc/org.atgeo.describeTileset?slug=overture-place")
        assert "overview" not in bare

    def test_answers_describe_service_with_a_did_and_tilesets(self) -> None:
        _, body = self.get(build([]), "/xrpc/org.atgeo.describeService")
        assert body == {
            "did": "did:web:places.atgeo.org",
            "maxTiles": 50,
            "tilesets": [
                {"slug": "overture-place", "collections": [{"dataset": "overturemaps.org", "collection": "place"}]}
            ],
        }

    # Beyond the reference: the checks the contract's minimum asks of a validator-less fake (§13).
    @pytest.mark.parametrize(
        "path",
        [
            "/xrpc/org.atgeo.getTiles?dataset=d&bbox=1,2,3,4",
            "/xrpc/org.atgeo.getTiles?dataset=d&bbox=1.00,2.00,3.00,4.00&extra=1",
            "/xrpc/org.atgeo.describeTileset",
            "/xrpc/org.atgeo.getPlace?uri=/relative",
        ],
    )
    def test_answers_400_invalid_request_to_a_malformed_request(self, path: str) -> None:
        status, body = self.get(build([]), path)
        assert (status, body["error"]) == (400, "InvalidRequest")

    def test_records_a_request_and_fails_it_when_offline(self) -> None:
        fake = build([])
        fake.go_offline()
        with pytest.raises(ConnectionError):
            fake.transport.get(TILE)
        assert fake.requests == [TILE]


class TestFakeServerGetTilesPrecision:
    """The server refuses a coordinate finer than 0.01 degrees; the contract's minimum (§13) is that the fake
    accepts only the two-decimal form the client sends."""

    @pytest.mark.parametrize("bbox", ["-122.4194,37.77,-122.41,37.78", "-122.42,37.77,-122.41,37.7801"])
    def test_answers_400_bbox_too_precise(self, bbox: str) -> None:
        status, body = TestFakeServerRequests.get(
            build([record(OK)]), f"/xrpc/org.atgeo.getTiles?dataset=overturemaps.org&bbox={bbox}"
        )
        assert (status, body["error"]) == (400, "BboxTooPrecise")

    @pytest.mark.parametrize("bbox", ["-122.4,37.77,-122.41,37.78", "-122,37.77,-122.41,37.78"])
    def test_answers_400_invalid_request_to_a_form_the_client_never_sends(self, bbox: str) -> None:
        status, body = TestFakeServerRequests.get(
            build([record(OK)]), f"/xrpc/org.atgeo.getTiles?dataset=overturemaps.org&bbox={bbox}"
        )
        assert (status, body["error"]) == (400, "InvalidRequest")


class TestFakeServerSearchPlaces:
    records: ClassVar[list[Any]] = [
        record(place("a", "Starbucks", [point(37.772, -122.412)]), 5),
        record(place("b", "McDonald's", [point(37.773, -122.413)], {"variants": [{"name": "Mickey D's"}]}), 9),
    ]

    def search(self, query: str) -> tuple[int, Any]:
        return TestFakeServerRequests.get(build(self.records), f"/xrpc/org.atgeo.searchPlaces?{query}")

    base = "dataset=overturemaps.org&q=star"

    def test_answers_place_views_matching_each_word_as_a_prefix_of_a_word_of_one_name(self) -> None:
        assert [p["uri"] for p in self.search(self.base)[1]["places"]] == [place_origin("a")]
        for q in ("mcdonalds", "mickey", "D%27s%20mick"):
            assert len(self.search(f"dataset=overturemaps.org&q={q}")[1]["places"]) == 1
        assert self.search("dataset=overturemaps.org&q=mcd%20starb")[1]["places"] == []

    def test_answers_the_whole_place_view(self) -> None:
        view = self.search(self.base)[1]["places"][0]
        assert sorted(view) == ["attribution", "importance", "uri", "url", "value"]

    @pytest.mark.parametrize("q", ["a", "%ED%95%98", "%E0%A4%95%E0%A4%BE", "a%20b%27", "%20%20"])
    def test_answers_400_keyword_required(self, q: str) -> None:
        status, body = self.search(f"dataset=overturemaps.org&q={q}")
        assert (status, body["error"]) == (400, "KeywordRequired")

    @pytest.mark.parametrize(
        ("bbox", "error"),
        [
            ("-76.0,39,-75,40", "BboxTooPrecise"),
            ("-76,39,-75,40.5", "BboxTooPrecise"),
            ("1e1,39,20,40", "BboxTooPrecise"),
            ("-76,39,-75", "InvalidBbox"),
            ("a,39,-75,40", "InvalidBbox"),
            ("-76,40,-75,40", "InvalidBbox"),
            ("-75,39,-76,40", "InvalidBbox"),
            ("10,39,10,40", "InvalidBbox"),
            ("180,39,-180,40", "InvalidBbox"),
            ("0,39,-1,40", "InvalidBbox"),
            ("-181,39,-75,40", "InvalidBbox"),
            ("-76,39,-75,91", "InvalidBbox"),
        ],
    )
    def test_answers_400_for_a_bbox_the_server_refuses(self, bbox: str, error: str) -> None:
        assert self.search(f"{self.base}&bbox={bbox}")[1]["error"] == error

    @pytest.mark.parametrize("bbox", ["-76,39,-75,40", "76.,39,77,40", "170,39,-170,40", "-180,39,180,40"])
    def test_accepts_a_bbox_the_server_accepts(self, bbox: str) -> None:
        assert self.search(f"{self.base}&bbox={bbox}")[0] == 200

    def test_checks_the_precision_then_the_bbox_then_the_dataset_then_the_keyword(self) -> None:
        assert self.search("dataset=nope&q=a&bbox=1.5,2,1,3")[1]["error"] == "BboxTooPrecise"
        assert self.search("dataset=nope&q=a&bbox=2,2,1,3")[1]["error"] == "InvalidBbox"
        assert self.search("dataset=nope&q=a")[1]["error"] == "DatasetNotFound"
        assert self.search("dataset=overturemaps.org&collection=nope&q=a")[1]["error"] == "CollectionNotFound"
        assert self.search("dataset=overturemaps.org&q=a")[1]["error"] == "KeywordRequired"

    @pytest.mark.parametrize(
        "query",
        ["q=ab", "dataset=overturemaps.org", "dataset=overturemaps.org&q=ab&extra=1", f"dataset=d&q={'x' * 257}"],
    )
    def test_answers_400_invalid_request_to_a_malformed_request(self, query: str) -> None:
        status, body = self.search(query)
        assert (status, body["error"]) == (400, "InvalidRequest")

    def test_orders_by_importance_then_origin(self) -> None:
        records = [
            record(place("b", "Starbucks", [point(1, 1)]), 5),
            record(place("a", "Starbucks", [point(1, 1)]), 5),
            record(place("c", "Starbucks", [point(1, 1)]), 9),
        ]
        _, body = TestFakeServerRequests.get(build(records), f"/xrpc/org.atgeo.searchPlaces?{self.base}")
        assert [p["uri"] for p in body["places"]] == [place_origin(i) for i in "cab"]
