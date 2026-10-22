"""End-to-end run of both clients against a live garganorn. Not part of pytest.

    ATGEO_SERVER=http://localhost:8000 uv run python scripts/e2e.py

Configured by the variables scripts/e2e.ts in atgeo-sdk-js reads: ATGEO_SERVER (default
http://localhost:8000), ATGEO_DATASET (default openstreetmap.org), ATGEO_COLLECTION (default none) and
ATGEO_REGION ("lat,lon,radius" in metres; default Manchester, NH, inside the local OSM New Hampshire
tileset).
"""

from __future__ import annotations

import asyncio
import os
import sys

from atgeo import AsyncAtgeoClient, AtgeoClient, Circle, CollectionRef, SearchResult, SortMode

SERVER = os.environ.get("ATGEO_SERVER", "http://localhost:8000")
DATASET = os.environ.get("ATGEO_DATASET", "openstreetmap.org")
COLLECTION = os.environ.get("ATGEO_COLLECTION") or None
LAT, LON, RADIUS = (float(x) for x in os.environ.get("ATGEO_REGION", "43.0,-71.45,3000").split(","))
REGION = Circle(LAT, LON, RADIUS)


def wanted(c: CollectionRef) -> bool:
    # With a collection set, the tileset must serve that collection, not just the dataset.
    return c.dataset == DATASET and (COLLECTION is None or c.collection == COLLECTION)


def log(client: str, step: str, detail: str) -> None:
    print(f"ok  {client} {step}: {detail}")


def check(cond: object, message: str) -> None:
    if not cond:
        sys.exit(f"FAIL {message}")


def check_nearby(nearby: SearchResult) -> None:
    check(nearby.complete, f"{nearby.tiles.failed} tile(s) failed")
    check(nearby.places, "no places in region")
    distances = [h.distance for h in nearby.places]
    check(
        all(d <= RADIUS for d in distances) and distances == sorted(distances),
        "not distance-ordered within radius",
    )


def run_sync() -> None:
    with AtgeoClient(SERVER) as client:
        service = client.describe_service()
        served = next((t for t in service.tilesets if any(wanted(c) for c in t.collections)), None)
        check(served and served.slug, f"{DATASET}{f'/{COLLECTION}' if COLLECTION else ''} not served")
        assert served is not None
        assert served.slug is not None
        log("sync", "describeService", f"{service.did}, {len(service.tilesets)} tileset(s)")

        tileset = client.describe_tileset(served.slug)
        check(
            any(wanted(CollectionRef(dataset=c.dataset, collection=c.collection)) for c in tileset.collections),
            "describeTileset does not list the dataset and collection",
        )
        log("sync", "describeTileset", f"{tileset.slug}, {len(tileset.collections)} collection(s)")

        nearby = client.search_places(REGION, dataset=DATASET, collection=COLLECTION, sort=SortMode.DISTANCE)
        check_nearby(nearby)
        log("sync", "searchPlaces (no text)", f"{len(nearby.places)} places from {nearby.tiles.fetched} tile(s)")

        named = next((h for h in nearby.places if h.origin and h.name), None)
        check(named, "no named place in region")
        assert named is not None
        assert named.name is not None
        assert named.origin is not None
        matched = client.search_places(REGION, dataset=DATASET, collection=COLLECTION, text=named.name)
        check(any(h.origin == named.origin and h.score is not None for h in matched.places), "named place missing")
        check((matched.places[0].score or 0) > 0, "top text hit has no score")
        log("sync", "searchPlaces (text)", f"{named.name!r} -> top hit {matched.places[0].name}")

    with AtgeoClient(SERVER) as fresh:
        record = fresh.get_place(named.origin)
        check(record.name == named.name, "getPlace returned another place")
        log("sync", "getPlace", f"{named.origin} -> {record.name}")


async def run_async() -> None:
    async with AsyncAtgeoClient(SERVER) as client:
        service = await client.describe_service()
        log("async", "describeService", f"{service.did}, {len(service.tilesets)} tileset(s)")
        nearby = await client.search_places(REGION, dataset=DATASET, collection=COLLECTION, sort=SortMode.DISTANCE)
        check_nearby(nearby)
        log("async", "searchPlaces (no text)", f"{len(nearby.places)} places from {nearby.tiles.fetched} tile(s)")
        named = next((h for h in nearby.places if h.origin and h.name), None)
        check(named, "no named place in region")
        assert named is not None
        assert named.origin is not None
        record = await client.get_place(named.origin)
        check(record.name == named.name, "getPlace returned another place")
        log("async", "getPlace (from cache)", f"{named.origin} -> {record.name}")


if __name__ == "__main__":
    run_sync()
    asyncio.run(run_async())
