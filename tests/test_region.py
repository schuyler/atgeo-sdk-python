from __future__ import annotations

import dataclasses
import math
import re
import urllib.parse

import pytest

from atgeo._region import center, query_bboxes
from atgeo.types import Bbox, Circle, Region

from .conftest import Make
from .fake_server import FakeServer, FakeTile, overture
from .helpers import OVERTURE, place, place_origin, point, record

COORD = re.compile(r"^-?\d{1,3}\.\d{2}$")


def bboxes_sent(fake: FakeServer) -> list[str]:
    out = []
    for u in fake.requests:
        if "org.atgeo.getTiles" in u:
            out.append(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query)).get("bbox", ""))
    return out


def emitted(make: Make, region: Region) -> list[str]:
    fake = FakeServer()
    make(fake).search_places(region, **OVERTURE)
    return bboxes_sent(fake)


def parse(bbox: str) -> list[float]:
    return [float(x) for x in bbox.split(",")]


class TestBboxFieldNames:
    """The region's fields are west, south, east and north, the wire order (contract §3, garganorn#16)."""

    def test_names_the_fields_west_south_east_north_in_that_order(self) -> None:
        assert [f.name for f in dataclasses.fields(Bbox)] == ["west", "south", "east", "north"]
        box = Bbox(-122.42, 37.77, -122.41, 37.78)
        assert (box.west, box.south, box.east, box.north) == (-122.42, 37.77, -122.41, 37.78)

    def test_takes_the_fields_by_keyword(self, make: Make) -> None:
        by_name = Bbox(west=-122.42, south=37.77, east=-122.41, north=37.78)
        assert by_name == Bbox(-122.42, 37.77, -122.41, 37.78)
        assert emitted(make, by_name) == ["-122.42,37.77,-122.41,37.78"]

    def test_crosses_the_antimeridian_when_west_is_greater_than_east(self, make: Make) -> None:
        assert emitted(make, Bbox(west=170, south=10, east=-170, north=20)) == [
            "170.00,10.00,180.00,20.00",
            "-180.00,10.00,-170.00,20.00",
        ]
        assert center(Bbox(west=170, south=10, east=-170, north=20)) == (15, 180)

    @pytest.mark.parametrize("old", ["min_lon", "min_lat", "max_lon", "max_lat"])
    def test_has_no_field_under_its_old_name(self, old: str) -> None:
        assert not hasattr(Bbox(0, 0, 1, 1), old)
        with pytest.raises(TypeError):
            Bbox(**{old: 0.0})

    def test_names_the_fields_in_the_invalid_bbox_message(self, make: Make) -> None:
        with pytest.raises(ValueError, match="south > north"):
            make(FakeServer()).search_places(Bbox(west=0, south=10, east=1, north=5), **OVERTURE)


class TestWorkedExamples:
    """Contract §3's tables, computed with the reference implementation."""

    @pytest.mark.parametrize(
        ("region", "expected"),
        [
            (Circle(37.7598, -122.4194, 1000), ["-122.44,37.75,-122.40,37.77"]),
            (Circle(10, 20, 0), ["20.00,10.00,20.01,10.01"]),
            (Circle(0, 179.999, 5000), ["179.95,-0.05,180.00,0.05", "-180.00,-0.05,-179.95,0.05"]),
            (Circle(0, -179.999, 5000), ["179.95,-0.05,180.00,0.05", "-180.00,-0.05,-179.95,0.05"]),
            (Circle(89.9, 0, 50000), ["-180.00,89.45,180.00,90.00"]),
            (Circle(80, 0, 100000), ["-5.19,79.10,5.19,80.90"]),
            (Circle(-0.0, -0.0, 0), ["0.00,0.00,0.01,0.01"]),
            (Bbox(-122.42, 37.77, -122.41, 37.78), ["-122.42,37.77,-122.41,37.78"]),
            (Bbox(-122.4194155, 37.7749295, -122.4094155, 37.7849295), ["-122.42,37.77,-122.40,37.79"]),
            (Bbox(170, 10, -170, 20), ["170.00,10.00,180.00,20.00", "-180.00,10.00,-170.00,20.00"]),
            (Bbox(180, 90, 180, 90), ["179.99,89.99,180.00,90.00"]),
            (Bbox(10, 10, 10.5, 10), ["10.00,10.00,10.50,10.01"]),
            # The reference's float expressions, to the last bit: (x * PI) / 180, not x * (PI / 180).
            (Circle(31.56740462151079, -118.62, 245355.87467988167), ["-121.22,29.36,-116.03,33.78"]),
        ],
        ids=str,
    )
    def test_bbox_strings(self, region: Region, expected: list[str]) -> None:
        assert query_bboxes(region) == expected

    @pytest.mark.parametrize(
        ("region", "expected"),
        [(Bbox(170, 10, -170, 20), (15, 180)), (Bbox(100, 0, -100, 0), (0, 180)), (Circle(1, 2, 3), (1, 2))],
        ids=str,
    )
    def test_centre(self, region: Region, expected: tuple[float, float]) -> None:
        assert center(region) == expected


class TestCoordinateSerialization:
    @pytest.mark.parametrize(
        "region",
        [
            pytest.param(Circle(37.7749295, -122.4194155, 250.123456), id="full GPS precision circle"),
            pytest.param(Bbox(-122.4194155, 37.7749295, -122.4094155, 37.7849295), id="full GPS precision bbox"),
            pytest.param(Bbox(-122.4, 37.7, -122.3, 37.8), id="trailing zeros"),
            pytest.param(Bbox(1e-7, 1e-7, 2e-7, 2e-7), id="scientific notation tiny"),
            pytest.param(Bbox(3.7749e1, 3.7749e1, 3.775e1, 3.776e1), id="scientific notation"),
            pytest.param(Bbox(-1e-7, -2e-7, 1e-7, 2e-7), id="negative scientific notation"),
            pytest.param(Circle(12.3456789, 98.7654321, 1e-9), id="tiny radius"),
            pytest.param(Circle(12.3456789, 98.7654321, 0), id="radius zero"),
            pytest.param(Bbox(10.123456, 20.123456, 10.123456, 20.123456), id="zero-area bbox"),
            pytest.param(Bbox(-0.0, -0.0, 0, 0), id="negative zero bbox"),
            pytest.param(Circle(-0.0, -0.0, 0), id="negative zero circle"),
            pytest.param(Bbox(-0.0001, -0.0001, -0.00001, -0.00001), id="tiny negative"),
            pytest.param(Circle(90, 0, 1000), id="circle at pole"),
            pytest.param(Circle(0, 180, 1000), id="circle at antimeridian"),
            pytest.param(Bbox(179.123456, 1.987654, -179.123456, 2.987654), id="crossing bbox"),
        ],
    )
    def test_emits_only_two_decimal_coordinates(self, make: Make, region: Region) -> None:
        sent = emitted(make, region)
        assert len(sent) > 0
        for bbox in sent:
            parts = bbox.split(",")
            assert len(parts) == 4
            for c in parts:
                assert COORD.match(c), c

    def test_never_emits_a_coordinate_outside_the_valid_range(self, make: Make) -> None:
        regions: list[Region] = [
            Bbox(-180, -90, 180, 90),
            Bbox(180, 90, 180, 90),
            Bbox(-180, -90, -180, -90),
            Circle(90, 180, 5),
            Circle(-90, -180, 5),
            Circle(89.999, 179.999, 100000),
        ]
        for region in regions:
            for bbox in emitted(make, region):
                west, south, east, north = parse(bbox)
                assert west >= -180
                assert east <= 180
                assert south >= -90
                assert north <= 90
                assert west < east
                assert south < north


class TestOutwardSnapping:
    def test_floors_mins_and_ceils_maxes(self, make: Make) -> None:
        assert emitted(make, Bbox(-122.4194155, 37.7749295, -122.4094155, 37.7849295)) == [
            "-122.42,37.77,-122.40,37.79"
        ]

    def test_contains_the_callers_region_for_arbitrary_precision_input(self, make: Make) -> None:
        inputs = [
            (-122.419415, 37.774929, -122.418001, 37.775001),
            (0.001, 0.002, 0.003, 0.004),
            (-0.004, -0.003, -0.002, -0.001),
            (151.2093, -33.8688, 151.2193, -33.8588),
            (-73.9857, 40.7484, -73.9757, 40.7584),
        ]
        for west, south, east, north in inputs:
            [bbox] = emitted(make, Bbox(west, south, east, north))
            a, b, c, d = parse(bbox)
            assert a <= west
            assert b <= south
            assert c >= east
            assert d >= north
            assert c - a < east - west + 0.0201
            assert d - b < north - south + 0.0201

    def test_does_not_widen_a_grid_aligned_bbox_through_float_error(self, make: Make) -> None:
        assert emitted(make, Bbox(-122.42, 37.77, -122.41, 37.78)) == ["-122.42,37.77,-122.41,37.78"]

    def test_does_not_widen_grid_aligned_values_anywhere_on_the_grid(self, make: Make) -> None:
        for k in range(0, 9000, 97):
            lat = [(k + 1) / 100, (k + 6) / 100]
            lon = [-(k + 8) / 100, -(k + 5) / 100]
            expected = ",".join(f"{v:.2f}" for v in (lon[0], lat[0], lon[1], lat[1]))
            assert emitted(make, Bbox(lon[0], lat[0], lon[1], lat[1])) == [expected]


class TestDegenerateSpans:
    def test_widens_a_grid_aligned_point_by_one_step(self, make: Make) -> None:
        assert emitted(make, Bbox(10, 20, 10, 20)) == ["10.00,20.00,10.01,20.01"]

    def test_widens_a_zero_radius_circle_on_the_grid(self, make: Make) -> None:
        assert emitted(make, Circle(20, 10, 0)) == ["10.00,20.00,10.01,20.01"]

    def test_widens_negative_zero(self, make: Make) -> None:
        assert emitted(make, Bbox(-0.0, -0.0, 0, 0)) == ["0.00,0.00,0.01,0.01"]

    def test_widens_a_line(self, make: Make) -> None:
        assert emitted(make, Bbox(10, 20, 10.5, 20)) == ["10.00,20.00,10.50,20.01"]

    def test_steps_min_down_at_latitude_90_and_longitude_180(self, make: Make) -> None:
        assert emitted(make, Bbox(0, 90, 0, 90)) == ["0.00,89.99,0.01,90.00"]
        assert emitted(make, Bbox(180, 0, 180, 0)) == ["179.99,0.00,180.00,0.01"]

    def test_does_not_widen_a_span_that_snapping_already_opened(self, make: Make) -> None:
        assert emitted(make, Bbox(10.005, 20.005, 10.005, 20.005)) == ["10.00,20.00,10.01,20.01"]


class TestCircleToBbox:
    def test_uses_111194_927_m_per_degree_of_latitude(self, make: Make) -> None:
        assert emitted(make, Circle(0, 0, 111194.927)) == ["-1.00,-1.00,1.00,1.00"]

    def test_widens_longitude_to_the_spherical_caps_half_width(self, make: Make) -> None:
        [bbox] = emitted(make, Circle(60, 10, 111194.927))
        west, south, east, north = parse(bbox)
        assert abs(south - 59) < 0.05  # toBeCloseTo(59, 1)
        assert abs(north - 61) < 0.05
        assert 7.99 <= west <= 8
        assert 12 <= east <= 12.01

    # asin(sin 20° / cos 60°) = 43.16°; the flat 20° / cos 60° is 40°.
    def test_reaches_the_caps_true_east_and_west_edges_at_high_latitude(self, make: Make) -> None:
        assert emitted(make, Circle(60, 0, 20 * 111194.927)) == ["-43.17,40.00,43.17,80.00"]

    def test_covers_all_longitudes_for_a_polar_circle(self, make: Make) -> None:
        north = emitted(make, Circle(89.9, 12.34, 50000))
        assert len(north) == 1
        n_west, n_south, n_east, n_north = parse(north[0])
        assert [n_west, n_east, n_north] == [-180, 180, 90]
        assert 89.44 <= n_south <= 89.46

        south = emitted(make, Circle(-89.9, -45, 50000))
        assert len(south) == 1
        s_west, s_south, s_east, s_north = parse(south[0])
        assert [s_west, s_east, s_south] == [-180, 180, -90]
        assert -89.46 <= s_north <= -89.44


class TestAntimeridian:
    def test_splits_a_crossing_bbox_into_two_get_tiles_calls(self, make: Make) -> None:
        assert emitted(make, Bbox(170, 10, -170, 20)) == ["170.00,10.00,180.00,20.00", "-180.00,10.00,-170.00,20.00"]

    def test_splits_a_circle_that_wraps_past_180(self, make: Make) -> None:
        assert emitted(make, Circle(0, 179.5, 111194.927)) == ["178.50,-1.00,180.00,1.00", "-180.00,-1.00,-179.50,1.00"]

    def test_splits_a_circle_that_wraps_past_minus_180(self, make: Make) -> None:
        assert emitted(make, Circle(0, -179.5, 111194.927)) == [
            "179.50,-1.00,180.00,1.00",
            "-180.00,-1.00,-178.50,1.00",
        ]

    def test_issues_a_single_call_for_a_non_crossing_bbox(self, make: Make) -> None:
        assert len(emitted(make, Bbox(-10, 10, 10, 20))) == 1

    def test_snaps_each_half_independently(self, make: Make) -> None:
        assert emitted(make, Bbox(170.123, 10.123, -170.123, 20.123)) == [
            "170.12,10.12,180.00,20.13",
            "-180.00,10.12,-170.12,20.13",
        ]

    def test_merges_results_from_both_halves(self, make: Make) -> None:
        west = "https://tiles.example/west.json"
        east = "https://tiles.example/east.json"
        fake = FakeServer(
            tilesets=[
                overture(
                    [
                        FakeTile(west, [record(place("w", "West", [point(15, 175)]))]),
                        FakeTile(east, [record(place("e", "East", [point(15, -175)]))]),
                    ]
                )
            ],
            tiles_for=lambda bbox: [west] if bbox.startswith("170") else [east],
        )
        result = make(fake).search_places(Bbox(170, 10, -170, 20), **OVERTURE)
        assert sorted(str(h.origin) for h in result.places) == [place_origin("e"), place_origin("w")]
        assert result.complete is True


class TestInvalidRegions:
    @pytest.mark.parametrize(
        "region",
        [
            pytest.param(Circle(math.nan, 0, 10), id="NaN lat"),
            pytest.param(Circle(0, math.nan, 10), id="NaN lon"),
            pytest.param(Circle(0, math.inf, 10), id="Infinity lon"),
            pytest.param(Circle(0, 0, math.nan), id="NaN radius"),
            pytest.param(Circle(0, 0, math.inf), id="Infinity radius"),
            pytest.param(Circle(0, 0, -1), id="negative radius"),
            pytest.param(Circle(90.01, 0, 10), id="lat above 90"),
            pytest.param(Circle(-91, 0, 10), id="lat below -90"),
            pytest.param(Circle(0, 180.5, 10), id="lon above 180"),
            pytest.param(Circle(0, -181, 10), id="lon below -180"),
            pytest.param(Bbox(0, 0, math.nan, 1), id="bbox NaN"),
            pytest.param(Bbox(0, 0, 1, math.inf), id="bbox Infinity"),
            pytest.param(Bbox(-math.inf, 0, 1, 1), id="bbox -Infinity"),
            pytest.param(Bbox(0, 10, 1, 5), id="bbox south > north"),
            pytest.param(Bbox(0, 0, 1, 91), id="bbox lat above 90"),
            pytest.param(Bbox(0, -91, 1, 0), id="bbox lat below -90"),
            pytest.param(Bbox(0, 0, 181, 1), id="bbox lon above 180"),
            pytest.param(Bbox(-181, 0, 1, 1), id="bbox lon below -180"),
        ],
    )
    def test_raises_value_error_before_any_request(self, make: Make, region: Region) -> None:
        fake = FakeServer()
        with pytest.raises(ValueError, match="invalid"):
            make(fake).search_places(region, **OVERTURE)
        assert fake.requests == []

    def test_accepts_west_greater_than_east(self, make: Make) -> None:
        assert make(FakeServer()).search_places(Bbox(170, 0, -170, 1), **OVERTURE) is not None

    def test_raises_value_error_before_any_request_for_similarity_sort_without_text(self, make: Make) -> None:
        fake = FakeServer()
        with pytest.raises(ValueError, match="similarity"):
            make(fake).search_places(Bbox(0, 0, 1, 1), sort="similarity", **OVERTURE)
        assert fake.requests == []

    # Python-specific: the argument checks the reference's types make unnecessary.
    def test_raises_value_error_before_any_request_for_an_unknown_sort(self, make: Make) -> None:
        fake = FakeServer()
        with pytest.raises(ValueError, match="unknown sort"):
            make(fake).search_places(Bbox(0, 0, 1, 1), sort="nearest", **OVERTURE)
        assert fake.requests == []

    def test_raises_type_error_before_any_request_for_a_region_of_the_wrong_type(self, make: Make) -> None:
        fake = FakeServer()
        with pytest.raises(TypeError, match="Circle or a Bbox"):
            make(fake).search_places((0, 0, 1, 1), **OVERTURE)
        assert fake.requests == []

    def test_checks_the_region_before_the_sort(self, make: Make) -> None:
        with pytest.raises(ValueError, match="invalid"):
            make(FakeServer()).search_places(Circle(math.nan, 0, 1), sort="nearest", **OVERTURE)
        with pytest.raises(ValueError, match="unknown sort"):
            make(FakeServer()).search_places(Circle(0, 0, 1), sort="nearest", text=None, **OVERTURE)
