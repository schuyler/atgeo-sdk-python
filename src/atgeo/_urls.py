"""URL checks and preparation, done once above the transports so that every transport sends the same bytes,
and the redirect rule every transport follows."""

from __future__ import annotations

import urllib.parse

MAX_REDIRECTS = 10

_KEEP = frozenset(range(0x21, 0x7F))  # printable ASCII but space, % included
_REDIRECTS = frozenset({301, 302, 303, 307, 308})


class RedirectError(OSError):
    """A redirect to anything but an http or https URL, or more than MAX_REDIRECTS of them."""


def prepare_url(url: str) -> str:
    """Drop tab, LF and CR; percent-encode space, other controls and non-ASCII; leave %XX alone.

    What WHATWG URL parsing (the reference's) does to those characters; a clean URL is unchanged.
    """
    url = url.replace("\t", "").replace("\n", "").replace("\r", "")
    return "".join(chr(b) if b in _KEEP else f"%{b:02X}" for b in url.encode("utf-8", "surrogatepass"))


def is_http_url(url: str) -> bool:
    """An absolute http or https URL with a host."""
    try:
        parts = urllib.parse.urlsplit(url)
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:  # an invalid port or IPv6 literal
        return False


def next_hop(url: str, status: int, location: str | None) -> str | None:
    """The URL a redirect sends a GET to, or None if the response is not a redirect to follow."""
    if status not in _REDIRECTS or location is None:
        return None
    target = urllib.parse.urljoin(url, prepare_url(location))
    if not is_http_url(target):
        raise RedirectError(f"redirect to {target!r} is not an http or https URL")
    return target
