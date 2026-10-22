"""A Transport over requests, for an app that already holds a configured ``requests.Session``.

Copy it into your app; it is not part of the atgeo package. It does what the default transports do:
no redirect following of its own (``next_hop`` decides, and a redirect's body is never read), raw bytes
through ``CappedBody``, no retries, no cookies, no ``.netrc`` and none of the session's own auth or
headers (proxies are chosen here), and a deadline on the redirects and the body (not on headers trickled
a byte at a time, which only the default transport's watchdog bounds). It sends through the session's
adapter rather than ``Session.send``, which reads a redirect's whole body even with
``allow_redirects=False``.

    client = AtgeoClient(transport=RequestsTransport(session=my_session))
"""

from __future__ import annotations

import time
import urllib.parse
import urllib.request

import requests
import urllib3.exceptions

from atgeo.transport import HEADERS, MAX_REDIRECTS, BodyError, CappedBody, HttpResponse, RedirectError, next_hop


class RequestsTransport:
    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        total_timeout: float = 120.0,
        max_body_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        # The session lends its adapters (TLS settings, mounts, pools) and nothing else: requests are prepared
        # without it, so its auth, headers, cookies and params never go to the hosts the server names, and it
        # is never changed, so the rest of the app keeps its own settings.
        self._owned = session is None
        self._session = session or requests.Session()
        self._timeout = (connect_timeout, read_timeout)
        self._total_timeout = total_timeout
        self._max_body_bytes = max_body_bytes
        self._proxies = urllib.request.getproxies()

    def _proxy_for(self, url: str) -> dict[str, str]:
        parts = urllib.parse.urlsplit(url)
        proxy = self._proxies.get(parts.scheme) or self._proxies.get("all")
        host = parts.netloc.rsplit("@", 1)[-1]
        if not proxy or urllib.request.proxy_bypass(host):
            return {}
        return {parts.scheme: proxy}

    def get(self, url: str) -> HttpResponse:
        deadline = time.monotonic() + self._total_timeout
        for _ in range(MAX_REDIRECTS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("response took longer than total_timeout")
            # Through the session's adapter, not Session.send: even with allow_redirects=False, send() reads a
            # redirect's whole body into memory to work out the next request.
            prepared = requests.Request("GET", url, headers=HEADERS).prepare()
            r = self._session.get_adapter(url).send(
                prepared,
                stream=True,
                timeout=(min(self._timeout[0], remaining), min(self._timeout[1], remaining)),
                verify=self._session.verify,
                cert=self._session.cert,
                proxies=self._proxy_for(url),
            )
            try:
                target = next_hop(url, r.status_code, r.headers.get("Location"))
                if target is None:
                    return HttpResponse(r.status_code, self._read(r, deadline))
            finally:
                r.close()
            url = target
        raise RedirectError(f"more than {MAX_REDIRECTS} redirects")

    def _read(self, r: requests.Response, deadline: float) -> bytes:
        try:
            body = CappedBody(r.headers.get("Content-Encoding"), self._max_body_bytes)
            while chunk := r.raw.read1(64 * 1024, decode_content=False):  # what has arrived, undecoded
                body.feed(chunk)
                if time.monotonic() > deadline:
                    raise BodyError("response took longer than total_timeout")
            return body.finish()
        except BodyError:
            raise
        except (urllib3.exceptions.HTTPError, OSError) as e:
            raise BodyError(f"bad body: {e!r}") from e

    def close(self) -> None:
        if self._owned:
            self._session.close()
