"""The proxy each request goes through, the same rule for both default transports.

The proxy variables are read once, at construction; ``no_proxy`` is read again on every request, by
``urllib.request.proxy_bypass``. Neither library is left to do this: urllib3's PoolManager ignores the
environment, and aiohttp's ``trust_env`` would also send credentials from ``~/.netrc``.
"""

from __future__ import annotations

import urllib.parse
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Proxy:
    url: str  # without credentials, so that they never reach an exception message
    username: str | None  # unquoted
    password: str | None

    @property
    def basic_auth(self) -> str | None:
        """ "user:password", for urllib3's Proxy-Authorization."""
        return None if self.username is None else f"{self.username}:{self.password or ''}"


def _parse(raw: str) -> Proxy:
    if "://" not in raw:
        raw = "http://" + raw  # as urllib.request reads a bare host:port
    parts = urllib.parse.urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme.startswith("socks"):
        raise ValueError(f"SOCKS proxies are not supported: {raw.split('@')[-1]!r}")
    if scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"unsupported proxy URL: {raw.split('@')[-1]!r}")
    host = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    username = urllib.parse.unquote(parts.username) if parts.username is not None else None
    password = urllib.parse.unquote(parts.password) if parts.password is not None else None
    return Proxy(f"{scheme}://{netloc}", username, password)


class Proxies:
    def __init__(self) -> None:
        found = urllib.request.getproxies()
        self._by_scheme: dict[str, Proxy] = {}
        for scheme in ("http", "https"):
            raw = found.get(scheme) or found.get("all")
            if raw:
                self._by_scheme[scheme] = _parse(raw)  # a SOCKS proxy fails here, not on every request

    def for_url(self, url: str) -> Proxy | None:
        parts = urllib.parse.urlsplit(url)
        proxy = self._by_scheme.get(parts.scheme)
        if proxy is None or not parts.hostname:
            return None
        host = parts.hostname if parts.port is None else f"{parts.hostname}:{parts.port}"
        if urllib.request.proxy_bypass(host):
            return None
        return proxy
