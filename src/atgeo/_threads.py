"""Threads the clients start, each with an 8 MiB stack.

On CPython 3.12 and 3.13, json.loads recurses in C and can run out of a small thread stack before Python's
recursion limit catches it: with a 1 MiB stack (the official Alpine images' default), 8,000 levels of
nested arrays crash the interpreter. threading.stack_size() is process-wide, so it is set under a lock
around each start and then restored.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

STACK_SIZE = 8 * 1024 * 1024
_lock = threading.Lock()


def start_daemon(target: Callable[[], object], name: str) -> threading.Thread:
    thread = threading.Thread(target=target, name=name, daemon=True)
    with _lock:
        try:
            previous = threading.stack_size(STACK_SIZE)
        except (ValueError, RuntimeError):  # a platform that cannot set it: take its default
            thread.start()
            return thread
        try:
            thread.start()
        finally:
            threading.stack_size(previous)
    return thread
