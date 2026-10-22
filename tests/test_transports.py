"""The default transports, and CappedBody, against a raw-socket loopback server. No reference counterpart.

Every case runs against every transport: the two defaults, and the adapters in examples/ when their
library is installed (CI's examples job installs them).
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import os
import threading
import time
import urllib.parse
import zlib
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import urllib3.exceptions

from atgeo._aiohttp import AiohttpTransport
from atgeo._urllib3 import Urllib3Transport
from atgeo.transport import HEADERS, BodyError, CappedBody, HttpResponse, RedirectError, prepare_url

from .loopback import Loopback, Request, Writer

PROXY_VARS = ["http_proxy", "https_proxy", "all_proxy", "no_proxy"]


@pytest.fixture(autouse=True)
def no_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def server() -> Iterator[Loopback]:
    with Loopback() as s:
        yield s


class Kind:
    """One transport under test: ``fetch`` makes a transport, GETs once and closes it; ``session`` GETs
    several URLs through one transport."""

    def __init__(self, name: str, factory: Callable[..., Any], is_async: bool = False) -> None:
        self.name, self.factory, self.is_async = name, factory, is_async

    def __repr__(self) -> str:
        return self.name

    def session(self, urls: list[str], **opts: Any) -> list[HttpResponse]:
        if self.is_async:
            opts.pop("total_timeout", None)

            async def run() -> list[HttpResponse]:
                t = self.factory(**opts)
                try:
                    return [await t.get(u) for u in urls]
                finally:
                    await t.aclose()

            return asyncio.run(run())
        t = self.factory(**opts)
        try:
            return [t.get(u) for u in urls]
        finally:
            t.close()

    def __call__(self, url: str, **opts: Any) -> HttpResponse:
        return self.session([url], **opts)[0]


KINDS = [Kind("urllib3", Urllib3Transport), Kind("aiohttp", AiohttpTransport, is_async=True)]
try:
    from examples.requests_transport import RequestsTransport

    KINDS.append(Kind("requests", RequestsTransport))
except ImportError:
    pass
try:
    from examples.httpx_transport import HttpxTransport

    KINDS.append(Kind("httpx", HttpxTransport))
except ImportError:
    pass

SYNC_KINDS = [k for k in KINDS if not k.is_async]

# "No answer arrived": what each library raises before a response, unwrapped (Python spec §3).
NO_ANSWER: tuple[type[BaseException], ...] = (urllib3.exceptions.HTTPError, OSError)
try:
    import httpx

    NO_ANSWER += (httpx.HTTPError,)
except ImportError:
    pass


@pytest.fixture(params=KINDS, ids=repr)
def fetch(request: pytest.FixtureRequest) -> Kind:
    return request.param  # type: ignore[no-any-return]


def gz(data: bytes) -> bytes:
    return gzip.compress(data, mtime=0)


CAP = 100_000


class TestStatuses:
    def test_returns_a_4xx_with_its_json_error_body(self, server: Loopback, fetch: Kind) -> None:
        body = b'{"error":"PlaceNotFound","message":"nope"}'
        server.handler = lambda _r, w: w.respond(404, body, [("Content-Type", "application/json")])
        assert fetch(server.url("/x")) == HttpResponse(404, body)

    def test_returns_a_4xx_with_an_empty_body(self, server: Loopback, fetch: Kind) -> None:
        server.handler = lambda _r, w: w.respond(429)
        assert fetch(server.url("/x")) == HttpResponse(429, b"")

    def test_returns_a_500_without_raising(self, server: Loopback, fetch: Kind) -> None:
        server.handler = lambda _r, w: w.respond(500)
        assert fetch(server.url("/x")) == HttpResponse(500, b"")


def redirecting(final: Callable[[Request, Writer], None] | None = None) -> Callable[[Request, Writer], None]:
    """/hop/N redirects to /hop/N-1; /hop/0 answers ok."""

    def handle(r: Request, w: Writer) -> None:
        if r.target.startswith("/hop/"):
            n = int(r.target.rsplit("/", 1)[1])
            if n > 0:
                w.respond(302, b"", [("Location", f"/hop/{n - 1}")])
                return
        if final:
            final(r, w)
        else:
            w.respond(200, b"done")

    return handle


class TestRedirects:
    def test_follows_a_same_scheme_redirect(self, server: Loopback, fetch: Kind) -> None:
        server.handler = redirecting()
        assert fetch(server.url("/hop/1")) == HttpResponse(200, b"done")

    def test_follows_ten_hops_and_refuses_an_eleventh(self, server: Loopback, fetch: Kind) -> None:
        server.handler = redirecting()
        assert fetch(server.url("/hop/10")).body == b"done"
        server.requests.clear()
        with pytest.raises(RedirectError):
            fetch(server.url("/hop/11"))
        assert len(server.requests) == 11  # the first request and ten hops; the eleventh redirect is not followed

    @pytest.mark.parametrize("location", ["file:///etc/hostname", "ftp://127.0.0.1/x", "data:,hi"])
    def test_refuses_a_redirect_to_another_scheme(self, server: Loopback, fetch: Kind, location: str) -> None:
        server.handler = lambda _r, w: w.respond(302, b"", [("Location", location)])
        with pytest.raises(RedirectError):
            fetch(server.url("/x"))

    def test_never_reads_a_redirects_body(self, server: Loopback, fetch: Kind) -> None:
        megabyte = b"x" * (1024 * 1024)

        def handle(r: Request, w: Writer) -> None:
            if r.target == "/big":
                w.head(302, [("Location", "/ok"), ("Transfer-Encoding", "chunked")])
                for _ in range(300):  # 300 MiB, if anyone reads it
                    w.chunk(megabyte)
                w.end_chunks()
            else:
                w.respond(200, b"ok")

        server.handler = handle
        start = time.monotonic()
        assert fetch(server.url("/big")) == HttpResponse(200, b"ok")
        assert time.monotonic() - start < 10
        deadline = time.monotonic() + 5
        while not server.closed and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.closed, "the redirect's connection was not closed"
        assert server.sent < 40 * 1024 * 1024  # what the socket buffers took before the client hung up

    def test_sends_no_referer_on_any_hop(self, server: Loopback, fetch: Kind) -> None:
        server.handler = redirecting()
        fetch(server.url("/hop/3"))
        assert len(server.requests) == 4
        assert all(r.header("Referer") is None for r in server.requests)
        assert len({tuple(r.header_names) for r in server.requests}) == 1


def body_handler(raw: bytes, headers: list[tuple[str, str]] | None = None) -> Callable[[Request, Writer], None]:
    return lambda _r, w: w.respond(200, raw, headers or [])


GZIP = [("Content-Encoding", "gzip")]


class TestBodies:
    """Python spec §4's list, with a 100,000-byte cap; both transports accept and reject the same bodies."""

    def test_reads_a_body_of_exactly_the_cap(self, server: Loopback, fetch: Kind) -> None:
        server.handler = body_handler(b"a" * CAP)
        assert fetch(server.url(), max_body_bytes=CAP).body == b"a" * CAP

    def test_reads_a_gzip_body_decoding_to_exactly_the_cap(self, server: Loopback, fetch: Kind) -> None:
        server.handler = body_handler(gz(b"a" * CAP), GZIP)
        assert fetch(server.url(), max_body_bytes=CAP).body == b"a" * CAP

    @pytest.mark.parametrize(
        ("raw", "headers"),
        [
            pytest.param(b"a" * (CAP + 1), [], id="one byte more"),
            pytest.param(gz(b"a" * (CAP + 1)), GZIP, id="gzip decoding to one byte more"),
            pytest.param(gz(b"\0" * 5_000_000), GZIP, id="a 5 MB gzip bomb"),
            pytest.param(gz(b"hello world" * 100)[:-10], GZIP, id="truncated gzip"),
            pytest.param(gz(b"hello") + b"garbage", GZIP, id="trailing garbage"),
            pytest.param(b"\x1b\x00\x00", [("Content-Encoding", "br")], id="an unrequested br body"),
        ],
    )
    def test_refuses(self, server: Loopback, fetch: Kind, raw: bytes, headers: list[tuple[str, str]]) -> None:
        server.handler = body_handler(raw, headers)
        with pytest.raises(BodyError):
            fetch(server.url(), max_body_bytes=CAP)

    def test_refuses_a_body_short_of_its_content_length(self, server: Loopback, fetch: Kind) -> None:
        def handle(_r: Request, w: Writer) -> None:
            w.head(200, [("Content-Length", "100")])
            w.send(b"x" * 50)
            w.close()

        server.handler = handle
        with pytest.raises(BodyError):
            fetch(server.url())

    def test_refuses_a_cut_chunked_body(self, server: Loopback, fetch: Kind) -> None:
        def handle(_r: Request, w: Writer) -> None:
            w.head(200, [("Transfer-Encoding", "chunked")])
            w.chunk(b"x" * 50)
            w.close()

        server.handler = handle
        with pytest.raises(BodyError):
            fetch(server.url())

    def test_reads_an_empty_body_labelled_gzip(self, server: Loopback, fetch: Kind) -> None:
        server.handler = body_handler(b"", GZIP)
        assert fetch(server.url()).body == b""

        def chunked(_r: Request, w: Writer) -> None:
            w.head(200, [*GZIP, ("Transfer-Encoding", "chunked")])
            w.end_chunks()

        server.handler = chunked
        assert fetch(server.url()).body == b""

    def test_concatenates_two_gzip_members(self, server: Loopback, fetch: Kind) -> None:
        server.handler = body_handler(gz(b"one ") + gz(b"two"), GZIP)
        assert fetch(server.url()).body == b"one two"

    def test_reads_a_large_gzipped_chunked_tile_byte_for_byte(self, server: Loopback, fetch: Kind) -> None:
        tile = os.urandom(512 * 1024).hex().encode()  # 1 MiB of text

        def handle(_r: Request, w: Writer) -> None:
            data = gz(tile)
            w.head(200, [*GZIP, ("Transfer-Encoding", "chunked")])
            for i in range(0, len(data), 7919):
                w.chunk(data[i : i + 7919])
            w.end_chunks()

        server.handler = handle
        assert fetch(server.url()).body == tile


class TestCappedBody:
    def test_concatenates_two_members_split_at_every_byte_offset(self) -> None:
        data = gz(b"one ") + gz(b"two")
        for k in range(len(data) + 1):
            body = CappedBody("gzip", CAP)
            body.feed(data[:k])
            body.feed(data[k:])
            assert body.finish() == b"one two", k

    def test_stops_a_gzip_bomb_early(self) -> None:
        bomb = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
        data = bomb.compress(b"\0" * (64 * 1024 * 1024)) + bomb.flush()
        body = CappedBody("gzip", 1024 * 1024)
        start = time.monotonic()
        with pytest.raises(BodyError, match="decoded"):
            body.feed(data)
        assert time.monotonic() - start < 2

    @pytest.mark.parametrize("encoding", [None, "identity", " GZIP ", "x-gzip"])
    def test_accepts_the_encodings_it_asks_for(self, encoding: str | None) -> None:
        body = CappedBody(encoding, CAP)
        body.feed(b"" if encoding in (None, "identity") else gz(b""))
        assert body.finish() == b""

    @pytest.mark.parametrize("encoding", ["br", "deflate", "zstd", "gzip, br"])
    def test_refuses_any_other_encoding(self, encoding: str) -> None:
        with pytest.raises(BodyError):
            CappedBody(encoding, CAP)


def trickle(n: int, gap: float, then_stall: float = 0) -> Callable[[Request, Writer], None]:
    def handle(_r: Request, w: Writer) -> None:
        w.head(200, [("Content-Length", str(n))])
        for i in range(n):
            w.send(b"x")
            time.sleep(gap)
            if then_stall and i == n // 2:
                time.sleep(then_stall)

    return handle


class TestTimeouts:
    def test_reads_a_trickle_longer_than_the_read_timeout(self, server: Loopback, fetch: Kind) -> None:
        server.handler = trickle(8, 0.15)  # 1.2 s in all, never 0.5 s without a byte
        assert fetch(server.url(), read_timeout=0.5).body == b"x" * 8

    def test_refuses_a_body_that_stalls(self, server: Loopback, fetch: Kind) -> None:
        server.handler = trickle(4, 0.01, then_stall=2)
        start = time.monotonic()
        with pytest.raises(BodyError):
            fetch(server.url(), read_timeout=0.5)
        assert time.monotonic() - start < 1.5

    def test_sends_one_request_for_a_read_timeout(self, server: Loopback, fetch: Kind) -> None:
        server.handler = lambda _r, _w: time.sleep(1.5)
        with pytest.raises(NO_ANSWER):
            fetch(server.url(), read_timeout=0.3)
        time.sleep(0.3)
        assert len(server.requests) == 1

    @pytest.mark.parametrize("kind", SYNC_KINDS, ids=repr)
    def test_bounds_a_whole_response_at_total_timeout(self, server: Loopback, kind: Kind) -> None:
        server.handler = trickle(40, 0.1)  # 4 s
        start = time.monotonic()
        with pytest.raises(BodyError):
            kind(server.url(), read_timeout=1, total_timeout=0.5)
        assert time.monotonic() - start < 1.5


class TestRequests:
    def test_sends_the_same_fixed_headers_on_every_request(self, server: Loopback, fetch: Kind) -> None:
        fetch(server.url("/a"))
        fetch(server.url("/b?bbox=1,2,3,4"))
        first, second = server.requests
        assert first.header_names == second.header_names
        assert first.header("User-Agent") == "atgeo-client-python"
        assert first.header("Accept-Language") is None
        assert first.header("Accept") == "application/json"
        assert first.header("Accept-Encoding") == "gzip"
        assert set(first.header_names) <= {"host", "connection", *(k.lower() for k in HEADERS)}

    def test_keeps_no_cookie(self, server: Loopback, fetch: Kind) -> None:
        server.handler = lambda _r, w: w.respond(200, b"ok", [("Set-Cookie", "id=1; Path=/"), ("ETag", '"e"')])
        fetch.session([server.url("/a"), server.url("/b")])  # one transport: a fresh one would prove nothing
        assert server.requests[1].header("Cookie") is None
        assert server.requests[1].header("If-None-Match") is None

    def test_reuses_one_connection_for_sequential_requests(self, server: Loopback) -> None:
        t = Urllib3Transport()
        try:
            for i in range(20):
                t.get(server.url(f"/{i}"))
        finally:
            t.close()
        assert server.connections == 1

    def test_sends_escapes_byte_for_byte(self, server: Loopback, fetch: Kind) -> None:
        path = "/t/%7e%27?bbox=1%2C2%2C3%2C4&c=a%3Ab"
        fetch(server.url(path))
        sent = server.requests[0].target
        if fetch.name in ("aiohttp", "urllib3"):
            assert sent.lower() == path.lower() if fetch.name == "urllib3" else sent == path
        else:
            # requests unquotes unreserved characters (%7e to ~); every form decodes the same.
            assert urllib.parse.unquote(sent) == urllib.parse.unquote(path)

    def test_sends_a_prepared_url_as_prepared(self, server: Loopback, fetch: Kind) -> None:
        url = prepare_url(server.url("/t/a b\tc/café.json"))
        assert url.endswith("/t/a%20bc/caf%C3%A9.json")
        fetch(url)
        assert server.requests[0].target == "/t/a%20bc/caf%C3%A9.json"


class TestProxies:
    @pytest.fixture
    def proxy(self) -> Iterator[Loopback]:
        """A proxy that answers absolute-form requests itself, saying what it was asked for."""
        with Loopback() as p:
            p.handler = lambda r, w: w.respond(200, f"via proxy: {r.target}".encode())
            yield p

    def test_routes_through_the_environments_proxy_unless_no_proxy_says_otherwise(
        self,
        server: Loopback,
        proxy: Loopback,
        fetch: Kind,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("http_proxy", proxy.url(""))
        monkeypatch.setenv("no_proxy", "localhost")
        assert fetch(server.url("/direct", host="localhost")).body == b"ok"
        assert fetch("http://proxied.invalid/x?a=1").body == b"via proxy: http://proxied.invalid/x?a=1"
        assert [r.target for r in server.requests] == ["/direct"]

    def test_chooses_the_proxy_afresh_on_every_hop(
        self,
        server: Loopback,
        proxy: Loopback,
        fetch: Kind,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("http_proxy", proxy.url(""))
        monkeypatch.setenv("no_proxy", "localhost")
        server.handler = lambda _r, w: w.respond(302, b"", [("Location", "http://proxied.invalid/ok")])
        assert fetch(server.url("/r", host="localhost")).body == b"via proxy: http://proxied.invalid/ok"

    def test_sends_the_proxy_urls_credentials(
        self,
        proxy: Loopback,
        fetch: Kind,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("http_proxy", f"http://user:pa%20ss@127.0.0.1:{proxy.port}")
        fetch("http://proxied.invalid/x")
        expected = "Basic " + base64.b64encode(b"user:pa ss").decode()
        assert proxy.requests[0].header("Proxy-Authorization") == expected

    @pytest.mark.parametrize("scheme", ["socks4", "socks5", "socks5h"])
    @pytest.mark.parametrize("variable", ["https_proxy", "all_proxy"])
    def test_refuses_a_socks_proxy_at_construction(
        self, scheme: str, variable: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(variable, f"{scheme}://127.0.0.1:1080")
        with pytest.raises(ValueError, match="SOCKS"):
            Urllib3Transport()
        with pytest.raises(ValueError, match="SOCKS"):
            AiohttpTransport()


class TestAiohttpTransport:
    def test_returns_cancelled_error_promptly_while_a_body_streams_and_closes_the_connection(
        self, server: Loopback
    ) -> None:
        streaming = threading.Event()

        def handle(_r: Request, w: Writer) -> None:
            w.head(200, [("Transfer-Encoding", "chunked")])
            streaming.set()
            for _ in range(200):
                w.chunk(b"x" * 1000)
                time.sleep(0.05)
            w.end_chunks()

        server.handler = handle

        async def run() -> float:
            t = AiohttpTransport()
            task = asyncio.create_task(t.get(server.url()))
            await asyncio.to_thread(streaming.wait, 5)
            await asyncio.sleep(0.1)
            task.cancel()
            start = time.monotonic()
            with pytest.raises(asyncio.CancelledError):
                await task
            elapsed = time.monotonic() - start
            await t.aclose()
            return elapsed

        assert asyncio.run(run()) < 0.2
        deadline = time.monotonic() + 3
        while not server.closed and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.closed, "the server never saw the connection close"

    def test_names_the_rule_when_used_from_a_second_event_loop(self, server: Loopback) -> None:
        def handle(_r: Request, w: Writer) -> None:
            # No pooled connection may outlive the first loop: nothing in a later loop can close it.
            w.respond(200, b"ok", [("Connection", "close")])
            w.close()

        server.handler = handle
        t = AiohttpTransport()

        async def first() -> None:
            await t.get(server.url())

        async def second() -> None:
            with pytest.raises(RuntimeError, match="one client per event loop"):
                await t.get(server.url())

        asyncio.run(first())
        try:
            asyncio.run(second())
        finally:
            asyncio.run(t.aclose())
