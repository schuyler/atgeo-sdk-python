"""The client's in-memory caches (contract §10). None is thread-safe on its own: the engine's lock guards them."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from typing import Generic, TypeVar

from ._text import utf16
from ._wire import TilePayload, parse_body, read_tile_payload

TTL_SECONDS = 3600.0

_V = TypeVar("_V")


class TileEntry:
    """A tile as served, with the keys of its usable records for the get_place scan.

    Identity matters: ParsedTiles trusts its parsed records only while the cache holds the same entry.
    """

    __slots__ = ("body", "keys")

    def __init__(self, body: bytes, keys: frozenset[str]) -> None:
        self.body = body
        self.keys = keys

    @property
    def size(self) -> int:
        return len(self.body)

    def payload(self) -> TilePayload:
        """The tile's records and attribution, parsed again from the body (contract §10)."""
        # The body parsed once already, when it was stored, so this cannot fail on its contents.
        return read_tile_payload(parse_body(self.body))


class TileCache:
    """Tiles by URL, least recently used out first, capped at ``budget`` bytes of body."""

    def __init__(self, budget: int) -> None:
        self._budget = budget
        self._tiles: OrderedDict[str, TileEntry] = OrderedDict()
        self._total = 0

    def __len__(self) -> int:
        return len(self._tiles)

    @property
    def total(self) -> int:
        return self._total

    def get(self, url: str) -> TileEntry | None:
        """Reading a tile marks it most recently used."""
        entry = self._tiles.get(url)
        if entry is not None:
            self._tiles.move_to_end(url)
        return entry

    def set(self, url: str, entry: TileEntry) -> None:
        old = self._tiles.pop(url, None)
        if old is not None:
            self._total -= old.size
        self._tiles[url] = entry
        self._total += entry.size
        # One large tile may evict several, itself last: the cache never ends an insert over budget.
        while self._total > self._budget and self._tiles:
            _, victim = self._tiles.popitem(last=False)
            self._total -= victim.size

    def urls(self) -> list[str]:
        """Least recently used first, without touching recency."""
        return list(self._tiles)

    def find(self, key: str) -> tuple[str, TileEntry] | None:
        """The cached tile with the lowest URL (by UTF-16) holding a record with this key. Touches no recency."""
        best: tuple[str, TileEntry] | None = None
        for url, entry in self._tiles.items():  # iterating does not reorder an OrderedDict
            if key in entry.keys and (best is None or utf16(url) < utf16(best[0])):
                best = (url, entry)
        return best


class ParsedTiles:
    """The parsed records and attribution of the latest search's tiles, kept outside the budget so that searching
    the same region again with new text parses nothing. Used only while the cache holds the same entry object, so
    what is fetched, and when, and the recency order are exactly as without it."""

    def __init__(self) -> None:
        self._tiles: dict[str, tuple[TileEntry, TilePayload]] = {}

    def get(self, url: str, entry: TileEntry) -> TilePayload | None:
        memo = self._tiles.get(url)
        return memo[1] if memo is not None and memo[0] is entry else None

    def replace(self, tiles: dict[str, tuple[TileEntry, TilePayload]]) -> None:
        self._tiles = tiles


class TtlCache(Generic[_V]):
    """Entries fresh while ``clock() < set_at + ttl``, by wall-clock time."""

    def __init__(self, clock: Callable[[], float], ttl: float = TTL_SECONDS) -> None:
        self._clock = clock
        self._ttl = ttl
        self._entries: dict[str, tuple[_V, float]] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: str) -> _V | None:
        entry = self._entries.get(key)
        if entry is None or not self._clock() < entry[1] + self._ttl:
            return None
        return entry[0]

    def set(self, key: str, value: _V) -> None:
        now = self._clock()
        # Prune (a freedom, contract §14): every snapped box is a key, and a panning map makes many.
        self._entries = {k: e for k, e in self._entries.items() if now < e[1] + self._ttl}
        self._entries[key] = (value, now)
