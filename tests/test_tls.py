"""Both default transports verify certificates against the operating system's trust store. Whether that store
refuses a bad certificate is truststore's and the platform's business; this checks only that the SDK uses it."""

from __future__ import annotations

import asyncio
import ssl
from typing import Any

import aiohttp
import pytest
import truststore
import urllib3

from atgeo import AiohttpTransport, Urllib3Transport
from atgeo._tls import default_ssl_context


def test_the_default_context_verifies_against_the_os_store() -> None:
    context = default_ssl_context()
    assert isinstance(context, truststore.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


@pytest.mark.parametrize("given", [None, ssl.create_default_context()])
def test_urllib3_uses_the_context_directly_and_through_a_proxy(
    given: ssl.SSLContext | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    t = Urllib3Transport(ssl_context=given)
    try:
        direct = t._direct.connection_pool_kw["ssl_context"]
        via = t._pool_for("https://places.atgeo.org/")
        assert isinstance(via, urllib3.ProxyManager)
        assert via.connection_pool_kw["ssl_context"] is direct
        assert via.proxy_ssl_context is direct
        assert direct is given if given is not None else isinstance(direct, truststore.SSLContext)
    finally:
        t.close()


@pytest.mark.parametrize("given", [None, ssl.create_default_context()])
def test_aiohttp_uses_the_context(given: ssl.SSLContext | None, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    real = aiohttp.TCPConnector

    def spy(*args: Any, **kwargs: Any) -> aiohttp.TCPConnector:
        seen.append(kwargs.get("ssl"))
        return real(*args, **kwargs)

    monkeypatch.setattr(aiohttp, "TCPConnector", spy)

    async def run() -> None:
        t = AiohttpTransport(ssl_context=given)
        t._session_for_running_loop()
        await t.aclose()

    asyncio.run(run())
    [context] = seen
    assert context is given if given is not None else isinstance(context, truststore.SSLContext)
