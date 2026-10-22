"""AsyncAtgeoClient: the asyncio driver (Python spec §5)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from typing import Self, TypeVar

from ._engine import (
    DEFAULT_CACHE_BUDGET_BYTES,
    DEFAULT_SERVER,
    MAX_TILES_IN_FLIGHT,
    Engine,
    Outcome,
    TileListRequest,
    returned,
)
from ._threads import start_daemon
from ._wire import TileList
from .transport import AsyncTransport
from .types import PlaceRecord, Region, SearchResult, ServiceDescription, SortMode, TileFailure, Tileset

_T = TypeVar("_T")


async def _in_thread(fn: Callable[[], _T]) -> _T:
    """Run CPU work on a daemon thread of the client's own, keeping the event loop free.

    Not run_in_executor: the default executor's threads get the platform's default stack, and an executor
    an app passes could be a ProcessPoolExecutor, which would fail every job. A job already running when
    the awaiting task is cancelled runs to its end, and what it stores is consistent.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[_T] = loop.create_future()

    def settle(ok: bool, value: object) -> None:
        if not future.done():
            if ok:
                future.set_result(value)  # type: ignore[arg-type]
            else:
                future.set_exception(value)  # type: ignore[arg-type]

    def run() -> None:
        try:
            result: tuple[bool, object] = (True, fn())
        except BaseException as e:
            result = (False, e)
        with contextlib.suppress(RuntimeError):  # the loop has closed: nobody is waiting
            loop.call_soon_threadsafe(settle, *result)

    start_daemon(run, "atgeo-cpu")
    return await future


def _first(group: BaseExceptionGroup[BaseException]) -> BaseException:
    """The exception a TaskGroup's failure stands for: a MemoryError if there is one, else the first."""
    leaves: list[BaseException] = []

    def walk(g: BaseExceptionGroup[BaseException]) -> None:
        for e in g.exceptions:
            if isinstance(e, BaseExceptionGroup):
                walk(e)
            else:
                leaves.append(e)

    walk(group)
    return next((e for e in leaves if isinstance(e, MemoryError)), leaves[0])


class AsyncAtgeoClient:
    """An asyncio client for the atgeo gazetteer. Safe to share between tasks; make one per server.

    The client's own state is not bound to an event loop, but the default transport's aiohttp session is,
    from its first request: a client using it belongs to the loop of its first call. Use it in an
    ``async with`` block, or ``await aclose()`` when done.
    """

    DEFAULT_SERVER = DEFAULT_SERVER
    DEFAULT_CACHE_BUDGET_BYTES = DEFAULT_CACHE_BUDGET_BYTES

    def __init__(
        self,
        server: str = DEFAULT_SERVER,
        *,
        transport: AsyncTransport | None = None,
        cache_budget_bytes: int = DEFAULT_CACHE_BUDGET_BYTES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._engine = Engine(server, cache_budget_bytes, clock)
        self._owns_transport = transport is None
        if transport is None:
            try:
                from ._aiohttp import AiohttpTransport  # noqa: PLC0415
            except ImportError as e:
                raise ImportError(
                    'AsyncAtgeoClient needs aiohttp: pip install "atgeo[async]", or pass a transport'
                ) from e
            transport = AiohttpTransport()
        self._transport = transport
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    async def search_places(
        self,
        region: Region,
        *,
        text: str | None = None,
        dataset: str | None = None,
        collection: str | None = None,
        sort: SortMode | str = SortMode.FUSED,
    ) -> SearchResult:
        """Search a region; the text, and the precise region, never leave the device (README, Privacy)."""
        engine = self._engine
        plan = engine.plan(region, text, dataset, collection, sort)
        self._check_loop()

        lists: list[TileList | None] = [engine.cached_tile_list(r.key) for r in plan.lists]
        missing = [i for i, cached in enumerate(lists) if cached is None]
        if missing:

            async def fetch_list(i: int, r: TileListRequest) -> None:
                response = await self._transport.get(r.url)
                lists[i] = await _in_thread(lambda: engine.store_tile_list(r.key, response))

            try:
                async with asyncio.TaskGroup() as group:  # one failing cancels the other (contract §14)
                    for i in missing:
                        group.create_task(fetch_list(i, plan.lists[i]))
            except BaseExceptionGroup as eg:
                # A TaskGroup wraps even a single failure. A plain raise keeps the error's own __cause__.
                raise _first(eg)  # noqa: B904
        ready = [tile_list for tile_list in lists if tile_list is not None]

        urls = engine.tile_urls(ready)
        outcomes: list[Outcome | None] = [None] * len(urls)
        claimed = 0

        async def worker() -> None:
            nonlocal claimed
            while claimed < len(urls):
                i = claimed
                claimed += 1
                outcomes[i] = await self._tile(urls[i])  # anything it raises fails the search

        try:
            async with asyncio.TaskGroup() as group:
                for _ in range(min(MAX_TILES_IN_FLIGHT, len(urls))):
                    group.create_task(worker())
        except BaseExceptionGroup as eg:
            raise _first(eg)  # noqa: B904
        overview = any(tile_list.overview for tile_list in ready)
        done = [outcome for outcome in outcomes if outcome is not None]
        assert len(done) == len(urls)
        return returned(await _in_thread(lambda: engine.assemble(plan, urls, done, overview)))

    async def _tile(self, url: str) -> Outcome:
        """A tile, or why it failed. Only the transport call is guarded here; the engine guards the rest."""
        engine = self._engine
        entry = engine.cached_tile(url)  # checked when a worker reaches the URL, as the reference does
        if entry is not None:
            records = engine.memo(url, entry)
            if records is not None:
                return entry, records
            return await _in_thread(lambda: engine.reparse(url, entry))
        request = engine.tile_request(url)
        if isinstance(request, TileFailure):
            return request
        try:
            response = engine.check_response(await self._transport.get(request))
        except MemoryError:
            raise
        except Exception as e:  # whatever the transport raises; CancelledError is a BaseException and passes
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise  # a transport that turned our cancellation into an error: the search is cancelled
            return engine.transport_failure(url, e)
        return await _in_thread(lambda: engine.load_tile(url, response))

    async def get_place(self, origin: str) -> PlaceRecord:
        """A place by origin (or, for a record with none, by its tile record's uri)."""
        self._check_loop()
        engine = self._engine
        cached = await _in_thread(lambda: engine.cached_place(origin))
        if cached is not None:
            return cached
        response = await self._transport.get(engine.place_url(origin))
        return await _in_thread(lambda: engine.read_place(origin, response))

    async def describe_service(self) -> ServiceDescription:
        """The server's DID and tilesets; cached for an hour."""
        self._check_loop()
        engine = self._engine
        cached = engine.cached_service()
        if cached is not None:
            return cached
        response = await self._transport.get(engine.service_url())
        return await _in_thread(lambda: engine.store_service(response))

    async def describe_tileset(self, slug: str) -> Tileset:
        """A tileset's descriptor; not cached."""
        self._check_loop()
        response = await self._transport.get(self._engine.tileset_url(slug))
        return await _in_thread(lambda: self._engine.read_tileset(response))

    def _check_loop(self) -> None:
        # The default transport's session belongs to the loop of its first request. Checked here, before any
        # request, so that a search from another loop raises rather than counting every tile as failed.
        if not self._owns_transport:
            return
        # A closed transport refuses every request, which inside a search would count each tile as failed.
        if self._closed:
            raise RuntimeError("this AsyncAtgeoClient is closed")
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif loop is not self._loop:
            raise RuntimeError(
                "this AsyncAtgeoClient's default transport belongs to the event loop of its first call; "
                "make one client per event loop"
            )

    async def aclose(self) -> None:
        """Close the transport, if the client made it; such a client then refuses every call."""
        if self._owns_transport:
            self._closed = True
            await self._transport.aclose()  # type: ignore[attr-defined]

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
