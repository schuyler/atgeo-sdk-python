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
    """Trim, drop tab, LF and CR; percent-encode space, other controls and non-ASCII; leave %XX alone.

    What WHATWG URL parsing (the reference's) does to those characters; a clean URL is unchanged.
    """
    # WHATWG trims leading and trailing C0 controls and spaces, then drops tab and newlines anywhere.
    url = url.strip("".join(map(chr, range(0x21)))).replace("\t", "").replace("\n", "").replace("\r", "")
    return "".join(chr(b) if b in _KEEP else f"%{b:02X}" for b in url.encode("utf-8", "surrogatepass"))


def is_http_url(url: str) -> bool:
    """An absolute http or https URL with a host and no credentials.

    Credentials are refused, as the Fetch standard's Request does in the reference, so that no transport
    sends any: urllib3 drops them and aiohttp would send them as Basic auth.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        return (
            parts.scheme in ("http", "https")
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and (parts.port is None or parts.port >= 0)
        )
    except ValueError:  # an invalid port or IPv6 literal
        return False


def next_hop(url: str, status: int, location: str | None) -> str | None:
    """The URL a redirect sends a GET to, or None if the response is not a redirect to follow."""
    if status not in _REDIRECTS or location is None:
        return None
    target = urllib.parse.urljoin(url, prepare_url(location))
    if not is_http_url(target):
        raise RedirectError(f"redirect to {_safe(target)!r} is not an http or https URL without credentials")
    return target


def _safe(url: str) -> str:
    """The URL with any credentials removed, for messages."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    authority, slash, path = rest.partition("/")
    return f"{scheme}://{authority.rpartition('@')[2]}{slash}{path}"
