"""A loopback HTTP/1.1 server on raw sockets, for the transport tests.

Raw sockets so that a test can record the request bytes exactly, trickle a body, stall, cut one short,
and send any Content-Encoding. Each test installs a handler, ``handler(request, writer)``.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field


@dataclass
class Request:
    raw: bytes
    method: str
    target: str
    headers: list[tuple[str, str]]
    connection: int  # which accepted connection carried it, in order

    def header(self, name: str) -> str | None:
        for k, v in self.headers:
            if k.lower() == name.lower():
                return v
        return None

    @property
    def header_names(self) -> list[str]:
        return sorted(k.lower() for k, _ in self.headers)


class PeerClosed(Exception):
    pass


@dataclass
class Writer:
    sock: socket.socket
    sent: int = 0
    keep_alive: bool = True

    def send(self, data: bytes) -> None:
        try:
            self.sock.sendall(data)
        except OSError as e:
            raise PeerClosed from e
        self.sent += len(data)

    def head(self, status: int, headers: Iterable[tuple[str, str]] = (), reason: str = "X") -> None:
        lines = [f"HTTP/1.1 {status} {reason}"] + [f"{k}: {v}" for k, v in headers]
        self.send(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))

    def respond(self, status: int, body: bytes = b"", headers: Iterable[tuple[str, str]] = ()) -> None:
        self.head(status, [*headers, ("Content-Length", str(len(body)))])
        self.send(body)

    def chunk(self, data: bytes) -> None:
        self.send(f"{len(data):x}\r\n".encode() + data + b"\r\n")

    def end_chunks(self) -> None:
        self.send(b"0\r\n\r\n")

    def close(self) -> None:
        """End the connection now, whatever was promised."""
        self.keep_alive = False


Handler = Callable[[Request, Writer], None]


def ok(_request: Request, w: Writer) -> None:
    w.respond(200, b"ok")


@dataclass
class Loopback:
    handler: Handler = ok
    requests: list[Request] = field(default_factory=list)
    closed: list[float] = field(default_factory=list)  # monotonic times at which a peer closed mid-response
    connections: int = 0
    sent: int = 0  # body and header bytes written, over every response

    def __post_init__(self) -> None:
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(64)
        self.port = self._sock.getsockname()[1]
        self._lock = threading.Lock()
        self._stopping = False
        self._threads: list[threading.Thread] = []
        self._open: list[socket.socket] = []
        t = threading.Thread(target=self._accept, daemon=True)
        t.start()
        self._threads.append(t)

    def url(self, path: str = "/", host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}{path}"

    def _accept(self) -> None:
        while not self._stopping:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with self._lock:
                self.connections += 1
                number = self.connections
                self._open.append(conn)
            t = threading.Thread(target=self._serve, args=(conn, number), daemon=True)
            t.start()

    def _serve(self, conn: socket.socket, number: int) -> None:
        buf = b""
        try:
            while not self._stopping:
                while b"\r\n\r\n" not in buf:
                    data = conn.recv(65536)
                    if not data:
                        return
                    buf += data
                raw, buf = buf.split(b"\r\n\r\n", 1)
                lines = raw.decode("latin-1").split("\r\n")
                method, target, _ = lines[0].split(" ", 2)
                headers = [(k, v.strip()) for k, _, v in (line.partition(":") for line in lines[1:])]
                request = Request(raw + b"\r\n\r\n", method, target, headers, number)
                with self._lock:
                    self.requests.append(request)
                w = Writer(conn)
                try:
                    self.handler(request, w)
                except PeerClosed:
                    with self._lock:
                        self.closed.append(time.monotonic())
                    return
                finally:
                    with self._lock:
                        self.sent += w.sent
                if not w.keep_alive:
                    return
        except OSError:
            return
        finally:
            conn.close()

    def stop(self) -> None:
        self._stopping = True
        self._sock.close()
        with self._lock:
            for c in self._open:
                with contextlib.suppress(OSError):
                    c.shutdown(socket.SHUT_RDWR)
                c.close()

    def __enter__(self) -> Loopback:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
