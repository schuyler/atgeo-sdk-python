"""transport.test.ts, ported: failed tiles, typed errors, URL construction. The default transports
themselves are test_transports.py's."""

from __future__ import annotations

import urllib.parse

import pytest

from atgeo import AtgeoError, Bbox, TileCounts
from atgeo.transport import HttpResponse

from .conftest import Make
from .fake_server import DEFAULT_SERVER, FakeServer, FakeTile, overture
from .helpers import place, place_origin, point, record, tile_url

REGION = Bbox(-122.42, 37.77, -122.41, 37.78)


def two_tiles(fail_tiles: list[str] | None = None, **opts: object) -> FakeServer:
    return FakeServer(
        fail_tiles=fail_tiles or [],
        tilesets=[
            overture(
                [
                    FakeTile(tile_url("a"), [record(place("a", "A", [point(37.772, -122.412)]))]),
                    FakeTile(tile_url("b"), [record(place("b", "B", [point(37.774, -122.414)]))]),
                ]
            )
        ],
        **opts,  # type: ignore[arg-type]
    )


class TestFailedTiles:
    def test_reports_a_complete_search(self, make: Make) -> None:
        result = make(two_tiles()).search_places(REGION)
        assert result.complete is True
        assert result.tiles == TileCounts(fetched=2, failed=0)

    def test_returns_surviving_results_marked_incomplete_when_a_tile_answers_non_200(self, make: Make) -> None:
        result = make(two_tiles([tile_url("b")])).search_places(REGION)
        assert result.complete is False
        assert result.tiles == TileCounts(fetched=1, failed=1)
        assert [h.origin for h in result.places] == [place_origin("a")]

    def test_returns_an_incomplete_result_when_a_tile_fetch_rejects(self, make: Make) -> None:
        result = make(two_tiles(network_errors=[tile_url("a")])).search_places(REGION)
        assert result.complete is False
        assert result.tiles == TileCounts(fetched=1, failed=1)
        assert [h.origin for h in result.places] == [place_origin("b")]

    def test_does_not_retry_a_failed_tile(self, make: Make) -> None:
        fake = two_tiles([tile_url("b")])
        make(fake).search_places(REGION)
        assert fake.requests.count(tile_url("b")) == 1

    def test_reports_every_tile_failed_without_throwing(self, make: Make) -> None:
        result = make(two_tiles([tile_url("a"), tile_url("b")])).search_places(REGION)
        assert (result.places, result.complete, result.tiles) == ([], False, TileCounts(fetched=0, failed=2))


class TestTypedServerErrors:
    def test_surfaces_bbox_too_large_from_get_tiles(self, make: Make) -> None:
        fake = FakeServer(tiles_for=lambda _: {"error": "BboxTooLarge", "message": "bbox too large"})
        with pytest.raises(AtgeoError) as e:
            make(fake).search_places(REGION)
        assert (e.value.code, e.value.status) == ("BboxTooLarge", 400)

    @pytest.mark.parametrize(
        ("status", "body"),
        [
            (404, b'{"error":"PlaceNotFound"}'),
            (429, b""),
            (503, b'{"error":"InvalidRequest","message":"a 5xx with a 4xx-sounding code"}'),
        ],
    )
    def test_carries_the_http_status_whatever_the_body_says(self, make: Make, status: int, body: bytes) -> None:
        fake = FakeServer(answer=lambda _: HttpResponse(status, body))
        with pytest.raises(AtgeoError) as e:
            make(fake).get_place("urn:atgeo:overturemaps.org:place:x")
        assert e.value.status == status

    def test_surfaces_collection_not_found_from_get_tiles(self, make: Make) -> None:
        with pytest.raises(AtgeoError) as e:
            make(FakeServer()).search_places(REGION, collection="nope")
        assert e.value.code == "CollectionNotFound"

    def test_surfaces_dataset_not_found_from_get_tiles(self, make: Make) -> None:
        with pytest.raises(AtgeoError) as e:
            make(FakeServer()).search_places(REGION, dataset="example.org")
        assert e.value.code == "DatasetNotFound"

    @pytest.mark.parametrize(("half", "prefix"), [("first", "170"), ("second", "-180")])
    def test_throws_when_the_antimeridian_half_is_bbox_too_large(self, make: Make, half: str, prefix: str) -> None:
        fake = FakeServer(
            tilesets=[overture([FakeTile(tile_url("a"), [record(place("a", "A", [point(15, 175)]))])])],
            tiles_for=lambda bbox: (
                {"error": "BboxTooLarge", "message": "too large"} if bbox.startswith(prefix) else [tile_url("a")]
            ),
        )
        with pytest.raises(AtgeoError) as e:
            make(fake).search_places(Bbox(170, 10, -170, 20))
        assert e.value.code == "BboxTooLarge", half

    def test_does_not_subdivide_after_bbox_too_large(self, make: Make) -> None:
        fake = FakeServer(tiles_for=lambda _: {"error": "BboxTooLarge", "message": "too large"})
        with pytest.raises(AtgeoError):
            make(fake).search_places(REGION)
        assert len(fake.requests) == 1


def origin_and_path(url: str) -> str:
    u = urllib.parse.urlsplit(url)
    return f"{u.scheme}://{u.netloc}{u.path}"


class TestXrpcUrls:
    def test_defaults_to_places_atgeo_org(self, make: Make) -> None:
        fake = two_tiles()
        make(fake).search_places(REGION)
        assert origin_and_path(fake.requests[0]) == f"{DEFAULT_SERVER}/xrpc/org.atgeo.getTiles"
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(fake.requests[0]).query))
        assert params == {"dataset": "overturemaps.org", "collection": "place", "bbox": "-122.42,37.77,-122.41,37.78"}

    def test_targets_a_custom_server_for_every_xrpc_call(self, make: Make) -> None:
        base = "http://localhost:8000"
        fake = FakeServer(
            server=base,
            tilesets=[
                overture([FakeTile(f"{base}/tiles/a.json", [record(place("a", "A", [point(37.772, -122.412)]))])])
            ],
        )
        client = make(fake, server=base)
        client.search_places(REGION)
        client.describe_service()
        client.describe_tileset("overture-place")
        with pytest.raises(AtgeoError):
            client.get_place(place_origin("zzz"))
        assert [origin_and_path(r) for r in fake.requests if "/xrpc/" in r] == [
            f"{base}/xrpc/org.atgeo.getTiles",
            f"{base}/xrpc/org.atgeo.describeService",
            f"{base}/xrpc/org.atgeo.describeTileset",
            f"{base}/xrpc/org.atgeo.getPlace",
        ]
        assert f"{base}/tiles/a.json" in fake.requests

    def test_encodes_query_values_as_forms_do(self, make: Make) -> None:
        # Contract §1: the server must decode the exact strings; a literal + goes as %2B.
        fake = FakeServer()
        with pytest.raises(AtgeoError):
            make(fake).get_place("urn:x:a,b:c d&e=f#g+h%i~j")
        assert fake.requests == [
            f"{DEFAULT_SERVER}/xrpc/org.atgeo.getPlace?uri=urn%3Ax%3Aa%2Cb%3Ac+d%26e%3Df%23g%2Bh%25i~j"
        ]
