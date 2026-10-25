"""Shared test helpers: fixture loading and place builders, as test/helpers/fakeServer.ts has them."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, Final

import pytest

from atgeo import Bbox, Circle, CollectionAttribution, CollectionRef, Region

FIXTURES = Path(__file__).parent / "fixtures"

# The dataset and collection a search names; search_places has no defaults (contract §4.1).
OVERTURE: Final = {"dataset": "overturemaps.org", "collection": "place"}


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


def tile_url(n: str) -> str:
    return f"https://tiles.example/{n}.json"


def xrpc(nsid: str, params: list[tuple[str, str]], server: str = "https://places.atgeo.org") -> str:
    """The URL the clients send for an XRPC call, for the fake's raw-response override."""
    import urllib.parse  # noqa: PLC0415

    return f"{server}/xrpc/{nsid}?{urllib.parse.urlencode(params)}"


def attribution(entry: Mapping[str, Any]) -> CollectionAttribution:
    """A CollectionAttribution from a wire-shaped entry: a missing key is None, and licenses a tuple."""
    fields: dict[str, Any] = {k: tuple(v) if k == "licenses" else v for k, v in entry.items()}
    return CollectionAttribution(**fields)


def collection_ref(pair: Mapping[str, str] | None) -> CollectionRef | None:
    """A hit's expected collection from its wire shape."""
    return None if pair is None else CollectionRef(dataset=pair["dataset"], collection=pair["collection"])


def region_of(r: Mapping[str, Any]) -> Region:
    """A fixture's region: {"bbox": [west, south, east, north]} or {"lat", "lon", "radius"}."""
    if "bbox" in r:
        return Bbox(*r["bbox"])
    return Circle(r["lat"], r["lon"], r["radius"])


def assert_immutable(entries: Sequence[CollectionAttribution]) -> None:
    """The attribution an app is handed is its own to hold but not to change (contract §9): this port hands out
    immutable values, a tuple of frozen entries whose licenses are a tuple, where the reference copies."""
    assert isinstance(entries, tuple)
    with pytest.raises(TypeError):
        entries[0] = entries[-1]  # type: ignore[index]
    with pytest.raises(AttributeError):
        entries.append(entries[0])  # type: ignore[attr-defined]
    for entry in entries:
        with pytest.raises(FrozenInstanceError):
            entry.release = "changed"
        with pytest.raises(FrozenInstanceError):
            entry.licenses = ("https://l.example/changed",)
        assert entry.licenses is None or isinstance(entry.licenses, tuple)
