"""AiohttpTransport, the async client's default. Needs the ``async`` extra (``pip install "atgeo[async]"``)."""

from __future__ import annotations

import asyncio
import base64
import ssl
import urllib.parse
from typing import Any

import aiohttp
import yarl

from ._body import BodyError, CappedBody
from ._proxy import Proxies
from ._tls import DEFAULT_MAX_BODY_BYTES, default_ssl_context
from ._urls import MAX_REDIRECTS, RedirectError, next_hop
from .transport import HEADERS, HttpResponse


class AiohttpTransport:
    """An aiohttp transport with no cookies, no ``.netrc`` and no total timeout (the task can be cancelled).

    Its session is made on the first request, inside the running event loop, and belongs to that loop:
    a request from another loop raises RuntimeError. Make one per event loop. After ``aclose()`` it refuses
    every request with RuntimeError.
    """

    def __init__(
        self,
        *,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._max_body_bytes = max_body_bytes
        self._ssl_context = ssl_context if ssl_context is not None else default_ssl_context()
        self._proxies = Proxies()
        self._session: aiohttp.ClientSession | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    def _session_for_running_loop(self) -> aiohttp.ClientSession:
        # Closed is final: a new session made after aclose() would never be closed.
        if self._closed:
            raise RuntimeError("this AiohttpTransport is closed")
        loop = asyncio.get_running_loop()
        if self._session is None:
            self._loop = loop
            self._session = aiohttp.ClientSession(
                auto_decompress=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(
                    total=None, sock_connect=self._connect_timeout, sock_read=self._read_timeout
                ),
                connector=aiohttp.TCPConnector(ssl=self._ssl_context),
            )
        elif loop is not self._loop:
            raise RuntimeError(
                "AiohttpTransport belongs to the event loop of its first request; make one client per event loop"
            )
        return self._session

    def _request_options(self, url: str) -> dict[str, Any]:
        """The headers and proxy options for one request."""
        proxy = self._proxies.for_url(url)
        if proxy is None:
            return {"headers": HEADERS}
        # The credentials go as a header, never in the proxy URL, so that aiohttp's errors (which quote that
        # URL) never carry them. An https request tunnels with CONNECT, which carries proxy_headers, and our
        # User-Agent rather than aiohttp's, which names its version. A plain http request goes to the proxy in
        # absolute form, with its own headers; only then may Proxy-Authorization join them, since an https
        # request's headers travel through the tunnel to the server.
        auth = None
        if proxy.basic_auth is not None:
            auth = "Basic " + base64.b64encode(proxy.basic_auth.encode()).decode()
        if urllib.parse.urlsplit(url).scheme == "https":
            connect = {"User-Agent": HEADERS["User-Agent"], **({"Proxy-Authorization": auth} if auth else {})}
            return {"headers": HEADERS, "proxy": proxy.url, "proxy_headers": connect}
        return {"headers": {**HEADERS, **({"Proxy-Authorization": auth} if auth else {})}, "proxy": proxy.url}

    async def get(self, url: str) -> HttpResponse:
        session = self._session_for_running_loop()
        for _ in range(MAX_REDIRECTS + 1):
            # encoded=True: otherwise yarl requotes the URL, and a tile URL is fetched as the server gave it.
            async with session.get(
                yarl.URL(url, encoded=True),
                allow_redirects=False,
                **self._request_options(url),
            ) as r:
                target = next_hop(url, r.status, r.headers.get("Location"))
                if target is None:
                    return HttpResponse(r.status, await self._read(r))
            url = target  # leaving the block unread closes the redirect's connection
        raise RedirectError(f"more than {MAX_REDIRECTS} redirects")

    async def _read(self, r: aiohttp.ClientResponse) -> bytes:
        try:
            body = CappedBody(r.headers.get("Content-Encoding"), self._max_body_bytes)
            async for chunk in r.content.iter_chunked(64 * 1024):
                body.feed(chunk)
            return body.finish()
        except BodyError:
            raise
        except (aiohttp.ClientError, TimeoutError) as e:  # a cut body; a stall past sock_read
            raise BodyError(f"bad body: {e!r}") from e

    async def aclose(self) -> None:
        """Close the session. The transport refuses every request after this."""
        self._closed = True
        if self._session is not None:
            await self._session.close()
