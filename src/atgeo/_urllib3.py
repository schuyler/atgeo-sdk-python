"""Urllib3Transport, the blocking client's default."""

from __future__ import annotations

import contextlib
import socket
import ssl
import threading
import time

import urllib3
import urllib3.connection
import urllib3.exceptions
import urllib3.response

from ._body import BodyError, CappedBody
from ._proxy import Proxies, Proxy
from ._tls import DEFAULT_MAX_BODY_BYTES, default_ssl_context
from ._urls import MAX_REDIRECTS, RedirectError, next_hop
from .transport import HEADERS, HttpResponse

# Send each request once (contract §14 forbids retrying a tile) and leave redirects to the transport.
# Retry(total=0, redirect=10) would follow none at all, because total wins.
RETRY = urllib3.Retry(total=None, connect=0, read=0, status=0, other=0, redirect=0, raise_on_redirect=False)


# The deadline of the request this thread is making, read by the connections' header watchdog.
_deadline = threading.local()


class _Watchdog:
    """Shuts a socket down at a deadline, and remembers that it did."""

    def __init__(self, sock: socket.socket, at: float) -> None:
        self.fired = False
        self._sock = sock
        self._timer = threading.Timer(max(0.0, at - time.monotonic()), self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self) -> None:
        self.fired = True
        with contextlib.suppress(OSError):
            self._sock.shutdown(socket.SHUT_RDWR)

    def cancel(self) -> None:
        self._timer.cancel()


class _Watched(urllib3.connection.HTTPConnection):
    """A connection that gives up on the response headers at the request's deadline.

    http.client reads headers a line at a time under the per-recv read timeout, so a server trickling them
    could otherwise hold a request indefinitely. The watchdog shuts the socket down, which unblocks the read.
    Whatever http.client makes of the cut-off headers (an EOF there reads as their end, so it can even
    return a 200 with no body), a request the watchdog stopped raises TimeoutError.
    """

    def getresponse(self) -> urllib3.response.HTTPResponse:  # type: ignore[override]
        at: float | None = getattr(_deadline, "at", None)
        sock = self.sock
        if at is None or sock is None:
            return super().getresponse()
        watchdog = _Watchdog(sock, at)
        try:
            response = super().getresponse()
        except BaseException as e:
            if watchdog.fired:
                raise TimeoutError("response took longer than total_timeout") from e
            raise
        finally:
            watchdog.cancel()
        if watchdog.fired:
            response.close()
            raise TimeoutError("response took longer than total_timeout")
        return response


class _WatchedHTTPS(_Watched, urllib3.connection.HTTPSConnection):
    pass


class _Pool(urllib3.HTTPConnectionPool):
    ConnectionCls = _Watched


class _HTTPSPool(urllib3.HTTPSConnectionPool):
    ConnectionCls = _WatchedHTTPS


_POOLS = {"http": _Pool, "https": _HTTPSPool}


class Urllib3Transport:
    """A pooled urllib3 transport with no retries, no cookies and no cache.

    It follows redirects itself, never reading a redirect's body, decodes gzip itself under a cap of
    ``max_body_bytes``, and bounds the whole call, redirects and headers included, at ``total_timeout``
    plus one ``read_timeout``, since a blocking call cannot otherwise be cancelled. Proxies come from the
    environment, read at construction.
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
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._total_timeout = total_timeout
        self._max_body_bytes = max_body_bytes
        self._ssl_context = ssl_context if ssl_context is not None else default_ssl_context()
        self._proxies = Proxies()
        # maxsize=8: otherwise the pool keeps one connection per host and discards the rest.
        self._direct = urllib3.PoolManager(maxsize=8, retries=RETRY, ssl_context=self._ssl_context)
        self._direct.pool_classes_by_scheme = _POOLS
        self._via: dict[str, urllib3.ProxyManager] = {}
        self._lock = threading.Lock()

    def _pool_for(self, url: str) -> urllib3.PoolManager:
        proxy = self._proxies.for_url(url)
        if proxy is None:
            return self._direct
        with self._lock:
            key = f"{proxy.url} {proxy.basic_auth}"
            manager = self._via.get(key)
            if manager is None:
                manager = self._via[key] = self._proxy_manager(proxy)
            return manager

    def _proxy_manager(self, proxy: Proxy) -> urllib3.ProxyManager:
        # urllib3 does not read credentials from the proxy URL.
        headers = urllib3.make_headers(proxy_basic_auth=proxy.basic_auth) if proxy.basic_auth is not None else None
        manager = urllib3.ProxyManager(
            proxy.url,
            proxy_headers=headers,
            proxy_ssl_context=self._ssl_context,  # an https proxy is verified like any server
            maxsize=8,
            retries=RETRY,
            ssl_context=self._ssl_context,
        )
        manager.pool_classes_by_scheme = _POOLS
        return manager

    def get(self, url: str) -> HttpResponse:
        # total_timeout bounds the whole call: every hop, its headers (the connections' watchdog) and its body.
        deadline = time.monotonic() + self._total_timeout
        _deadline.at = deadline
        try:
            for _ in range(MAX_REDIRECTS + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("response took longer than total_timeout")
                timeout = urllib3.Timeout(
                    connect=min(self._connect_timeout, remaining), read=min(self._read_timeout, remaining)
                )
                try:
                    # Headers passed here replace the pool's own entirely, so all of HEADERS goes every time.
                    r = self._pool_for(url).request(
                        "GET",
                        url,
                        headers=HEADERS,
                        redirect=False,
                        retries=RETRY,
                        preload_content=False,
                        decode_content=False,
                        timeout=timeout,
                    )
                except urllib3.exceptions.HTTPError as e:
                    if time.monotonic() >= deadline:  # the watchdog's doing, or a clamped timeout
                        raise TimeoutError("response took longer than total_timeout") from e
                    raise
                try:
                    target = next_hop(url, r.status, r.headers.get("Location"))
                    if target is None:
                        return HttpResponse(r.status, self._read(r, deadline))
                finally:
                    r.close()  # a redirect's body is never read, and a half-read connection never pooled
                    r.release_conn()
                url = target
            raise RedirectError(f"more than {MAX_REDIRECTS} redirects")
        finally:
            _deadline.at = None

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
