"""Urllib3Transport, the blocking client's default."""

from __future__ import annotations

import ssl
import threading
import time

import urllib3
import urllib3.exceptions

from ._body import BodyError, CappedBody
from ._proxy import Proxies, Proxy
from ._tls import DEFAULT_MAX_BODY_BYTES, default_ssl_context
from ._urls import MAX_REDIRECTS, RedirectError, next_hop
from .transport import HEADERS, HttpResponse

# Send each request once (contract §14 forbids retrying a tile) and leave redirects to the transport.
# Retry(total=0, redirect=10) would follow none at all, because total wins.
RETRY = urllib3.Retry(total=None, connect=0, read=0, status=0, other=0, redirect=0, raise_on_redirect=False)


class Urllib3Transport:
    """A pooled urllib3 transport with no retries, no cookies and no cache.

    It follows redirects itself, never reading a redirect's body, decodes gzip itself under a cap of
    ``max_body_bytes``, and bounds a whole response at ``total_timeout`` plus one ``read_timeout``, since a
    blocking call cannot otherwise be cancelled. Proxies come from the environment, read at construction.
    """

    def __init__(
        self,
        *,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        total_timeout: float = 120.0,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._timeout = urllib3.Timeout(connect=connect_timeout, read=read_timeout)
        self._total_timeout = total_timeout
        self._max_body_bytes = max_body_bytes
        self._ssl_context = ssl_context if ssl_context is not None else default_ssl_context()
        self._proxies = Proxies()
        # maxsize=8: otherwise the pool keeps one connection per host and discards the rest.
        self._direct = urllib3.PoolManager(maxsize=8, retries=RETRY, ssl_context=self._ssl_context)
        self._via: dict[str, urllib3.ProxyManager] = {}
        self._lock = threading.Lock()

    def _pool_for(self, url: str) -> urllib3.PoolManager:
        proxy = self._proxies.for_url(url)
        if proxy is None:
            return self._direct
        with self._lock:
            manager = self._via.get(proxy.raw)
            if manager is None:
                manager = self._via[proxy.raw] = self._proxy_manager(proxy)
            return manager

    def _proxy_manager(self, proxy: Proxy) -> urllib3.ProxyManager:
        # urllib3 does not read credentials from the proxy URL.
        headers = urllib3.make_headers(proxy_basic_auth=proxy.basic_auth) if proxy.basic_auth is not None else None
        return urllib3.ProxyManager(
            proxy.url, proxy_headers=headers, maxsize=8, retries=RETRY, ssl_context=self._ssl_context
        )

    def get(self, url: str) -> HttpResponse:
        deadline = time.monotonic() + self._total_timeout
        for _ in range(MAX_REDIRECTS + 1):
            # Headers passed here replace the pool's own entirely, so all of HEADERS goes every time.
            r = self._pool_for(url).request(
                "GET",
                url,
                headers=HEADERS,
                redirect=False,
                retries=RETRY,
                preload_content=False,
                decode_content=False,
                timeout=self._timeout,
            )
            try:
                target = next_hop(url, r.status, r.headers.get("Location"))
                if target is None:
                    return HttpResponse(r.status, self._read(r, deadline))
            finally:
                r.close()  # a redirect's body is never read, and a half-read connection never pooled
                r.release_conn()
            url = target
        raise RedirectError(f"more than {MAX_REDIRECTS} redirects")

    def _read(self, r: urllib3.BaseHTTPResponse, deadline: float) -> bytes:
        try:
            body = CappedBody(r.headers.get("Content-Encoding"), self._max_body_bytes)
            # read1, not stream: stream(amt) waits for amt bytes, so a trickle would reach the deadline check
            # only at its end.
            while chunk := r.read1(64 * 1024, decode_content=False):
                body.feed(chunk)
                if time.monotonic() > deadline:
                    raise BodyError("response took longer than total_timeout")
            return body.finish()
        except BodyError:
            raise
        except (urllib3.exceptions.HTTPError, OSError) as e:  # a short or cut body; a stall past the read timeout
            raise BodyError(f"bad body: {e!r}") from e

    def close(self) -> None:
        self._direct.clear()
        with self._lock:
            for manager in self._via.values():
                manager.clear()
            self._via.clear()
