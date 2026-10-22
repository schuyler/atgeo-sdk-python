"""Defaults both default transports share."""

from __future__ import annotations

import ssl

import truststore

# The Kotlin port's cap. The largest tiles places.atgeo.org serves are about 2 MB, and a small gzip body
# whose URL the server chose can decode to gigabytes.
DEFAULT_MAX_BODY_BYTES = 32 * 1024 * 1024


def default_ssl_context() -> ssl.SSLContext:
    """Verification against the operating system's trust store, which python.org's macOS builds otherwise lack."""
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
