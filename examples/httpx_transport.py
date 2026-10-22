"""A Transport over httpx, for an app that already uses it.

Copy it into your app; it is not part of the atgeo package. It does what the default transports do:
no redirect following of its own (``next_hop`` decides, and a redirect's body is never read), raw bytes
through ``CappedBody``, no cookies, no ``.netrc`` (``trust_env`` off, proxies chosen here), and a
deadline on the whole response. httpx does not retry.

    client = AtgeoClient(transport=HttpxTransport())
"""

from __future__ import annotations

import http.cookiejar
import threading
import time
import urllib.parse
import urllib.request

import httpx

from atgeo.transport import HEADERS, MAX_REDIRECTS, BodyError, CappedBody, HttpResponse, RedirectError, next_hop


class _NoCookies(http.cookiejar.DefaultCookiePolicy):
    def set_ok(self, cookie: http.cookiejar.Cookie, request: urllib.request.Request) -> bool:
        return False


class HttpxTransport:
    def __init__(
        self,
        *,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        total_timeout: float = 120.0,
        max_body_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        self._timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
        self._total_timeout = total_timeout
        self._max_body_bytes = max_body_bytes
        self._proxies = urllib.request.getproxies()
        self._clients: dict[str | None, httpx.Client] = {}  # one per proxy, None for direct
        self._lock = threading.Lock()

    def _client_for(self, url: str) -> httpx.Client:
        parts = urllib.parse.urlsplit(url)
        proxy = self._proxies.get(parts.scheme) or self._proxies.get("all")
        if proxy and urllib.request.proxy_bypass(parts.netloc.rsplit("@", 1)[-1]):
            proxy = None
        with self._lock:
            client = self._clients.get(proxy)
            if client is None:
                client = self._clients[proxy] = httpx.Client(
                    trust_env=False,
                    follow_redirects=False,
                    cookies=http.cookiejar.CookieJar(policy=_NoCookies()),
                    timeout=self._timeout,
                    proxy=proxy,
                )
            return client

    def get(self, url: str) -> HttpResponse:
        deadline = time.monotonic() + self._total_timeout
        for _ in range(MAX_REDIRECTS + 1):
            try:
                with self._client_for(url).stream("GET", url, headers=HEADERS) as r:
                    target = next_hop(url, r.status_code, r.headers.get("Location"))
                    if target is None:
                        return HttpResponse(r.status_code, self._read(r, deadline))
            except (httpx.InvalidURL, httpx.UnsupportedProtocol) as e:
                # httpx builds the next request even with follow_redirects=False, and refuses a redirect to
                # data: or file: itself (closing the response) before next_hop sees it.
                raise RedirectError(f"bad redirect from {url!r}: {e}") from e
            url = target
        raise RedirectError(f"more than {MAX_REDIRECTS} redirects")

    def _read(self, r: httpx.Response, deadline: float) -> bytes:
        try:
            body = CappedBody(r.headers.get("Content-Encoding"), self._max_body_bytes)
            for chunk in r.iter_raw():  # as it arrives, undecoded
                body.feed(chunk)
                if time.monotonic() > deadline:
                    raise BodyError("response took longer than total_timeout")
            return body.finish()
        except BodyError:
            raise
        except httpx.HTTPError as e:
            raise BodyError(f"bad body: {e!r}") from e

    def close(self) -> None:
        with self._lock:
            for client in self._clients.values():
                client.close()
            self._clients.clear()
