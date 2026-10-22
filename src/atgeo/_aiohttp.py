"""AiohttpTransport, the async client's default. Needs the ``async`` extra (``pip install "atgeo[async]"``)."""

from __future__ import annotations

import asyncio
import ssl

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
    a request from another loop raises RuntimeError. Make one per event loop.
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

    def _session_for_running_loop(self) -> aiohttp.ClientSession:
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

    def _proxy_for(self, url: str) -> str | None:
        proxy = self._proxies.for_url(url)
        return proxy.raw if proxy is not None else None  # aiohttp sends the URL's credentials itself

    async def get(self, url: str) -> HttpResponse:
        session = self._session_for_running_loop()
        for _ in range(MAX_REDIRECTS + 1):
            # encoded=True: otherwise yarl requotes the URL, and a tile URL is fetched as the server gave it.
            async with session.get(
                yarl.URL(url, encoded=True),
                allow_redirects=False,
                headers=HEADERS,
                proxy=self._proxy_for(url),
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
        if self._session is not None:
            await self._session.close()
