from __future__ import annotations

import math

import pytest

from atgeo._locations import Located, distance_to, in_region, locate
from atgeo._region import haversine
from atgeo.types import Bbox, Circle

from .helpers import bbox_location, place, point


def box_at(south: float, north: float, west: float, east: float) -> Located:
    return locate(place("b", "Box", [bbox_location(north, south, east, west)]))


def point_at(lat: float, lon: float) -> Located:
    return locate(place("p", "Point", [point(lat, lon)]))


def close(a: float | None, b: float, rel: float = 1e-6) -> bool:
    return a is not None and math.isclose(a, b, rel_tol=rel, abs_tol=1e-9)


def sampled_distance(  # noqa: PLR0917
    lat: float, lon: float, south: float, north: float, west: float, east: float
) -> float:
    """Minimum haversine distance from the centre to a dense sampling of the box boundary."""
    steps = 100000
    spans = [(west, east)] if west <= east else [(west, 180.0), (-180.0, east)]
    best = math.inf
    for lo, hi in spans:
        for i in range(steps + 1):
            la = south + (north - south) * i / steps
            lo2 = lo + (hi - lo) * i / steps
            for a, b in ((la, lo), (la, hi), (south, lo2), (north, lo2)):
                best = min(best, haversine(lat, lon, a, b))
    return best


class TestDistanceToContainmentIsExactlyZero:
    def test_for_a_latitude_that_does_not_survive_a_radian_round_trip(self) -> None:
        assert distance_to(box_at(46, 47, 10, 11), 46.0986, 10.5) == 0

    def test_for_center_lon_180_in_a_box_with_west_minus_180(self) -> None:
        assert distance_to(box_at(40, 50, -180, -170), 45, 180) == 0

    def test_for_center_lon_minus_180_in_a_box_with_east_180(self) -> None:
        assert distance_to(box_at(40, 50, 170, 180), 45, -180) == 0

    def test_for_a_pole_center_in_a_box_reaching_the_pole_whose_longitudes_exclude_0(self) -> None:
        assert distance_to(box_at(-90, -80, 10, 20), -90, 0) == 0

    def test_for_a_point_exactly_at_the_center(self) -> None:
        assert distance_to(point_at(46.0986, 10.5), 46.0986, 10.5) == 0

    @pytest.mark.parametrize(("point_lon", "center_lon"), [(180, -180), (-180, 180)])
    def test_for_a_point_at_lon_and_a_center_at_its_seam_twin(self, point_lon: float, center_lon: float) -> None:
        assert distance_to(point_at(10, point_lon), 10, center_lon) == 0


class TestDistanceToOutsideABox:
    @pytest.mark.parametrize(
        ("lat", "lon", "box"),
        [
            pytest.param(0, 5, (10, 20, 0, 10), id="due south within the longitude range"),
            pytest.param(65, 10, (60, 70, 20, 30), id="east of a high-latitude box"),
            pytest.param(50, 0, (40, 60, 120, 140), id="more than 90 degrees of longitude away"),
            pytest.param(60, -179, (55, 65, 175, 179), id="across the antimeridian from a box"),
            pytest.param(45, -160, (40, 50, 170, -170), id="from an antimeridian-crossing box"),
            pytest.param(45, 150, (40, 50, 170, -170), id="west of an antimeridian-crossing box"),
        ],
    )
    def test_matches_the_sampled_boundary_distance(
        self, lat: float, lon: float, box: tuple[float, float, float, float]
    ) -> None:
        expected = sampled_distance(lat, lon, *box)
        got = distance_to(box_at(*box), lat, lon)
        assert got is not None
        assert abs(got - expected) < 0.05  # toBeCloseTo(expected, 1)

    def test_is_strictly_less_than_the_latitude_clamped_distance_for_a_high_latitude_box_to_the_east(self) -> None:
        clamped = haversine(65, 10, 65, 20)
        actual = distance_to(box_at(60, 70, 20, 30), 65, 10)
        assert actual is not None
        assert actual < clamped - 1000


class TestDistanceToOutOfRangeCoordinates:
    def test_is_nan_where_javascript_gives_nan_rather_than_raising(self) -> None:
        # haversine's `a` goes negative here: math.sqrt would raise, Math.sqrt gives NaN.
        assert math.isnan(distance_to(point_at(95, 180), 85, 0) or 0.0)


class TestDistanceToOutOfRangeBox:
    def test_is_nan_when_any_candidate_is_as_math_min_gives(self) -> None:
        # Only the north corner (latitude 215) is NaN; min() would skip it, Math.min does not.
        loc = locate({"locations": [{"$type": "community.lexicon.location.bbox", "south": "7", "north": "215",
                                     "west": "-24", "east": "-24"}]})  # fmt: skip
        assert math.isnan(distance_to(loc, -35, 156) or 0.0)
        assert in_region(loc, Circle(-35, 156, 2e7)) is False


class TestDistanceToNoLocation:
    def test_is_none(self) -> None:
        assert distance_to(locate(place("n", "None", [])), 0, 0) is None


def region(west: float, south: float, east: float, north: float) -> Bbox:
    return Bbox(west, south, east, north)


class TestInRegionBboxRegionSeams:
    @pytest.mark.parametrize(
        ("loc", "r"),
        [
            pytest.param(
                point_at(45, 180), region(-180, 40, -170, 50), id="a point at lon 180 in a region with west -180"
            ),
            pytest.param(
                point_at(45, -180), region(170, 40, 180, 50), id="a point at lon -180 in a region with east 180"
            ),
            pytest.param(
                box_at(40, 50, 170, 180),
                region(-180, 40, -170, 50),
                id="a box ending at 180 and a region starting at -180",
            ),
            pytest.param(
                box_at(40, 50, -180, -170),
                region(170, 40, 180, 50),
                id="a box starting at -180 and a region ending at 180",
            ),
            pytest.param(point_at(90, 0), region(10, 80, 20, 90), id="a point at the pole whatever its longitude"),
            pytest.param(
                box_at(85, 90, 100, 110), region(10, 80, 20, 90), id="a box reaching the pole at other longitudes"
            ),
        ],
    )
    def test_keeps(self, loc: Located, r: Bbox) -> None:
        assert in_region(loc, r) is True

    @pytest.mark.parametrize(
        ("loc", "r"),
        [
            pytest.param(point_at(45, 179.99), region(-180, 40, -170, 50), id="a point just short of the seam"),
            pytest.param(box_at(40, 50, 160, 179.99), region(-180, 40, -170, 50), id="a box just short of the seam"),
            pytest.param(
                box_at(85, 90, 100, 110),
                region(10, 80, 20, 89.99),
                id="a polar box when the region stops short of the pole",
            ),
        ],
    )
    def test_drops(self, loc: Located, r: Bbox) -> None:
        assert in_region(loc, r) is False


class TestWorkedExamples:
    """Contract §6's tables, to 1e-6 relative; containment exactly."""

    def test_haversine(self) -> None:
        assert close(haversine(0, 0, 1, 0), 111195.0802335329)
        assert close(haversine(37.7749, -122.4194, 34.0522, -118.2437), 559121.3493517868)

    @pytest.mark.parametrize(
        ("loc", "center", "metres"),
        [
            (point_at(0.001, 0), (0, 0), 111.19508023353292),
            (box_at(44, 46, 9, 11), (45, 10), 0),
            (box_at(44, 46, 9, 11), (45, 8), 78624.79923702011),
            (box_at(44, 46, 9, 11), (47, 10), 111195.0802335329),
            (box_at(44, 46, 9, 11), (47, 8), 134989.80127268212),
            (box_at(70, 80, 10, 30), (75, 60), 826787.9307589367),
            (box_at(40, 50, -180, -170), (45, 180), 0),
            (box_at(-10, 10, 170, -170), (0, 160), 1111950.8023353291),
        ],
    )
    def test_distance(self, loc: Located, center: tuple[float, float], metres: float) -> None:
        got = distance_to(loc, *center)
        if metres == 0:
            assert got == 0
        else:
            assert close(got, metres)
