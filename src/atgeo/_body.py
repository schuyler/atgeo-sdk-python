"""The gzip-decoding, size-capped body reader both default transports share."""

from __future__ import annotations

import zlib


class BodyError(OSError):
    """A response arrived but its body could not be read whole: past max_body_bytes once decoded,
    cut short, stalled, or not valid gzip."""


class CappedBody:
    """Feed it a response's raw bytes as they arrive; ``finish()`` returns the decoded body.

    Raises :class:`BodyError` as soon as more than ``max_bytes`` has arrived, or decoded, and for a
    ``Content-Encoding`` other than identity and gzip, a truncated gzip stream, or trailing garbage.
    """

    def __init__(self, content_encoding: str | None, max_bytes: int) -> None:
        enc = (content_encoding or "identity").strip().lower()
        if enc not in ("identity", "gzip", "x-gzip"):
            raise BodyError(f"unexpected Content-Encoding {enc!r}")
        self._dec = None if enc == "identity" else zlib.decompressobj(16 + zlib.MAX_WBITS)
        self._max = max_bytes
        self._raw = 0
        self._out = bytearray()

    def feed(self, chunk: bytes) -> None:
        self._raw += len(chunk)
        if self._raw > self._max:
            raise BodyError(f"body exceeds {self._max} bytes")
        if self._dec is None:
            self._out += chunk
            return
        try:
            while chunk:
                if self._dec.eof:  # the next gzip member
                    self._dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
                # max_length is never 0, which would mean unlimited.
                self._out += self._dec.decompress(chunk, self._max + 1 - len(self._out))
                if len(self._out) > self._max:
                    raise BodyError(f"decoded body exceeds {self._max} bytes")
                chunk = self._dec.unused_data if self._dec.eof else self._dec.unconsumed_tail
        except zlib.error as e:
            raise BodyError(f"bad gzip body: {e}") from e

    def finish(self) -> bytes:
        if self._dec is not None and self._raw and not self._dec.eof:
            raise BodyError("truncated gzip body")
        return bytes(self._out)
