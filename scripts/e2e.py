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
import re
import sys

from atgeo import AsyncAtgeoClient, AtgeoClient, Bbox, Circle, CollectionRef, NearbySearchResult, SortMode

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


def check_nearby(nearby: NearbySearchResult) -> None:
    check(nearby.complete, f"{nearby.tiles.failed} tile(s) failed")
    check(nearby.places, "no places in region")
    check(nearby.attribution, "no attribution for the tiles used")
    check(all(h.collection is not None for h in nearby.places), "a hit names no collection")
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
        log(
            "sync",
            "describeService",
            f"{service.did}, maxTiles {service.max_tiles}, {len(service.tilesets)} tileset(s)",
        )

        tileset = client.describe_tileset(served.slug)
        check(
            any(wanted(CollectionRef(dataset=c.dataset, collection=c.collection)) for c in tileset.collections),
            "describeTileset does not list the dataset and collection",
        )
        log("sync", "describeTileset", f"{tileset.slug}, {len(tileset.collections)} collection(s)")

        nearby = client.search_nearby(REGION, dataset=DATASET, collection=COLLECTION, sort=SortMode.DISTANCE)
        check_nearby(nearby)
        log("sync", "searchNearby (no text)", f"{len(nearby.places)} places from {nearby.tiles.fetched} tile(s)")

        named = next((h for h in nearby.places if h.origin and h.name), None)
        check(named, "no named place in region")
        assert named is not None
        assert named.name is not None
        assert named.origin is not None
        matched = client.search_nearby(REGION, dataset=DATASET, collection=COLLECTION, text=named.name)
        check(any(h.origin == named.origin and h.score is not None for h in matched.places), "named place missing")
        check((matched.places[0].score or 0) > 0, "top text hit has no score")
        log("sync", "searchNearby (text)", f"{named.name!r} -> top hit {matched.places[0].name}")

        point = named.location.point
        check(point, "named place has no point")
        assert point is not None
        cell = Bbox(point.lon, point.lat, point.lon, point.lat)
        keyword = client.search_by_keyword(named.name, dataset=DATASET, collection=COLLECTION, region=cell)
        check(keyword.places, "keyword search found nothing in the named place's degree")
        check(all(h.via == "keyword" for h in keyword.places), "keyword hits are not marked via keyword")
        check(keyword.disclosed.keyword and keyword.disclosed.region == 1, "keyword search disclosed something else")
        log("sync", "search_by_keyword", f"{named.name!r} in a 1 degree cell -> {len(keyword.places)} places")

        solid = client.search_nearby_then_by_keyword(
            REGION, dataset=DATASET, collection=COLLECTION, keyword=named.name, keyword_region="degree"
        )
        check(any(h.origin == named.origin for h in solid.places), "named place missing from the combined search")
        check(not solid.disclosed.keyword, "the keyword was sent though a nearby hit matched")
        log("sync", "search_nearby_then_by_keyword (nearby hit)", f"{len(solid.places)} places, keyword not sent")

        fallback = client.search_nearby_then_by_keyword(
            REGION, dataset=DATASET, collection=COLLECTION, keyword="zzzzqqqq", keyword_region="none"
        )
        check(fallback.disclosed.keyword, "the fallback did not send its keyword")
        log("sync", "search_nearby_then_by_keyword (fallback)", f"{len(fallback.places)} places, keyword sent")

    with AtgeoClient(SERVER) as fresh:
        record = fresh.get_place(named.origin)
        check(record.name == named.name, "getPlace returned another place")
        check(record.attribution, "getPlace carries no attribution")
        log("sync", "getPlace", f"{named.origin} -> {record.name}")


async def run_async() -> None:
    async with AsyncAtgeoClient(SERVER) as client:
        service = await client.describe_service()
        log("async", "describeService", f"{service.did}, {len(service.tilesets)} tileset(s)")
        nearby = await client.search_nearby(REGION, dataset=DATASET, collection=COLLECTION, sort=SortMode.DISTANCE)
        check_nearby(nearby)
        log("async", "searchNearby (no text)", f"{len(nearby.places)} places from {nearby.tiles.fetched} tile(s)")
        named = next((h for h in nearby.places if h.origin and h.name), None)
        check(named, "no named place in region")
        assert named is not None
        assert named.origin is not None
        record = await client.get_place(named.origin)
        check(record.name == named.name, "getPlace returned another place")
        log("async", "getPlace (from cache)", f"{named.origin} -> {record.name}")

        # The async client's autocompleter: a one-character prefix is skipped, a longer one is sent as typed.
        typing = client.autocomplete_by_keyword(dataset=DATASET, collection=COLLECTION)
        assert named.name is not None
        skipped = await typing.search_by_keyword(named.name[0])
        check(not skipped.disclosed.keyword and skipped.disclosed.region is None, "a one-character prefix was sent")
        word = next((w for w in re.split(r"[\W_]+", named.name) if len(w) >= 2), None)
        check(word, "the named place has no word of two characters")
        assert word is not None
        completed = await typing.search_by_keyword(word[:3])
        check(completed.disclosed.keyword and completed.disclosed.region is None, "the prefix was not sent")
        check(completed.places, "keyword search with no region found nothing")
        check(all(h.via == "keyword" for h in completed.places), "keyword hits are not marked via keyword")
        log(
            "async", "autocomplete_by_keyword", f"{word[:3]!r} -> {len(completed.places)} places, one character skipped"
        )


if __name__ == "__main__":
    run_sync()
    asyncio.run(run_async())
