from __future__ import annotations

import dataclasses

from atgeo import Bbox

from .conftest import Client, Make
from .fake_server import FakeServer, FakeTile, TilesFor, overture
from .helpers import OVERTURE, place, point, record

EAST = "https://tiles.example/east.json"
WEST = "https://tiles.example/west.json"
REGION = Bbox(-122.42, 37.77, -122.41, 37.78)
TILES = [
    FakeTile(EAST, [record(place("e", "East", [point(37.773, -122.413)]))]),
    FakeTile(WEST, [record(place("w", "West", [point(37.772, -122.412)]))]),
]


def setup(make: Make, tiles_for: TilesFor | None = None, fail_tiles: list[str] | None = None) -> Client:
    return make(FakeServer(fail_tiles=fail_tiles or [], tiles_for=tiles_for, tilesets=[overture(TILES)]))


def tiles_count(requests: list[str]) -> int:
    return sum("org.atgeo.getTiles" in r for r in requests)


class TestOverviewAnswers:
    def test_flags_an_overview_answer_whose_tiles_all_load_as_overview_and_complete(self, make: Make) -> None:
        result = setup(make, lambda _b: {"tiles": [EAST, WEST], "overview": True}).search_places(REGION, **OVERTURE)
        assert result.overview is True
        assert result.complete is True
        assert "summary" not in {f.name for f in dataclasses.fields(result)}

    def test_does_not_flag_a_regular_answer_with_a_failed_tile(self, make: Make) -> None:
        result = setup(make, None, [WEST]).search_places(REGION, **OVERTURE)
        assert result.overview is False
        assert result.complete is False

    def test_reads_an_answer_without_the_key_as_regular(self, make: Make) -> None:
        assert setup(make, lambda _b: {"tiles": [EAST, WEST]}).search_places(REGION, **OVERTURE).overview is False

    def test_keeps_the_flag_when_the_tile_list_cache_answers(self, make: Make) -> None:
        client = setup(make, lambda _b: {"tiles": [EAST, WEST], "overview": True})
        client.search_places(REGION, **OVERTURE)
        second = client.search_places(REGION, **OVERTURE)
        assert tiles_count(client.fake.requests) == 1
        assert second.overview is True

    def test_flags_a_split_antimeridian_search_when_only_one_half_is_overview(self, make: Make) -> None:
        client = setup(
            make, lambda b: {"tiles": [EAST], "overview": True} if b.startswith("170") else {"tiles": [WEST]}
        )
        assert client.search_places(Bbox(170, 10, -170, 20), **OVERTURE).overview is True
