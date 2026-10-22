"""Both clients through one fixture: ``make`` builds a blocking adapter over the sync client or the async
client, so that every client-driven test runs against both. The async client runs each call under its own
asyncio.run, which also exercises its lack of loop-bound state (the fake transport is not loop-bound)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from typing import Any

import pytest

from atgeo import AsyncAtgeoClient, AtgeoClient, PlaceRecord, SearchResult, ServiceDescription, Tileset

from .fake_server import FakeServer

PROXY_VARS = ["http_proxy", "https_proxy", "all_proxy", "no_proxy"]


@pytest.fixture(autouse=True)
def no_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tests set their own proxies; the environment's must not leak into them."""
    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


class Client:
    """A blocking face over either client."""

    def __init__(self, kind: str, fake: FakeServer, **opts: Any) -> None:
        self.kind = kind
        self.fake = fake
        self.inner: AtgeoClient | AsyncAtgeoClient
        if kind == "sync":
            self.inner = AtgeoClient(transport=fake.transport, **opts)
        else:
            self.inner = AsyncAtgeoClient(transport=fake.async_transport, **opts)

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        result = getattr(self.inner, name)(*args, **kwargs)
        return asyncio.run(result) if self.kind == "async" else result

    def search_places(self, region: Any, **kwargs: Any) -> SearchResult:
        return self._call("search_places", region, **kwargs)  # type: ignore[no-any-return]

    def get_place(self, origin: str) -> PlaceRecord:
        return self._call("get_place", origin)  # type: ignore[no-any-return]

    def describe_service(self) -> ServiceDescription:
        return self._call("describe_service")  # type: ignore[no-any-return]

    def describe_tileset(self, slug: str) -> Tileset:
        return self._call("describe_tileset", slug)  # type: ignore[no-any-return]


Make = Callable[..., Client]


@pytest.fixture(params=["sync", "async"])
def make(request: pytest.FixtureRequest) -> Make:
    return partial(Client, request.param)
