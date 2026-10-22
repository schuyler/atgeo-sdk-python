"""JSON decoding and the lenient readers (contract §1, §16)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TileRecord:
    value: Mapping[str, Any]
    uri: str | None = None
    importance: int | None = None


def importance_of(v: object) -> int | None:
    """An integer read through a double, as the contract requires; absent unless a finite number below 2^63."""
    if not (type(v) is int or type(v) is float):  # bool, str, list, None: absent
        return None
    try:
        d = float(v)
    except OverflowError:  # an integer literal past 1.8e308, which JavaScript reads as Infinity
        return None
    if not math.isfinite(d) or abs(d) >= 2.0**63:
        return None
    return int(d)  # truncates toward zero
