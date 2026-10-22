"""transport.test.ts, ported: failed tiles (each kind, with its URL and status, in URL order), typed errors,
URL construction. The default transports themselves are test_transports.py's."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from collections import namedtuple
from http import HTTPStatus
from typing import Any

import pytest

from atgeo import (
    AsyncAtgeoClient,
    AtgeoClient,
    AtgeoDecodingError,
    AtgeoError,
    Bbox,
    TileCounts,
    TileFailure,
    TileFailureKind,
    _engine,
)
from atgeo._cache import TileCache, TileEntry
from atgeo._engine import MAX_TILES_IN_FLIGHT
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


def serving(tiles: dict[str, HttpResponse | None], **opts: Any) -> FakeServer:
    """A fake whose tile list is ``tiles``' keys, in that order, each answered with its value; None falls
    through to the fake's routing, which answers 404 for a URL it does not know."""
    listing = HttpResponse(200, json.dumps({"tiles": list(tiles)}).encode())
    responses = {url: r for url, r in tiles.items() if r is not None}
    return FakeServer(
        answer=lambda url: listing if "/xrpc/org.atgeo.getTiles?" in url else None, responses=responses, **opts
    )


GOOD = HttpResponse(200, json.dumps({"records": [record(place("a", "A", [point(37.772, -122.412)]))]}).encode())


class TestTileFailures:
    """Contract §4.4: each failed tile, with its kind, URL and status, in URL order."""

    def test_reports_no_failures_for_a_complete_search(self, make: Make) -> None:
        result = make(two_tiles()).search_places(REGION)
        assert result.tile_failures == ()

    @pytest.mark.parametrize(
        "bad",
        [
            "tiles/a.json",
            "ftp://tiles.example/a.json",
            "file:///etc/hostname",
            "https://user:pw@tiles.example/a.json",
            "https://user@tiles.example/a.json",
            "https://:pw@tiles.example/a.json",
            "https://@tiles.example/a.json",  # no credentials, but a margin this port refuses (CONFORMANCE.md)
        ],
    )
    def test_reports_a_refused_url_without_a_request(self, make: Make, bad: str) -> None:
        fake = serving({bad: None, tile_url("a"): GOOD})
        result = make(fake).search_places(REGION)
        assert result.tile_failures == (TileFailure(url=bad, kind=TileFailureKind.REFUSED, status=None, error=_ANY),)
        assert isinstance(result.tile_failures[0].error, ValueError)
        assert result.tiles == TileCounts(fetched=1, failed=1)
        assert all(bad not in r for r in fake.requests)

    @pytest.mark.parametrize("status", [404, 500, 503])
    def test_reports_a_non_2xx_with_its_status(self, make: Make, status: int) -> None:
        body = b'{"error": "Upstream", "message": "no"}'
        result = make(serving({tile_url("a"): HttpResponse(status, body)})).search_places(REGION)
        [failure] = result.tile_failures
        assert (failure.url, failure.kind, failure.status) == (tile_url("a"), TileFailureKind.STATUS, status)
        assert isinstance(failure.error, AtgeoError)
        assert (failure.error.code, failure.error.status) == ("Upstream", status)
        assert failure.kind == "status"

    @pytest.mark.parametrize(
        ("body", "error"),
        [
            (b"<html>not json</html>", ValueError),
            (b"[]", AtgeoDecodingError),
            (b'"records"', AtgeoDecodingError),
            (b'{"tiles": []}', AtgeoDecodingError),
            (b'{"records": {}}', AtgeoDecodingError),
            (b'{"records": null}', AtgeoDecodingError),
        ],
    )
    def test_reports_a_2xx_without_a_usable_payload_as_decode(
        self, make: Make, body: bytes, error: type[Exception]
    ) -> None:
        result = make(serving({tile_url("a"): HttpResponse(200, body), tile_url("b"): GOOD})).search_places(REGION)
        [failure] = result.tile_failures
        assert (failure.url, failure.kind, failure.status) == (tile_url("a"), TileFailureKind.DECODE, None)
        assert isinstance(failure.error, error)
        assert result.tiles == TileCounts(fetched=1, failed=1)

    def test_reports_a_network_error_as_transport(self, make: Make) -> None:
        result = make(two_tiles(network_errors=[tile_url("a")])).search_places(REGION)
        [failure] = result.tile_failures
        assert (failure.url, failure.kind, failure.status) == (tile_url("a"), TileFailureKind.TRANSPORT, None)
        assert isinstance(failure.error, OSError)

    def test_reports_whatever_a_transport_raises_as_transport_with_its_traceback(self, make: Make) -> None:
        # A TypeError is the type of a bug, but it came from the app's transport, so it is a failed tile.
        raised: list[TypeError] = []

        def answer(url: str) -> HttpResponse | None:
            if url == tile_url("b"):
                raised.append(TypeError("the app's transport is broken"))
                raise raised[0]
            return None

        result = make(two_tiles(answer=answer)).search_places(REGION)
        [failure] = result.tile_failures
        assert (failure.url, failure.kind, failure.status) == (tile_url("b"), TileFailureKind.TRANSPORT, None)
        assert failure.error is raised[0]
        assert failure.error.__traceback__ is not None
        assert result.tiles == TileCounts(fetched=1, failed=1)

    def test_lists_failures_in_url_order_not_completion_order(self, make: Make) -> None:
        tiles: dict[str, HttpResponse | None] = {
            tile_url("e"): HttpResponse(200, b"nope"),
            tile_url("a"): GOOD,
            tile_url("d"): None,  # a network error, below
            "ftp://tiles.example/c.json": None,
            tile_url("b"): HttpResponse(502, b""),
        }
        # b, the first of the fetched failures in URL order, answers last.
        fake = serving(tiles, network_errors=[tile_url("d")], delays={tile_url("b"): 0.1})
        result = make(fake).search_places(REGION)
        assert [(f.url, f.kind, f.status) for f in result.tile_failures] == [
            ("ftp://tiles.example/c.json", TileFailureKind.REFUSED, None),
            (tile_url("b"), TileFailureKind.STATUS, 502),
            (tile_url("d"), TileFailureKind.TRANSPORT, None),
            (tile_url("e"), TileFailureKind.DECODE, None),
        ]
        assert result.tiles == TileCounts(fetched=1, failed=len(result.tile_failures))
        assert result.complete is False

    def test_compares_failures_without_their_errors(self, make: Make) -> None:
        first = make(two_tiles([tile_url("b")])).search_places(REGION)
        second = make(two_tiles([tile_url("b")])).search_places(REGION)
        assert first.tile_failures[0].error is not second.tile_failures[0].error
        assert first == second
        a = TileFailure(url="u", kind=TileFailureKind.DECODE, status=None, error=ValueError("x"))
        b = TileFailure(url="u", kind=TileFailureKind.DECODE, status=None, error=RuntimeError("y"))
        assert a == b
        assert hash(a) == hash(b)
        assert a != TileFailure(url="u", kind=TileFailureKind.TRANSPORT, status=None, error=ValueError("x"))

    def test_reports_a_cached_tile_that_fails_to_parse_again_as_decode(
        self, make: Make, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # It parsed once when it was stored, so this cannot happen; the guard is there all the same.
        client = make(two_tiles())
        client.search_places(REGION)
        engine = client.inner._engine
        with engine._lock:
            engine._parsed.replace({})  # else the memo answers without parsing
        original = TileEntry.records

        def records(self: TileEntry) -> Any:
            if b"place:a" in self.body:
                raise ValueError("unparsable")
            return original(self)

        monkeypatch.setattr(TileEntry, "records", records)
        result = client.search_places(REGION)
        assert [(f.url, f.kind) for f in result.tile_failures] == [(tile_url("a"), TileFailureKind.DECODE)]
        assert result.tiles == TileCounts(fetched=1, failed=1)
        assert client.fake.requests.count(tile_url("a")) == 1  # from the cache, not fetched again


class TestMalformedResponses:
    """A transport that returns something other than an HttpResponse of int and bytes: a transport failure,
    not an error in the status check or the decoder that fails the search (contract §4.4)."""

    @pytest.mark.parametrize(
        ("response", "says"),
        [
            (None, "NoneType, not an HttpResponse"),
            ((200, b"{}"), "tuple, not an HttpResponse"),
            (HttpResponse("503", b""), "status of type str"),  # type: ignore[arg-type]
            (HttpResponse(True, b""), "status of type bool"),
            (HttpResponse(200.0, b"{}"), "status of type float"),  # type: ignore[arg-type]
            (HttpResponse(404, None), "body of type NoneType"),  # type: ignore[arg-type]
            (HttpResponse(200, '{"records": []}'), "body of type str"),  # type: ignore[arg-type]
        ],
    )
    def test_reports_a_malformed_response_as_transport(self, make: Make, response: object, says: str) -> None:
        fake = serving({tile_url("a"): GOOD, tile_url("b"): GOOD})
        fake.responses[tile_url("a")] = response  # type: ignore[assignment]
        result = make(fake).search_places(REGION)
        [failure] = result.tile_failures
        assert (failure.url, failure.kind, failure.status) == (tile_url("a"), TileFailureKind.TRANSPORT, None)
        assert isinstance(failure.error, TypeError)
        assert says in str(failure.error)
        assert result.tiles == TileCounts(fetched=1, failed=1)

    @pytest.mark.parametrize(
        "response",
        [
            HttpResponse(HTTPStatus.OK, GOOD.body),
            HttpResponse(200, bytearray(GOOD.body)),  # type: ignore[arg-type]
            HttpResponse(200, memoryview(GOOD.body)),  # type: ignore[arg-type]
        ],
    )
    def test_takes_an_int_subclass_status_and_a_bytes_like_body(self, make: Make, response: HttpResponse) -> None:
        # Not stricter than the type allows (contract §14): HTTPStatus is an int, and a bytearray passes mypy.
        fake = serving({tile_url("a"): GOOD, tile_url("b"): GOOD})
        fake.responses[tile_url("a")] = response
        result = make(fake).search_places(REGION)
        assert result.tile_failures == ()
        assert result.tiles == TileCounts(fetched=2, failed=0)

    def test_reports_a_non_2xx_http_status_as_an_int(self, make: Make) -> None:
        fake = serving({tile_url("a"): HttpResponse(HTTPStatus.SERVICE_UNAVAILABLE, b"")})
        [failure] = make(fake).search_places(REGION).tile_failures
        assert (failure.kind, failure.status, type(failure.status)) == (TileFailureKind.STATUS, 503, int)

    def test_takes_a_duck_typed_response(self, make: Make) -> None:
        # As getTiles takes one: by attribute, not by class.
        Response = namedtuple("Response", ["status", "body"])
        fake = serving({tile_url("a"): GOOD, tile_url("b"): GOOD})
        fake.responses[tile_url("a")] = Response(200, GOOD.body)  # type: ignore[assignment]
        result = make(fake).search_places(REGION)
        assert result.complete is True
        assert result.tiles == TileCounts(fetched=2, failed=0)


class TestWhatIsNotAFailedTile:
    """Contract §4.4: the client's own work after decoding is not guarded, and MemoryError never is."""

    def test_fails_the_search_on_an_error_caching_a_tile(self, make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
        def broken(self: TileCache, url: str, entry: TileEntry) -> None:
            raise RuntimeError("a bug in the cache")

        monkeypatch.setattr(TileCache, "set", broken)
        with pytest.raises(RuntimeError, match="a bug in the cache"):
            make(two_tiles()).search_places(REGION)

    @pytest.mark.parametrize("where", ["decode", "cache"])
    def test_fails_the_search_on_a_memory_error(self, make: Make, monkeypatch: pytest.MonkeyPatch, where: str) -> None:
        def exhausted(*_: object) -> Any:
            raise MemoryError

        if where == "decode":
            monkeypatch.setattr(_engine, "read_tile_payload", exhausted)
        else:
            monkeypatch.setattr(TileCache, "set", exhausted)
        with pytest.raises(MemoryError):
            make(two_tiles()).search_places(REGION)

    def test_raises_cancelled_error_with_a_failed_tile_in_hand(self) -> None:
        fake = two_tiles(network_errors=[tile_url("a")], delays={tile_url("b"): 5})

        async def run() -> None:
            client = AsyncAtgeoClient(transport=fake.async_transport)
            task = asyncio.create_task(client.search_places(REGION))
            while fake.in_flight == 0 or tile_url("a") not in fake.requests:
                await asyncio.sleep(0.005)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(run())
        assert fake.in_flight == 0


class _Any(Exception):
    """An error for building an expected TileFailure: ``error`` takes no part in ``==``."""


_ANY = _Any()


class TestCancellationAndInterrupts:
    def test_fails_the_search_on_a_memory_error_reparsing_a_cached_tile(
        self, make: Make, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = make(two_tiles())
        client.search_places(REGION)
        engine = client.inner._engine
        with engine._lock:
            engine._parsed.replace({})  # else the memo answers without parsing

        def exhausted(self: TileEntry) -> Any:
            raise MemoryError

        monkeypatch.setattr(TileEntry, "records", exhausted)
        with pytest.raises(MemoryError):
            client.search_places(REGION)

    def test_fails_a_blocking_search_on_keyboard_interrupt_from_the_transport(self) -> None:
        def answer(url: str) -> HttpResponse | None:
            if url == tile_url("a"):
                raise KeyboardInterrupt
            return None

        with pytest.raises(KeyboardInterrupt):
            AtgeoClient(transport=two_tiles(answer=answer).transport).search_places(REGION)

    def test_stays_cancelled_when_the_transport_turns_cancellation_into_an_error(self) -> None:
        # More tiles than workers: a worker that took the transport's error for a failed tile would claim the
        # next URL, whose request the spent cancellation no longer interrupts, and the search would hang on it.
        urls = [tile_url(f"t{n:02}") for n in range(MAX_TILES_IN_FLIGHT + 2)]
        listing = HttpResponse(200, json.dumps({"tiles": urls}).encode())
        requested: list[str] = []
        started = asyncio.Event()

        class Swallowing:
            async def get(self, url: str) -> HttpResponse:
                if "/xrpc/" in url:
                    return listing
                requested.append(url)
                if len(requested) == MAX_TILES_IN_FLIGHT:
                    started.set()
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    raise RuntimeError("the transport's own error, in place of the cancellation") from None
                raise AssertionError("not cancelled")

        async def run() -> None:
            client = AsyncAtgeoClient(transport=Swallowing())
            task = asyncio.create_task(client.search_places(REGION))
            await started.wait()
            task.cancel()
            start = time.monotonic()
            with pytest.raises(asyncio.CancelledError):  # asyncio's convention, not the transport's error
                await task
            assert time.monotonic() - start < 1

        asyncio.run(run())
        assert len(requested) == MAX_TILES_IN_FLIGHT  # no tile claimed after the cancel

    def test_reports_a_transport_timeout_that_leaves_its_task_cancelling_as_a_failed_tile(self) -> None:
        # aiohttp 3.10's timeout cancels the task and raises TimeoutError without uncancel(), so the worker's
        # cancelling() stays raised; the search itself was not cancelled and must return.
        fake = two_tiles()

        class TimingOut:
            async def get(self, url: str) -> HttpResponse:
                if url != tile_url("a"):
                    return await fake.async_transport.get(url)
                task = asyncio.current_task()
                assert task is not None
                task.cancel()
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    raise TimeoutError("total timeout") from None
                raise AssertionError("not cancelled")

        result = asyncio.run(AsyncAtgeoClient(transport=TimingOut()).search_places(REGION))
        [failure] = result.tile_failures
        assert (failure.url, failure.kind) == (tile_url("a"), TileFailureKind.TRANSPORT)
        assert isinstance(failure.error, TimeoutError)
        assert result.tiles == TileCounts(fetched=1, failed=1)


class TestTransportCancellingItsOwnRequest:
    """A transport whose own deadline cancels its request, though nobody cancelled the search: a failed
    tile of kind transport, and the search goes on."""

    @staticmethod
    def self_cancelling(urls: list[str], bad: set[str], *, escape: bool) -> Any:
        listing = HttpResponse(200, json.dumps({"tiles": urls}).encode())
        good = HttpResponse(200, b'{"records": []}')

        class SelfCancelling:
            async def get(self, url: str) -> HttpResponse:
                if "/xrpc/" in url:
                    return listing
                if url in bad:
                    task = asyncio.current_task()
                    assert task is not None
                    task.cancel()
                    if escape:
                        await asyncio.sleep(1)  # the CancelledError escapes the transport
                    # else it returns, and the cancellation lands at the client's next await
                return good

        return SelfCancelling()

    @pytest.mark.parametrize("escape", [True, False])
    def test_reports_it_as_a_transport_failure(self, escape: bool) -> None:
        urls = [tile_url(f"t{n}") for n in range(3)]
        transport = self.self_cancelling(urls, {urls[1]}, escape=escape)
        result = asyncio.run(AsyncAtgeoClient(transport=transport).search_places(REGION))
        assert result.tiles == TileCounts(fetched=2, failed=1)
        [failure] = result.tile_failures
        assert (failure.url, failure.kind, failure.status) == (urls[1], TileFailureKind.TRANSPORT, None)
        assert isinstance(failure.error, TimeoutError)
        assert isinstance(failure.error.__cause__, asyncio.CancelledError)

    def test_reports_every_tile_when_every_worker_is_cancelled(self) -> None:
        urls = [tile_url(f"t{n:02}") for n in range(MAX_TILES_IN_FLIGHT + 3)]
        transport = self.self_cancelling(urls, set(urls), escape=True)
        result = asyncio.run(AsyncAtgeoClient(transport=transport).search_places(REGION))
        assert result.tiles == TileCounts(fetched=0, failed=len(urls))
        assert [f.url for f in result.tile_failures] == urls
        assert {f.kind for f in result.tile_failures} == {TileFailureKind.TRANSPORT}
