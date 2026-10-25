"""AtgeoClient: the blocking driver (Python spec §5)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from typing import Generic, Self, TypeVar

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
from .transport import Transport
from .types import (
    PlaceRecord,
    Region,
    SearchResult,
    ServiceDescription,
    SortMode,
    TileFailure,
    Tileset,
)

_I = TypeVar("_I")
_O = TypeVar("_O")
_WAIT = 0.1  # the caller waits in slices this long, so that KeyboardInterrupt reaches it promptly


class _Pool(Generic[_I, _O]):
    """``min(limit, n)`` daemon threads claiming items in order; the reference's mapLimit.

    Not ThreadPoolExecutor: its threads are joined at interpreter exit, so after a Ctrl-C the process
    would wait for every request in flight.
    """

    def __init__(self, items: Sequence[_I], fn: Callable[[_I], _O], limit: int, *, fail_fast: bool) -> None:
        self._items = items
        self._fn = fn
        self._fail_fast = fail_fast
        self._cond = threading.Condition()
        self._next = 0
        self._stop = False
        self._error: BaseException | None = None
        self.results: list[tuple[bool, _O | BaseException] | None] = [None] * len(items)
        self._alive = 0
        try:
            for i in range(min(limit, len(items))):
                with self._cond:
                    self._alive += 1
                try:
                    start_daemon(self._work, f"atgeo-{i}")
                except BaseException:
                    with self._cond:
                        self._alive -= 1
                    raise
        except BaseException:  # Ctrl-C while starting: the workers already running claim nothing more
            with self._cond:
                self._stop = True
            raise

    def _work(self) -> None:
        try:
            while True:
                with self._cond:
                    if self._stop or self._next >= len(self._items):
                        return
                    i = self._next
                    self._next += 1
                try:
                    result: tuple[bool, _O | BaseException] = (True, self._fn(self._items[i]))
                except BaseException as e:
                    result = (False, e)
                with self._cond:
                    self.results[i] = result
                    if not result[0] and self._error is None:
                        self._error = result[1]  # type: ignore[assignment]
                        if self._fail_fast:
                            self._stop = True
                    self._cond.notify_all()
        finally:
            with self._cond:
                self._alive -= 1
                self._cond.notify_all()

    def wait(self) -> None:
        """Until every worker has finished, or with fail_fast the first failure, which is raised."""
        try:
            with self._cond:
                while self._alive:
                    if self._fail_fast and self._error is not None:
                        break
                    self._cond.wait(_WAIT)
                error = self._error
            if self._fail_fast and error is not None:
                raise error
        except BaseException:
            with self._cond:
                self._stop = True  # no worker claims another item; requests in flight finish on their own
            raise


class AtgeoClient:
    """A blocking client for the atgeo gazetteer. Safe to share between threads; make one per server.

    Use it in a ``with`` block, or call ``close()`` when done: the default transport pools connections.
    """

    DEFAULT_SERVER = DEFAULT_SERVER
    DEFAULT_CACHE_BUDGET_BYTES = DEFAULT_CACHE_BUDGET_BYTES

    def __init__(
        self,
        server: str = DEFAULT_SERVER,
        *,
        transport: Transport | None = None,
        cache_budget_bytes: int = DEFAULT_CACHE_BUDGET_BYTES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._engine = Engine(server, cache_budget_bytes, clock)
        self._owns_transport = transport is None
        if transport is None:
            from ._urllib3 import Urllib3Transport  # noqa: PLC0415

            transport = Urllib3Transport()
        self._transport = transport

    def search_places(
        self,
        region: Region,
        *,
        dataset: str,
        text: str | None = None,
        collection: str | None = None,
        sort: SortMode | str = SortMode.FUSED,
    ) -> SearchResult:
        """Search a region; the text, and the precise region, never leave the device (README, Privacy)."""
        engine = self._engine
        plan = engine.plan(region, text, dataset, collection, sort)

        lists: list[TileList | None] = [engine.cached_tile_list(r.key) for r in plan.lists]
        missing = [r for r, cached in zip(plan.lists, lists, strict=True) if cached is None]
        if missing:

            def fetch_list(r: TileListRequest) -> TileList:
                return engine.store_tile_list(r.key, self._transport.get(r.url))

            pool: _Pool[TileListRequest, TileList] = _Pool(missing, fetch_list, len(missing), fail_fast=True)
            pool.wait()  # raises the first failure as soon as it is known
            fetched = iter(pool.results)
            lists = [cached if cached is not None else _value(next(fetched)) for cached in lists]
        ready = [tile_list for tile_list in lists if tile_list is not None]

        urls = engine.tile_urls(ready)
        # A failed tile is an outcome, not an exception: anything a worker raises fails the search, and
        # the pool stops claiming tiles as soon as one does.
        tiles: _Pool[str, Outcome] = _Pool(urls, self._tile, MAX_TILES_IN_FLIGHT, fail_fast=True)
        try:
            tiles.wait()
        except KeyboardInterrupt:
            raise
        except BaseException as e:
            # A MemoryError among the workers' errors wins, as it does in the async client. One that arrives
            # after this, from a request still in flight, is lost: waiting for those would hold up the raise,
            # and Ctrl-C, for as long as the slowest request takes.
            exhausted = [v for r in tiles.results if r is not None and isinstance(v := r[1], MemoryError)]
            raise exhausted[0] if exhausted else e  # noqa: B904
        outcomes: list[Outcome] = [_value(result) for result in tiles.results]  # type: ignore[misc]
        return returned(engine.assemble(plan, urls, outcomes, any(tile_list.overview for tile_list in ready)))

    def _tile(self, url: str) -> Outcome:
        """A tile, or why it failed. Only the transport call is guarded here; the engine guards the rest."""
        engine = self._engine
        entry = engine.cached_tile(url)  # checked when a worker reaches the URL, as the reference does
        if entry is not None:
            payload = engine.memo(url, entry)
            return (entry, payload) if payload is not None else engine.reparse(url, entry)
        request = engine.tile_request(url)
        if isinstance(request, TileFailure):
            return request
        try:
            response = engine.check_response(self._transport.get(request))
        except MemoryError:
            raise
        except Exception as e:  # whatever the transport raises; KeyboardInterrupt is a BaseException and passes
            return engine.transport_failure(url, e)
        return engine.load_tile(url, response)

    def get_place(self, origin: str) -> PlaceRecord:
        """A place by origin (or, for a record with none, by its tile record's uri)."""

        def run() -> PlaceRecord:
            cached = self._engine.cached_place(origin)
            if cached is not None:
                return cached
            return self._engine.read_place(origin, self._transport.get(self._engine.place_url(origin)))

        return _on_worker(run)

    def describe_service(self) -> ServiceDescription:
        """The server's DID and tilesets; cached for an hour."""

        def run() -> ServiceDescription:
            cached = self._engine.cached_service()
            if cached is not None:
                return cached
            return self._engine.store_service(self._transport.get(self._engine.service_url()))

        return _on_worker(run)

    def describe_tileset(self, slug: str) -> Tileset:
        """A tileset's descriptor; not cached."""
        return _on_worker(lambda: self._engine.read_tileset(self._transport.get(self._engine.tileset_url(slug))))

    def close(self) -> None:
        """Close the transport, if the client made it."""
        if self._owns_transport:
            self._transport.close()  # type: ignore[attr-defined]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _on_worker(fn: Callable[[], _O]) -> _O:
    """Run a call on one of the client's own threads, never the app's: a response is parsed there, with
    the 8 MiB stack deeply nested JSON needs, and the wait is in slices, so Ctrl-C returns control."""
    pool: _Pool[None, _O] = _Pool([None], lambda _: fn(), 1, fail_fast=True)
    pool.wait()
    return _value(pool.results[0])


def _value(result: tuple[bool, _O | BaseException] | None) -> _O:
    assert result is not None
    assert result[0]
    return result[1]  # type: ignore[return-value]
