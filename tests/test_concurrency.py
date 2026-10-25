"""The drivers' concurrency and cancellation (Python spec §5). No reference counterpart."""

from __future__ import annotations

import _thread
import asyncio
import threading
import time
from typing import Any

import pytest

from atgeo import AsyncAtgeoClient, AtgeoClient, AtgeoError, Bbox

from .conftest import Make
from .fake_server import FakeServer, FakeTile, overture
from .helpers import OVERTURE, place, place_origin, point, record

REGION = Bbox(-122.42, 37.77, -122.41, 37.78)


def tile(i: int) -> str:
    return f"https://tiles.example/{i:03d}.json"


def many_tiles(n: int, per_tile: int = 1, delay: float = 0.0) -> FakeServer:
    tiles = [
        FakeTile(
            tile(i), [record(place(f"{i}-{j}", f"Place {i} {j}", [point(37.775, -122.415)])) for j in range(per_tile)]
        )
        for i in range(n)
    ]
    return FakeServer(tilesets=[overture(tiles)], delays={tile(i): delay for i in range(n)} if delay else None)


class TestTilesInFlight:
    def test_never_exceed_eight_and_reach_it(self, make: Make) -> None:
        fake = many_tiles(20, delay=0.05)
        result = make(fake).search_places(REGION, **OVERTURE)
        assert result.tiles.fetched == 20
        assert fake.peak_in_flight == 8

    def test_claim_urls_in_order(self, make: Make) -> None:
        # URLs are claimed in order; with eight threads calling the transport at once, the requests can still
        # arrive interleaved (free-threaded CPython and PyPy show it), so the sync client is checked for the
        # first eight as a set. The async client claims and calls on one thread, in order.
        fake = many_tiles(20, delay=0.2)
        client = make(fake)
        client.search_places(REGION, **OVERTURE)
        requested = fake.requests[1:]
        assert sorted(requested) == [tile(i) for i in range(20)]
        if client.kind == "async":
            assert requested == [tile(i) for i in range(20)]
        else:
            assert set(requested[:8]) == {tile(i) for i in range(8)}


class TestFailedGetTiles:
    def test_caches_nothing_so_a_retry_sends_a_second_request(self, make: Make) -> None:
        calls = [0]

        def tiles_for(_bbox: str) -> list[str] | dict[str, Any]:
            calls[0] += 1
            return {"error": "BboxTooLarge", "message": "too large"} if calls[0] == 1 else [tile(0)]

        fake = many_tiles(1)
        fake.tiles_for = tiles_for
        client = make(fake)
        with pytest.raises(AtgeoError):
            client.search_places(REGION, **OVERTURE)
        assert client.search_places(REGION, **OVERTURE).tiles.fetched == 1
        assert calls[0] == 2

    def test_fails_a_crossing_search_when_the_first_half_fails(self, make: Make) -> None:
        def tiles_for(bbox: str) -> list[str] | dict[str, Any]:
            if bbox.startswith("170"):
                return {"error": "BboxTooLarge", "message": "first half"}
            return [tile(0)]

        fake = many_tiles(1)
        fake.tiles_for = tiles_for
        with pytest.raises(AtgeoError) as caught:
            make(fake).search_places(Bbox(170, 10, -170, 20), **OVERTURE)
        assert (caught.value.code, caught.value.message) == ("BboxTooLarge", "first half")


class TestAsyncCancellation:
    def test_raises_cancelled_error_rather_than_reporting_a_failed_tile(self) -> None:
        fake = many_tiles(3, delay=5)

        async def run() -> None:
            client = AsyncAtgeoClient(transport=fake.async_transport)
            task = asyncio.create_task(client.search_places(REGION, **OVERTURE))
            while fake.in_flight == 0:
                await asyncio.sleep(0.005)
            task.cancel()
            start = time.monotonic()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert time.monotonic() - start < 0.2

        asyncio.run(run())
        assert fake.in_flight == 0  # the fake's sleeps were cancelled with the search

    def test_keeps_the_event_loop_running_during_a_large_search(self) -> None:
        fake = many_tiles(40, per_tile=400)
        gaps: list[float] = []

        async def run() -> None:
            client = AsyncAtgeoClient(transport=fake.async_transport)
            stop = asyncio.Event()

            async def ticker() -> None:
                last = time.monotonic()
                while not stop.is_set():
                    await asyncio.sleep(0.001)
                    now = time.monotonic()
                    gaps.append(now - last)
                    last = now

            tick = asyncio.create_task(ticker())
            result = await client.search_places(REGION, text="Place 7", **OVERTURE)
            stop.set()
            await tick
            assert result.tiles.fetched == 40

        asyncio.run(run())
        print(f"ticker: {len(gaps)} ticks, longest gap {max(gaps) * 1000:.1f} ms")
        assert max(gaps) < 0.25


class TestKeyboardInterrupt:
    def test_returns_control_promptly_and_claims_no_more_urls(self) -> None:
        fake = many_tiles(20, delay=3)
        client = AtgeoClient(transport=fake.transport)
        timer = threading.Timer(0.3, _thread.interrupt_main)
        timer.start()
        start = time.monotonic()
        try:
            with pytest.raises(KeyboardInterrupt):
                client.search_places(REGION, **OVERTURE)
            elapsed = time.monotonic() - start
        finally:
            timer.cancel()
        print(f"KeyboardInterrupt returned after {elapsed * 1000:.0f} ms")
        assert elapsed < 0.3 + 0.4
        assert fake.in_flight == 8  # still in flight on their daemon threads
        claimed = len(fake.requests)
        assert claimed == 1 + 8  # getTiles and the first eight tiles
        time.sleep(3.3)  # the in-flight fetches finish
        assert len(fake.requests) == claimed


class TestSharedClient:
    def test_gives_every_thread_the_same_answer(self) -> None:
        fake = many_tiles(12, per_tile=20, delay=0.01)
        client = AtgeoClient(transport=fake.transport)
        results: list[list[str | None]] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def search() -> None:
            try:
                origins = [h.origin for h in client.search_places(REGION, text="Place 3", **OVERTURE).places]
                with lock:
                    results.append(origins)
            except BaseException as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=search) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert errors == []
        assert len(results) == 8
        assert all(r == results[0] for r in results)
        assert place_origin("3-0") in results[0]
