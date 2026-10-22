from __future__ import annotations

import itertools
from typing import Any, ClassVar

import pytest

from atgeo import Bbox, Circle, Region
from atgeo._region import haversine

from .conftest import Make
from .fake_server import FakeServer, FakeTile, FakeTileset, overture
from .helpers import bbox_location, place, place_origin, point, record, tile_url

url = tile_url


def id_of(origin: str | None) -> str:
    return str(origin)[len(place_origin("")) :]


def search(make: Make, records: list[dict[str, Any]], region: Region) -> list[str]:
    fake = FakeServer(tilesets=[overture([FakeTile(url("t"), [record(v) for v in records])])])
    return sorted(id_of(h.origin) for h in make(fake).search_places(region).places)


DIVISION = {"dataset": "overturemaps.org", "collection": "division"}


def divisions(tiles: list[FakeTile]) -> FakeTileset:
    return FakeTileset("overture-division", [DIVISION], tiles)


def division(id: str, name: str, lat: float = 37.775) -> dict[str, Any]:
    return place(id, name, [point(lat, -122.415)], {"origin": f"urn:atgeo:overturemaps.org:division:{id}"})


class TestDeduplication:
    first = place("dup", "First copy", [point(37.775, -122.415)])
    second = place("dup", "Second copy", [point(37.775, -122.415)])
    region = Bbox(-122.42, 37.77, -122.41, 37.78)

    def test_collapses_a_duplicate_origin_across_tiles_to_one_result(self, make: Make) -> None:
        fake = FakeServer(
            tilesets=[
                overture(
                    [
                        FakeTile(
                            url("a"), [record(self.first), record(place("a-only", "A", [point(37.772, -122.412)]))]
                        ),
                        FakeTile(
                            url("b"), [record(self.second), record(place("b-only", "B", [point(37.773, -122.413)]))]
                        ),
                    ]
                )
            ]
        )
        result = make(fake).search_places(self.region)
        assert sorted(id_of(h.origin) for h in result.places) == ["a-only", "b-only", "dup"]

    def test_keeps_the_first_tiles_copy_in_the_default_order_too(self, make: Make) -> None:
        fake = FakeServer(
            tilesets=[overture([FakeTile(url("a"), [record(self.first)]), FakeTile(url("b"), [record(self.second)])])]
        )
        assert [h.name for h in make(fake).search_places(self.region).places] == ["First copy"]

    def test_merges_a_division_record_held_by_two_tiles_into_one_hit(self, make: Make) -> None:
        fake = FakeServer(
            tilesets=[
                divisions(
                    [
                        FakeTile(url("a"), [record(division("d1", "Springfield"))]),
                        FakeTile(url("b"), [record(division("d1", "Springfield"))]),
                    ]
                )
            ]
        )
        result = make(fake).search_places(self.region, dataset="overturemaps.org", collection="division")
        assert len(result.places) == 1
        assert result.places[0].origin == "urn:atgeo:overturemaps.org:division:d1"

    @pytest.mark.parametrize(
        ("east_name", "west_name", "winner"),
        [pytest.param("a", "z", "East copy", id="lower"), pytest.param("z", "a", "West copy", id="higher")],
    )
    def test_keeps_the_lowest_url_copy_across_an_antimeridian_split(
        self, make: Make, east_name: str, west_name: str, winner: str
    ) -> None:
        fake = FakeServer(
            tilesets=[
                overture(
                    [
                        FakeTile(url(east_name), [record(place("x", "East copy", [point(15, 175)]))]),
                        FakeTile(url(west_name), [record(place("x", "West copy", [point(15, -175)]))]),
                    ]
                )
            ],
            tiles_for=lambda bbox: [url(east_name) if bbox.startswith("170") else url(west_name)],
        )
        result = make(fake).search_places(Bbox(170, 10, -170, 20))
        assert [h.name for h in result.places] == [winner]

    def test_keeps_two_records_that_have_neither_origin_nor_uri(self, make: Make) -> None:
        def anonymous(name: str) -> dict[str, Any]:
            return place("x", name, [point(37.775, -122.415)], {"origin": None})

        fake = FakeServer(
            tilesets=[overture([FakeTile(url("a"), [record(anonymous("One")), record(anonymous("Two"))])])]
        )
        result = make(fake).search_places(self.region, dataset="overturemaps.org")
        assert sorted(str(h.name) for h in result.places) == ["One", "Two"]
        assert all(h.origin is None for h in result.places)

    def test_identifies_an_origin_less_record_by_its_uri(self, make: Make) -> None:
        value = place("x", "By uri", [point(37.775, -122.415)], {"origin": None})
        fake = FakeServer(tilesets=[overture([FakeTile(url("a"), [record(value, None, "https://example.org/r/1")])])])
        result = make(fake).search_places(self.region, dataset="overturemaps.org")
        assert [h.origin for h in result.places] == ["https://example.org/r/1"]


class TestPipelineOrder:
    """No reference counterpart: what contract §4.3 and §4.6 fix that no ported test pins."""

    def test_takes_the_copy_from_the_lowest_tile_url_by_utf16_code_units(self, make: Make) -> None:
        # Code-point order puts U+FF61 first; UTF-16 order puts the astral character first.
        low, high = "https://tiles.example/\U00010000.json", "https://tiles.example/\uff61.json"
        fake = FakeServer(
            tilesets=[
                overture(
                    [
                        FakeTile(high, [record(place("x", "High", [point(37.775, -122.415)]))]),
                        FakeTile(low, [record(place("x", "Low", [point(37.775, -122.415)]))]),
                    ]
                )
            ]
        )
        places = make(fake).search_places(Bbox(-122.42, 37.77, -122.41, 37.78)).places
        assert [h.place["name"] for h in places] == ["Low"]

    def test_dedupes_before_the_region_filter_so_an_outside_winner_drops_the_place(self, make: Make) -> None:
        outside = record(place("x", "Outside", [point(10, 10)]))
        inside = record(place("x", "Inside", [point(37.775, -122.415)]))
        fake = FakeServer(tilesets=[overture([FakeTile(tile_url("a"), [outside]), FakeTile(tile_url("b"), [inside])])])
        assert make(fake).search_places(Bbox(-122.42, 37.77, -122.41, 37.78)).places == []


class TestOrderingTies:
    region = Bbox(-122.42, 37.77, -122.41, 37.78)

    def test_orders_records_equal_on_score_importance_and_distance_by_origin_null_last(self, make: Make) -> None:
        def same(id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
            return record(place(id, "Same", [point(37.775, -122.415)], extra), 5)

        fake = FakeServer(
            tilesets=[overture([FakeTile(url("a"), [same("x", {"origin": None}), same("c"), same("a"), same("b")])])]
        )
        result = make(fake).search_places(self.region, dataset="overturemaps.org")
        assert [h.origin for h in result.places] == [place_origin("a"), place_origin("b"), place_origin("c"), None]


class TestRegionFilteringBbox:
    region = Bbox(-122.4194, 37.7749, -122.415, 37.7799)

    def test_drops_a_point_inside_the_snapped_box_but_outside_the_region(self, make: Make) -> None:
        kept = search(
            make,
            [
                place("in", "In", [point(37.777, -122.417)]),
                place("south", "South", [point(37.771, -122.417)]),
                place("east", "East", [point(37.777, -122.411)]),
                place("far", "Far", [point(40, -100)]),
            ],
            self.region,
        )
        assert kept == ["in"]

    def test_keeps_a_point_on_the_region_boundary(self, make: Make) -> None:
        assert search(make, [place("edge", "Edge", [point(37.7749, -122.4194)])], self.region) == ["edge"]

    def test_passes_bbox_only_records_by_intersection(self, make: Make) -> None:
        kept = search(
            make,
            [
                place("overlap", "Overlap", [bbox_location(37.776, 37.7, -122.416, -122.5)]),
                place("encloses", "Encloses", [bbox_location(38, 37, -122, -123)]),
                place("inside", "Inside", [bbox_location(37.777, 37.776, -122.417, -122.418)]),
                place("north", "North", [bbox_location(38, 37.9, -122.416, -122.418)]),
                place("west", "West", [bbox_location(37.777, 37.776, -122.5, -122.6)]),
            ],
            self.region,
        )
        assert kept == ["encloses", "inside", "overlap"]

    def test_tests_a_record_with_both_point_and_bbox_on_the_point(self, make: Make) -> None:
        kept = search(
            make,
            [
                place("point-out", "Point out", [point(40, -100), bbox_location(38, 37, -122, -123)]),
                place("point-in", "Point in", [bbox_location(10, 9, 10, 9), point(37.777, -122.417)]),
            ],
            self.region,
        )
        assert kept == ["point-in"]

    def test_drops_records_with_no_parseable_location(self, make: Make) -> None:
        kept = search(
            make,
            [
                place("ok", "Ok", [point(37.777, -122.417)]),
                place("none", "None", []),
                place(
                    "address",
                    "Address",
                    [{"$type": "community.lexicon.location.address", "country": "US", "street": "1 Main St"}],
                ),
                place("garbage", "Garbage", [{**point(0, 0), "latitude": "north-ish"}]),
                {"$type": "org.atgeo.place", "origin": place_origin("missing"), "name": "Missing"},
            ],
            self.region,
        )
        assert kept == ["ok"]

    def test_handles_a_crossing_region_against_points_and_crossing_bboxes(self, make: Make) -> None:
        kept = search(
            make,
            [
                place("east-side", "E", [point(0, 175)]),
                place("west-side", "W", [point(0, -175)]),
                place("prime", "P", [point(0, 0)]),
                place("crossing-box", "X", [bbox_location(1, -1, -175, 178)]),
                place("far-box", "F", [bbox_location(1, -1, 100, 90)]),
            ],
            Bbox(170, -10, -170, 10),
        )
        assert kept == ["crossing-box", "east-side", "west-side"]


class TestRegionFilteringCircle:
    def test_uses_the_true_radius_not_the_enclosing_square(self, make: Make) -> None:
        lat, lon = 37.7749, -122.4194
        kept = search(
            make,
            [
                place("near", "Near", [point(lat + 0.003, lon)]),
                place("corner", "Corner", [point(lat + 0.004, lon + 0.005)]),
                place("beyond", "Beyond", [point(lat + 0.005, lon)]),
            ],
            Circle(lat, lon, 500),
        )
        assert kept == ["near"]

    def test_passes_bbox_only_records_by_nearest_point_distance(self, make: Make) -> None:
        kept = search(
            make,
            [
                place("close-box", "Close", [bbox_location(0.4, 0.3, 0.4, 0.3)]),
                place("corner-box", "Corner", [bbox_location(0.5, 0.4, 0.5, 0.4)]),
                place("surrounding", "Surrounding", [bbox_location(1, -1, 1, -1)]),
            ],
            Circle(0, 0, 55597),
        )
        assert kept == ["close-box", "surrounding"]

    def test_keeps_a_high_latitude_bbox_only_record_that_clips_the_circle_with_distance_within_the_radius(
        self, make: Make
    ) -> None:
        lat, radius = 85, 370000
        # Measured to the box edge along the center's parallel: 379.9 km. The true nearest point is 357.1 km.
        box = place("clipping", "Clipping", [bbox_location(89, 80, 50, 40)])
        assert haversine(lat, 0, lat, 40) > radius
        fake = FakeServer(tilesets=[overture([FakeTile(url("t"), [record(box)])])])
        places = make(fake).search_places(Circle(lat, 0, radius)).places
        assert [id_of(h.origin) for h in places] == ["clipping"]
        assert places[0].distance <= radius

    def test_finds_the_nearest_longitude_around_the_antimeridian(self, make: Make) -> None:
        kept = search(
            make,
            [
                place("near-edge", "Near", [bbox_location(1, -1, 180, 170)]),
                place("far-edge", "Far", [bbox_location(1, -1, 170, 160)]),
                place("crossing", "Crossing", [bbox_location(1, -1, -170, 170)]),
            ],
            Circle(0, -175, 667170),
        )
        assert kept == ["crossing", "near-edge"]

    def test_measures_point_distance_across_the_antimeridian(self, make: Make) -> None:
        kept = search(
            make,
            [
                place("p-wrap", "Wrap", [point(0, 179.5)]),
                place("p-far", "Far", [point(0, 178)]),
                place("p-same", "Same", [point(0, -176)]),
            ],
            Circle(0, -175, 667170),
        )
        assert kept == ["p-same", "p-wrap"]


class TestDeterminism:
    tiles: ClassVar[list[FakeTile]] = [
        FakeTile(
            url("a"),
            [
                record(place("dup", "Copy A", [point(37.775, -122.415)]), 5),
                record(place("a1", "Alpha", [point(37.772, -122.412)]), 5),
                record(place("a2", "Alpha", [point(37.772, -122.412)]), 5),
            ],
        ),
        FakeTile(
            url("b"),
            [
                record(place("dup", "Copy B", [point(37.775, -122.415)]), 5),
                record(place("b1", "Alpha", [point(37.772, -122.412)]), 5),
            ],
        ),
        FakeTile(
            url("c"),
            [
                record(place("dup", "Copy C", [point(37.775, -122.415)]), 5),
                record(place("c1", "Alpha", [point(37.772, -122.412)]), 5),
            ],
        ),
    ]
    region = Bbox(-122.42, 37.77, -122.41, 37.78)

    @pytest.mark.parametrize("text", [None, "Alpha"])
    def test_gives_identical_output_for_every_tile_arrival_order(self, make: Make, text: str | None) -> None:
        outputs = []
        for order in itertools.permutations("abc"):
            fake = FakeServer(tilesets=[overture(self.tiles)], delays={url(n): i * 0.015 for i, n in enumerate(order)})
            outputs.append(make(fake).search_places(self.region, text=text))
        assert all(o == outputs[0] for o in outputs)
        places = outputs[0].places
        assert sorted(id_of(h.origin) for h in places) == ["a1", "a2", "b1", "c1", "dup"]
        assert next(h for h in places if id_of(h.origin) == "dup").name == "Copy A"
