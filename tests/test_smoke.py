from __future__ import annotations

from atgeo import Bbox

from .conftest import Make
from .fake_server import FakeServer, FakeTile, overture
from .helpers import place, place_origin, point, record, tile_url


def test_searches_through_the_fake(make: Make) -> None:
    fake = FakeServer(
        tilesets=[overture([FakeTile(tile_url("a"), [record(place("a", "Alpha", [point(37.775, -122.415)]), 3)])])]
    )
    result = make(fake).search_places(Bbox(-122.42, 37.77, -122.41, 37.78), text="alfa")
    assert [h.origin for h in result.places] == [place_origin("a")]
    assert result.tiles.fetched == 1
