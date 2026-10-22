"""The transport seam, and the building blocks for writing a transport of your own.

A transport GETs a URL and returns the status and the decoded body for every status; it raises only
when no response arrived whole. The client turns a non-2xx into AtgeoError itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ._body import BodyError, CappedBody
from ._urls import MAX_REDIRECTS, RedirectError, next_hop, prepare_url

__all__ = [
    "HEADERS",
    "MAX_REDIRECTS",
    "AsyncTransport",
    "BodyError",
    "CappedBody",
    "HttpResponse",
    "RedirectError",
    "Transport",
    "next_hop",
    "prepare_url",
]

# Every header a default transport sets, on every request (contract §12, item 5): a fixed User-Agent with
# no version, in place of one naming the library's and the interpreter's, and no Accept-Language.
HEADERS = {"Accept": "application/json", "Accept-Encoding": "gzip", "User-Agent": "atgeo-client-python"}


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes  # decoded: no Content-Encoding left on it


class Transport(Protocol):
    def get(self, url: str) -> HttpResponse:
        """GET the URL as given, following redirects with next_hop(). Return a response for every other
        status; raise only when no response arrived whole. Must be safe to call from several threads at
        once. Must not cache, keep cookies or send credentials by default."""
        ...


class AsyncTransport(Protocol):
    async def get(self, url: str) -> HttpResponse:
        """The same, as a coroutine. Must respond to task cancellation."""
        ...
