"""Contract §16: reference behaviours a port must not reproduce (crashes, wrong counts, laxer typing).

The reference crashes on these, so the JS suite cannot test them. Served through the fake's raw overrides.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from atgeo import (
    AsyncAtgeoClient,
    AtgeoClient,
    AtgeoDecodingError,
    AtgeoError,
    Bbox,
    Circle,
    TileCounts,
    TileFailureKind,
)
from atgeo.transport import HttpResponse

from .conftest import Make
from .fake_server import FakeServer, FakeTile, FakeTileset, overture
from .helpers import place, place_origin, point, record, tile_url, xrpc

REGION = Bbox(-122.42, 37.77, -122.41, 37.78)
BBOX = "-122.42,37.77,-122.41,37.78"
INSIDE = point(37.775, -122.415)
GET_TILES = xrpc("org.atgeo.getTiles", [("dataset", "overturemaps.org"), ("collection", "place"), ("bbox", BBOX)])
GET_TILES_DATASET = xrpc("org.atgeo.getTiles", [("dataset", "overturemaps.org"), ("bbox", BBOX)])
DESCRIBE_SERVICE = xrpc("org.atgeo.describeService", [])
DESCRIBE_TILESET = xrpc("org.atgeo.describeTileset", [("slug", "s")])


def raw(body: str | bytes, status: int = 200) -> HttpResponse:
    return HttpResponse(status, body.encode() if isinstance(body, str) else body)


def ok(id: str, name: str | None = None) -> dict[str, Any]:
    return place(id, name or id, [INSIDE])


def tile_serving(records: str | bytes) -> FakeServer:
    """A server whose tile list is one tile at tile_url("t") holding `records` (served raw, unvalidated)."""
    listing = raw(json.dumps({"tiles": [tile_url("t")]}))
    body = b'{"records": ' + (records.encode() if isinstance(records, str) else records) + b"}"
    return FakeServer(responses={GET_TILES: listing, GET_TILES_DATASET: listing, tile_url("t"): raw(body)})


def js(value: object) -> str:
    return json.dumps(value)


class TestGetTilesBodies:
    @pytest.mark.parametrize("body", ["{}", "[]", '{"tiles": "x"}', "null", '{"tiles": null}'])
    def test_raise_atgeo_decoding_error_without_a_tiles_list(self, make: Make, body: str) -> None:
        with pytest.raises(AtgeoDecodingError):
            make(FakeServer(responses={GET_TILES: raw(body)})).search_places(REGION)

    def test_raise_atgeo_decoding_error_for_a_body_that_is_not_json_with_the_parser_error_as_cause(
        self, make: Make
    ) -> None:
        with pytest.raises(AtgeoDecodingError) as caught:
            make(FakeServer(responses={GET_TILES: raw("<html>")})).search_places(REGION)
        assert isinstance(caught.value.__cause__, json.JSONDecodeError)

    def test_cache_nothing_when_the_tile_list_is_unusable(self, make: Make) -> None:
        calls = [0]

        def answer(url: str) -> HttpResponse | None:
            if url == GET_TILES:
                calls[0] += 1
                if calls[0] == 1:
                    return raw("{}")
            return None

        fake = FakeServer(tilesets=[overture([FakeTile(tile_url("a"), [record(ok("a"))])])], answer=answer)
        client = make(fake)
        with pytest.raises(AtgeoDecodingError):
            client.search_places(REGION)
        assert [h.origin for h in client.search_places(REGION).places] == [place_origin("a")]
        assert calls[0] == 2

    def test_skip_non_string_tile_entries_instead_of_fetching_them(self, make: Make) -> None:
        listing = raw(f'{{"tiles": [1, null, {{}}, "{tile_url("t")}", ["x"]]}}')
        fake = FakeServer(responses={GET_TILES: listing, tile_url("t"): raw(f'{{"records": [{js(record(ok("a")))}]}}')})
        result = make(fake).search_places(REGION)
        assert (result.tiles.fetched, result.tiles.failed) == (1, 0)
        assert fake.requests == [GET_TILES, tile_url("t")]


class TestTileRecords:
    def test_skip_records_that_are_not_objects_or_have_no_object_value_and_keep_the_tile(self, make: Make) -> None:
        records = f'[7, "s", [1], null, {{"value": null}}, {{}}, {{"importance": 3}}, {js(record(ok("a")))}]'
        result = make(tile_serving(records)).search_places(REGION)
        assert (result.tiles.fetched, result.tiles.failed) == (1, 0)
        assert [h.origin for h in result.places] == [place_origin("a")]

    def test_skip_a_scalar_value_without_consuming_its_dedupe_key(self, make: Make) -> None:
        no_origin = place("x", "By uri", [INSIDE], {"origin": None})
        records = f'[{{"value": 5, "uri": "u"}}, {js(record(no_origin, uri="u"))}]'
        result = make(tile_serving(records)).search_places(REGION, dataset="overturemaps.org")
        assert [h.origin for h in result.places] == ["u"]

    def test_keep_answering_get_place_from_a_tile_that_held_bad_records(self, make: Make) -> None:
        fake = tile_serving(f"[null, 7, {js(record(ok('a'), 4))}]")
        client = make(fake)
        client.search_places(REGION)
        fake.go_offline()
        found = client.get_place(place_origin("a"))
        assert (found.origin, dict(found.place), found.importance) == (place_origin("a"), ok("a"), 4)

    def test_read_a_non_string_uri_as_absent(self, make: Make) -> None:
        anon = js(place("x", "Anon", [INSIDE], {"origin": None}))
        records = f'[{{"value": {anon}, "uri": {{}}}}, {{"value": {anon}, "uri": 9}}]'
        result = make(tile_serving(records)).search_places(REGION, dataset="overturemaps.org")
        assert [h.origin for h in result.places] == [None, None]

    def test_read_a_non_number_importance_as_absent(self, make: Make) -> None:
        records = (
            f'[{{"value": {js(ok("a"))}, "importance": "7"}}, {{"value": {js(ok("b"))}, "importance": [1]}},'
            f' {{"value": {js(ok("c"))}, "importance": true}}]'
        )
        result = make(tile_serving(records)).search_places(REGION)
        assert [h.importance for h in result.places] == [None, None, None]

    def test_count_a_2xx_tile_that_is_not_json_as_failed(self, make: Make) -> None:
        listing = raw(json.dumps({"tiles": [tile_url("bad"), tile_url("t")]}))
        fake = FakeServer(
            responses={
                GET_TILES: listing,
                tile_url("bad"): raw("<html>not json</html>"),
                tile_url("t"): raw(f'{{"records": [{js(record(ok("a")))}]}}'),
            }
        )
        result = make(fake).search_places(REGION)
        assert (result.tiles.fetched, result.tiles.failed, result.complete) == (1, 1, False)
        assert [h.origin for h in result.places] == [place_origin("a")]

    @pytest.mark.parametrize("bad", ["tiles/a.json", "file:///etc/hostname", "ftp://x/y", "//tiles.example/a"])
    def test_count_a_tile_url_that_is_not_absolute_http_as_failed_without_a_request(self, make: Make, bad: str) -> None:
        listing = raw(json.dumps({"tiles": [bad, tile_url("t")]}))
        fake = FakeServer(responses={GET_TILES: listing, tile_url("t"): raw(f'{{"records": [{js(record(ok("a")))}]}}')})
        result = make(fake).search_places(REGION)
        assert (result.tiles.fetched, result.tiles.failed) == (1, 1)
        assert bad not in fake.requests
        assert fake.requests == [GET_TILES, tile_url("t")]

    def test_count_a_deeply_nested_tile_as_failed_without_crashing(self, make: Make) -> None:
        depth = 100_000
        nested = b"[" * depth + b"]" * depth
        listing = raw(json.dumps({"tiles": [tile_url("deep"), tile_url("t")]}))
        fake = FakeServer(
            responses={
                GET_TILES: listing,
                tile_url("deep"): raw(b'{"records": [{"value": {"x": ' + nested + b"}}]}"),
                tile_url("t"): raw(f'{{"records": [{js(record(ok("a")))}]}}'),
            }
        )
        result = make(fake).search_places(REGION)
        assert (result.tiles.fetched, result.tiles.failed) == (1, 1)
        # The server's input overflowed the parser, so the tile failed to decode (contract §4.4).
        [failure] = result.tile_failures
        assert (failure.url, failure.kind) == (tile_url("deep"), TileFailureKind.DECODE)
        assert isinstance(failure.error, RecursionError)


class TestPlaceValues:
    def test_skip_null_and_other_non_object_locations(self, make: Make) -> None:
        value = place("a", "A", [None, 3, [], "x", INSIDE])
        result = make(tile_serving(f"[{js(record(value))}]")).search_places(REGION)
        assert [h.origin for h in result.places] == [place_origin("a")]

    def test_skip_null_variants_when_the_search_has_text(self, make: Make) -> None:
        value = place("m", "München", [INSIDE], {"variants": [None, 4, {"name": "Munich"}]})
        hits = make(tile_serving(f"[{js(record(value))}]")).search_places(REGION, text="Munich").places
        assert [h.score for h in hits] == [1.0]

    def test_read_a_coordinate_given_as_an_array_as_no_location(self, make: Make) -> None:
        arrayed = {**INSIDE, "latitude": [37.775]}
        result = make(tile_serving(f"[{js(record(place('a', 'A', [arrayed])))}]")).search_places(REGION)
        assert result.places == []


class TestErrorBodies:
    @pytest.mark.parametrize("body", ['{"error": 7, "message": {}}', "null", '{"error": null}', "[1]", ""])
    def test_read_non_string_error_and_message_fields_as_absent(self, make: Make, body: str) -> None:
        with pytest.raises(AtgeoError) as caught:
            make(FakeServer(responses={GET_TILES: raw(body, 400)})).search_places(REGION)
        assert (caught.value.code, caught.value.message, caught.value.status) == ("HttpError", "HTTP 400", 400)


class TestOtherBodies:
    GET_PLACE = xrpc("org.atgeo.getPlace", [("uri", place_origin("a"))])

    @pytest.mark.parametrize("body", ["{}", "null", '{"uri": "x", "record": 5}', '{"record": null}', "[]"])
    def test_raise_atgeo_decoding_error_for_a_get_place_body_with_no_object_record(self, make: Make, body: str) -> None:
        with pytest.raises(AtgeoDecodingError):
            make(FakeServer(responses={self.GET_PLACE: raw(body)})).get_place(place_origin("a"))

    def test_raise_atgeo_decoding_error_for_a_get_place_body_that_is_not_json(self, make: Make) -> None:
        with pytest.raises(AtgeoDecodingError) as caught:
            make(FakeServer(responses={self.GET_PLACE: raw("{")})).get_place(place_origin("a"))
        assert isinstance(caught.value.__cause__, json.JSONDecodeError)

    @pytest.mark.parametrize("body", ["null", "false", "0", '""', "[]"])
    def test_raise_atgeo_decoding_error_for_a_describe_body_that_is_not_an_object(self, make: Make, body: str) -> None:
        with pytest.raises(AtgeoDecodingError):
            make(FakeServer(responses={DESCRIBE_SERVICE: raw(body)})).describe_service()
        with pytest.raises(AtgeoDecodingError):
            make(FakeServer(responses={DESCRIBE_TILESET: raw(body)})).describe_tileset("s")

    def test_map_a_describe_body_of_wrong_typed_fields_without_failing(self, make: Make) -> None:
        fake = FakeServer(
            responses={DESCRIBE_SERVICE: raw('{"did": 5, "tilesets": [{"slug": [], "collections": 3}, 7]}')}
        )
        service = make(fake).describe_service()
        assert service.did is None
        assert [(t.slug, t.collections) for t in service.tilesets] == [(None, ())]


class TestArguments:
    @pytest.mark.parametrize("server", ["places.atgeo.org", "ftp://x", "/xrpc", "", "https://", "file:///tmp"])
    @pytest.mark.parametrize("cls", [AtgeoClient, AsyncAtgeoClient], ids=["sync", "async"])
    def test_refuse_a_server_that_is_not_an_absolute_http_url(self, cls: Callable[..., object], server: str) -> None:
        fake = FakeServer()
        with pytest.raises(ValueError, match="server"):
            cls(server, transport=fake.transport)

    def test_send_an_empty_dataset_as_given(self, make: Make) -> None:
        fake = FakeServer(tilesets=[overture([]), empty_dataset_tileset()])
        result = make(fake).search_places(REGION, dataset="", collection="c")
        assert "dataset=&collection=c&" in fake.requests[0]
        assert [h.origin for h in result.places] == ["urn:atgeo::c:1"]

    def test_refuse_an_unknown_sort_before_any_request(self, make: Make) -> None:
        fake = FakeServer()
        with pytest.raises(ValueError, match="sort"):
            make(fake).search_places(REGION, sort="nearest")
        assert fake.requests == []

    @pytest.mark.parametrize("region", [(0, 0, 1, 1), {"lat": 0, "lon": 0, "radius": 1}, None])
    def test_refuse_a_region_of_the_wrong_type_with_type_error(self, make: Make, region: object) -> None:
        fake = FakeServer()
        with pytest.raises(TypeError):
            make(fake).search_places(region)
        assert fake.requests == []

    def test_check_the_region_before_the_sort(self, make: Make) -> None:
        with pytest.raises(ValueError, match="circle"):
            make(FakeServer()).search_places(Circle(0, 0, -1), sort="nearest")


def empty_dataset_tileset() -> FakeTileset:
    value = place("1", "One", [INSIDE], {"origin": "urn:atgeo::c:1"})
    other = place("2", "Two", [INSIDE], {"origin": "urn:atgeo:x:c:2"})
    return FakeTileset(
        "empty", [{"dataset": "", "collection": "c"}], [FakeTile(tile_url("e"), [record(value), record(other)])]
    )


class TestReviewRegressions:
    """Found by review; each would otherwise fail a search, leak, or diverge from the reference."""

    def test_fail_the_search_on_a_memory_error_in_a_tile(self, make: Make) -> None:
        def answer(url: str) -> HttpResponse | None:
            if url == tile_url("t"):
                raise MemoryError
            return None

        fake = FakeServer(tilesets=[overture([FakeTile(tile_url("t"), [record(ok("a"))])])], answer=answer)
        with pytest.raises(MemoryError):
            make(fake).search_places(REGION)

    @pytest.mark.parametrize("bad", ["https://user:pw@tiles.example/a.json", "https://user@tiles.example/a.json"])
    def test_count_a_tile_url_with_credentials_as_failed_without_a_request(self, make: Make, bad: str) -> None:
        fake = FakeServer(answer=lambda url: raw(json.dumps({"tiles": [bad]})) if "getTiles" in url else None)
        result = make(fake).search_places(REGION)
        assert result.tiles == TileCounts(fetched=0, failed=1)
        assert all(bad not in r for r in fake.requests)

    def test_send_a_lone_surrogate_as_the_replacement_character(self, make: Make) -> None:
        fake = FakeServer()
        with pytest.raises(AtgeoError):
            make(fake).get_place("urn:\ud800x")
        assert fake.requests[0].endswith("?uri=urn%3A%EF%BF%BDx")

    def test_return_the_callers_origin_from_the_network(self, make: Make) -> None:
        body = raw('{"uri": "https://elsewhere.example/r/9", "record": {"name": "N", "origin": "urn:x:y:z:9"}}')
        fake = FakeServer(answer=lambda url: body if "getPlace" in url else None)
        assert make(fake).get_place("https://example.org/r/1").origin == "https://example.org/r/1"
