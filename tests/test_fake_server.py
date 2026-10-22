from __future__ import annotations

import json
from typing import Any

import pytest

from .fake_server import FakeServer, FakeTile, FakeTileset, overture
from .helpers import place, point, record

TILE = "https://tiles.example/a.json"
OK = place("a", "Alpha", [point(37.772, -122.412)])


def build(records: list[Any], slug: Any = "overture-place") -> FakeServer:
    tileset = overture([FakeTile(TILE, records)])
    return FakeServer(tilesets=[FakeTileset(slug, tileset.collections, tileset.tiles)])


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

    def test_answers_describe_service_with_a_did_and_tilesets(self) -> None:
        _, body = self.get(build([]), "/xrpc/org.atgeo.describeService")
        assert body == {
            "did": "did:web:places.atgeo.org",
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
