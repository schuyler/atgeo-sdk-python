"""A request-capturing fake garganorn, after test/helpers/fakeServer.ts.

One routing core with two faces, ``.transport`` (a Transport) and ``.async_transport`` (an
AsyncTransport), so that every client-driven test runs against both clients. There is no Python
validator for atproto lexicons, so it checks the contract's minimum (§13) plus what fakeServer.test.ts
asserts; the lexicons stay vendored in tests/fixtures/lexicon for a validator later.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from atgeo.transport import HttpResponse, prepare_url

DEFAULT_SERVER = "https://places.atgeo.org"
PUBLISHED_AT = "2026-01-01T00:00:00Z"
MAX_TILES = 50  # the tile budget describeService reports; constant, as garganorn serves it
BBOX = re.compile(r"^-?\d+\.\d\d,-?\d+\.\d\d,-?\d+\.\d\d,-?\d+\.\d\d$")
# format: uri, as the lexicon demands it: a scheme, a colon, optional //, then non-space characters not
# starting with /. "not a uri" fails; urn:atgeo:... and https://... pass.
URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:(?://)?[^\s/][^\s]*$")

PARAMS: dict[str, tuple[set[str], set[str]]] = {  # nsid: (required, optional)
    "org.atgeo.getTiles": ({"dataset", "bbox"}, {"collection"}),
    "org.atgeo.getPlace": ({"uri"}, set()),
    "org.atgeo.describeService": (set(), set()),
    "org.atgeo.describeTileset": ({"slug"}, set()),
}


@dataclass
class FakeTile:
    url: str
    records: list[dict[str, Any]]


@dataclass
class FakeTileset:
    slug: Any
    collections: list[dict[str, Any]]
    tiles: list[FakeTile] = field(default_factory=list)
    references: list[dict[str, Any]] | None = None
    overview: dict[str, Any] | None = None


def overture(tiles: Sequence[FakeTile]) -> FakeTileset:
    return FakeTileset("overture-place", [{"dataset": "overturemaps.org", "collection": "place"}], list(tiles))


# What tiles_for returns: a URL list, {"tiles": ..., "overview": ...}, or {"error": ..., "message": ...}.
TilesFor = Callable[[str], list[str] | dict[str, Any]]


def _json(body: object, status: int = 200) -> HttpResponse:
    return HttpResponse(status, json.dumps(body, ensure_ascii=False).encode())


def _error(error: str, message: str, status: int = 400) -> HttpResponse:
    return _json({"error": error, "message": message}, status)


def _attribution(c: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("dataset", "collection", "release", "source", "licenses", "attribution")
    return {k: c[k] for k in keys if k in c}


class FakeServer:
    def __init__(
        self,
        *,
        server: str = DEFAULT_SERVER,
        tilesets: Sequence[FakeTileset] | None = None,
        tiles_for: TilesFor | None = None,
        fail_tiles: Sequence[str] = (),
        responses: Mapping[str, HttpResponse] | None = None,
        delays: Mapping[str, float] | None = None,
        network_errors: Sequence[str] = (),
        answer: Callable[[str], HttpResponse | None] | None = None,
    ) -> None:
        self.server = server
        self.tilesets = list(tilesets) if tilesets is not None else [overture([])]
        self.tiles_for = tiles_for
        self.fail_tiles = set(fail_tiles)
        self.responses = dict(responses or {})
        self.delays = dict(delays or {})
        self.network_errors = set(network_errors)
        self.answer = answer  # consulted first, for every URL; None falls through to the routing
        self.requests: list[str] = []
        self.in_flight = 0
        self.peak_in_flight = 0
        self._offline = False
        self._lock = threading.Lock()
        for tileset in self.tilesets:
            self._validate(tileset)
        self.transport = _SyncFace(self)
        self.async_transport = _AsyncFace(self)

    @staticmethod
    def _validate(tileset: FakeTileset) -> None:
        if type(tileset.slug) is not str:
            raise ValueError("tileset descriptor: slug must be a string")
        for tile in tileset.tiles:
            for r in tile.records:
                if type(r) is not dict or type(r.get("value")) is not dict:
                    raise ValueError('tile record: "value" must be an object')
                if type(r["value"].get("name")) is not str:
                    raise ValueError("place record: name must be a string")
                if "importance" in r and type(r["importance"]) is not int:
                    raise ValueError("tile record: importance must be an integer")

    def go_offline(self) -> None:
        """After this, every request fails as a network failure."""
        with self._lock:
            self._offline = True

    # The faces.

    def _enter(self, url: str) -> float:
        with self._lock:
            self.requests.append(url)
            if self._offline or url in self.network_errors:
                raise ConnectionError("network disabled")
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        return self.delays.get(url, 0.0)

    def _leave(self) -> None:
        with self._lock:
            self.in_flight -= 1

    # Routing.

    def handle(self, url: str) -> HttpResponse:
        if self.answer is not None and (answered := self.answer(url)) is not None:
            return answered
        if url in self.responses:
            return self.responses[url]
        if url.startswith(f"{self.server}/xrpc/"):
            parts = urllib.parse.urlsplit(url)
            nsid = parts.path.rsplit("/", 1)[-1]
            if nsid not in PARAMS:
                return _error("MethodNotImplemented", f"Method {nsid} not implemented", 501)
            params = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
            required, optional = PARAMS[nsid]
            unknown = set(params) - required - optional
            if unknown:
                return _error("InvalidRequest", f"Unknown parameters: {sorted(unknown)}")
            if required - set(params):
                return _error("InvalidRequest", f"Missing required parameters: {sorted(required - set(params))}")
            if "bbox" in params and not BBOX.match(params["bbox"]):
                return _error("InvalidRequest", f"bbox must be two-decimal: {params['bbox']}")
            if "uri" in params and not URI.match(params["uri"]):
                return _error("InvalidRequest", "uri must be a valid uri")
            return self._xrpc(nsid, params)
        for tileset in self.tilesets:
            for tile in tileset.tiles:
                if url in (tile.url, prepare_url(tile.url)):  # the clients send a non-ASCII URL percent-encoded
                    if url in self.fail_tiles:
                        return HttpResponse(500, b"upstream error")
                    return _json(self._tile_body(tileset, tile.records))
        return HttpResponse(404, b"Not found")

    @staticmethod
    def _attribution_list(tileset: FakeTileset) -> list[dict[str, Any]]:
        """What every tile of the tileset carries and getPlace answers with: its collections, then its references."""
        return [_attribution(c) for c in tileset.collections] + list(tileset.references or [])

    def _tile_body(self, tileset: FakeTileset, records: list[dict[str, Any]]) -> dict[str, Any]:
        return {"publishedAt": PUBLISHED_AT, "attribution": self._attribution_list(tileset), "records": records}

    @staticmethod
    def _descriptor(t: FakeTileset) -> dict[str, Any]:
        d: dict[str, Any] = {"slug": t.slug, "collections": t.collections}
        if t.references is not None:
            d["references"] = t.references
        if t.overview is not None:
            d["overview"] = t.overview
        return d

    def _xrpc(self, nsid: str, params: dict[str, str]) -> HttpResponse:
        if nsid == "org.atgeo.getTiles":
            return self._get_tiles(params)
        if nsid == "org.atgeo.getPlace":
            return self._get_place(params["uri"])
        if nsid == "org.atgeo.describeService":
            host = urllib.parse.urlsplit(self.server).hostname
            return _json(
                {
                    "did": f"did:web:{host}",
                    "maxTiles": MAX_TILES,
                    "tilesets": [
                        {
                            "slug": t.slug,
                            "collections": [
                                {"dataset": c["dataset"], "collection": c["collection"]} for c in t.collections
                            ],
                        }
                        for t in self.tilesets
                    ],
                }
            )
        slug = params["slug"]
        for t in self.tilesets:
            if t.slug == slug:
                return _json(self._descriptor(t))
        return _error("TilesetNotFound", f"Tileset {slug} not found")

    def _get_tiles(self, params: dict[str, str]) -> HttpResponse:
        dataset, collection, bbox = params["dataset"], params.get("collection"), params["bbox"]
        with_dataset = [t for t in self.tilesets if any(c["dataset"] == dataset for c in t.collections)]
        if not with_dataset:
            return _error("DatasetNotFound", f"Dataset {dataset} not found")
        selected = [
            t
            for t in with_dataset
            if any(
                c["dataset"] == dataset and (collection is None or c["collection"] == collection) for c in t.collections
            )
        ]
        if not selected:
            return _error("CollectionNotFound", f"Collection {collection} not found")
        urls = sorted(tile.url for t in selected for tile in t.tiles)
        tiles = self.tiles_for(bbox) if self.tiles_for else urls
        if isinstance(tiles, list):
            return _json({"tiles": tiles})
        if "error" in tiles:
            return _error(tiles["error"], tiles["message"])
        return _json(tiles)

    def _get_place(self, origin: str) -> HttpResponse:
        parts = origin.split(":")
        dataset, collection, rest = (parts[2], parts[3], parts[4:]) if len(parts) >= 4 else ("", "", [])
        for t in self.tilesets:
            if not any(c["dataset"] == dataset and c["collection"] == collection for c in t.collections):
                continue
            for tile in t.tiles:
                for r in tile.records:
                    if r["value"].get("origin") == origin:
                        body: dict[str, Any] = {
                            "uri": origin,
                            "url": f"{self.server}/{dataset}/{collection}/{':'.join(rest)}",
                        }
                        if "importance" in r:
                            body["importance"] = r["importance"]
                        body["value"] = r["value"]
                        body["attribution"] = self._attribution_list(t)
                        return _json(body)
        return _error("PlaceNotFound", f"Place {origin} not found")


class _SyncFace:
    def __init__(self, fake: FakeServer) -> None:
        self._fake = fake

    def get(self, url: str) -> HttpResponse:
        delay = self._fake._enter(url)
        try:
            if delay:
                time.sleep(delay)
            return self._fake.handle(url)
        finally:
            self._fake._leave()


class _AsyncFace:
    def __init__(self, fake: FakeServer) -> None:
        self._fake = fake

    async def get(self, url: str) -> HttpResponse:
        delay = self._fake._enter(url)
        try:
            if delay:
                await asyncio.sleep(delay)
            return self._fake.handle(url)
        finally:
            self._fake._leave()
