"""Shared test helpers: fixture loading and place builders, as test/helpers/fakeServer.ts has them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def place_origin(id: str) -> str:
    return f"urn:atgeo:overturemaps.org:place:{id}"


def point(lat: float, lon: float) -> dict[str, Any]:
    return {"$type": "community.lexicon.location.geo", "latitude": f"{lat:.6f}", "longitude": f"{lon:.6f}"}


def bbox_location(north: float, south: float, east: float, west: float) -> dict[str, Any]:
    return {
        "$type": "community.lexicon.location.bbox",
        "north": f"{north:.6f}",
        "south": f"{south:.6f}",
        "east": f"{east:.6f}",
        "west": f"{west:.6f}",
    }


_UNSET: Any = object()


def place(id: str, name: str, locations: list[Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """`extra` may override "origin", or set it to None to make a value with no origin."""
    value: dict[str, Any] = {
        "$type": "org.atgeo.place",
        "origin": place_origin(id),
        "name": name,
        "locations": locations,
    }
    for k, v in (extra or {}).items():
        if v is None and k == "origin":
            del value["origin"]
        else:
            value[k] = v
    return value


def record(value: dict[str, Any], importance: Any = None, uri: str | None = None) -> dict[str, Any]:
    r: dict[str, Any] = {"value": value}
    if importance is not None:
        r["importance"] = importance
    if uri is not None:
        r["uri"] = uri
    return r
