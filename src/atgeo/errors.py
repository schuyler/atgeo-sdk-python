"""The client's own exceptions."""

from __future__ import annotations


class AtgeoError(Exception):
    """A non-2xx answer from the server (contract §1).

    ``code`` is the server's error name, or ``"HttpError"`` when the body names none; ``status``
    is the HTTP status, which tells a refused request (4xx) from a server failure (5xx).
    """

    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(code, message, status)  # all three in args, so that the error pickles
        self.code = code
        self.message = message
        self.status = status

    def __str__(self) -> str:
        return f"{self.message} ({self.code})"


class AtgeoDecodingError(Exception):
    """A 2xx answer the client could not use: not JSON, or without what the call needs (contract §1).

    ``__cause__`` is the parser's exception when there was one. Not a ``ValueError``, so that an
    ``except ValueError`` written for a bad region does not swallow a garbled answer.
    """
