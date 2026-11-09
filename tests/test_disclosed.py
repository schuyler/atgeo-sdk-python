"""disclosed.test.ts, ported: what each result reports the server learned during that call (contract §4.11), and
section 12's item 9 (it agrees with the captured requests)."""

from __future__ import annotations

from typing import Any

from atgeo import Bbox, Disclosure

from .conftest import Client, Make
from .fake_server import FakeServer, FakeTile, overture
from .helpers import OVERTURE, place, point, record

TILES = [FakeTile("https://tiles.example/p.json", [record(place("zc", "Zanzibar Cafe", [point(37.772, -122.412)]), 5)])]
REGION = Bbox(-122.42, 37.77, -122.41, 37.78)


def setup(make: Make, **kwargs: Any) -> Client:
    return make(FakeServer(tilesets=[overture(TILES)], **kwargs))


class TestDisclosed:
    def test_is_the_0_01_grid_no_keyword_on_a_cold_nearby_search_whose_hits_come_from_tiles(self, make: Make) -> None:
        result = setup(make).search_nearby(REGION, text="Zanzibar", **OVERTURE)
        assert result.disclosed == Disclosure(keyword=False, region=0.01)
        assert result.places
        assert all(h.via == "tiles" for h in result.places)

    def test_reports_no_region_for_a_nearby_search_answered_from_the_cache(self, make: Make) -> None:
        client = setup(make)
        client.search_nearby(REGION, **OVERTURE)
        warm = client.search_nearby(REGION, text="Zanzibar", **OVERTURE)
        assert warm.disclosed == Disclosure(keyword=False, region=None)

    def test_reports_the_keyword_and_a_1_degree_region_for_a_keyword_search_with_a_region(self, make: Make) -> None:
        result = setup(make).search_by_keyword("Zanzibar", **OVERTURE, region=REGION)
        assert result.disclosed == Disclosure(keyword=True, region=1)

    def test_reports_the_keyword_and_no_region_for_a_keyword_search_without_one(self, make: Make) -> None:
        result = setup(make).search_by_keyword("Zanzibar", **OVERTURE)
        assert result.disclosed == Disclosure(keyword=True, region=None)


class TestRegionCounting:
    """What counts as sent for the region (contract §4.11)."""

    def test_counts_a_get_tiles_request_as_sent_even_when_it_lists_no_tiles(self, make: Make) -> None:
        client = setup(make, tiles_for=lambda _bbox: [])
        result = client.search_nearby(REGION, **OVERTURE)
        assert len(client.fake.requests) == 1
        assert result.disclosed == Disclosure(keyword=False, region=0.01)

    def test_does_not_count_a_tile_the_client_refused_to_fetch(self, make: Make) -> None:
        # The tile list is cached by the first search, so the second sends nothing: its only tile is refused.
        client = setup(make, tiles_for=lambda _bbox: ["ftp://tiles.example/p.json"])
        client.search_nearby(REGION, **OVERTURE)
        before = len(client.fake.requests)
        warm = client.search_nearby(REGION, **OVERTURE)
        assert len(client.fake.requests) == before
        assert [f.kind.value for f in warm.tile_failures] == ["refused"]
        assert warm.disclosed == Disclosure(keyword=False, region=None)

    def test_counts_a_tile_fetch_that_fails(self, make: Make) -> None:
        fake = FakeServer(tilesets=[overture(TILES)])
        client = make(fake, cache_budget_bytes=0)
        client.search_nearby(REGION, **OVERTURE)  # caches the list; the tile is evicted as it is stored
        fake.fail_tiles.add(TILES[0].url)
        warm = client.search_nearby(REGION, **OVERTURE)
        assert [f.kind.value for f in warm.tile_failures] == ["status"]
        assert warm.disclosed == Disclosure(keyword=False, region=0.01)

    def test_counts_a_tile_fetch_for_a_cached_list_whose_tiles_were_evicted(self, make: Make) -> None:
        fake = FakeServer(tilesets=[overture(TILES)])
        client = make(fake, cache_budget_bytes=0)  # every tile is evicted as it is stored
        client.search_nearby(REGION, **OVERTURE)
        warm = client.search_nearby(REGION, **OVERTURE)
        assert warm.disclosed == Disclosure(keyword=False, region=0.01)  # the list was cached; the tile was fetched

    def test_counts_a_transport_failure_of_a_tile(self, make: Make) -> None:
        fake = FakeServer(tilesets=[overture(TILES)], network_errors=[TILES[0].url])
        result = make(fake).search_nearby(REGION, **OVERTURE)
        assert [f.kind.value for f in result.tile_failures] == ["transport"]
        assert result.disclosed == Disclosure(keyword=False, region=0.01)

    def test_a_warm_antimeridian_search_sends_nothing_and_a_half_cold_one_sends(self, make: Make) -> None:
        client = setup(make)
        east = Bbox(170, 10, 175, 20)
        client.search_nearby(east, **OVERTURE)
        crossing = Bbox(170, 10, -170, 20)
        both = client.search_nearby(crossing, **OVERTURE)  # the west half's list is new
        assert both.disclosed.region == 0.01
        assert client.search_nearby(crossing, **OVERTURE).disclosed.region is None
