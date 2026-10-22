"""truststore against the platform's certificate store. The only tests that need the network: CI's tls job
runs them with ATGEO_NETWORK_TESTS=1 (pytest -m network); everywhere else they are skipped."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from atgeo import AiohttpTransport, Urllib3Transport
from atgeo.transport import HttpResponse

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(os.environ.get("ATGEO_NETWORK_TESTS") != "1", reason="needs the network: ATGEO_NETWORK_TESTS=1"),
]

SERVICE = "https://places.atgeo.org/xrpc/org.atgeo.describeService?"
BAD = ["https://expired.badssl.com/", "https://self-signed.badssl.com/"]


def fetch(kind: str, url: str) -> HttpResponse:
    if kind == "urllib3":
        t = Urllib3Transport()
        try:
            return t.get(url)
        finally:
            t.close()

    async def run() -> HttpResponse:
        a = AiohttpTransport()
        try:
            return await a.get(url)
        finally:
            await a.aclose()

    return asyncio.run(run())


@pytest.mark.parametrize("kind", ["urllib3", "aiohttp"])
def test_reaches_places_atgeo_org(kind: str) -> None:
    response = fetch(kind, SERVICE)
    assert response.status == 200
    assert "did" in json.loads(response.body)


@pytest.mark.parametrize("url", BAD)
@pytest.mark.parametrize("kind", ["urllib3", "aiohttp"])
def test_refuses_a_bad_certificate(kind: str, url: str) -> None:
    with pytest.raises(Exception):  # noqa: B017, PT011  (each library raises its own TLS error)
        fetch(kind, url)
