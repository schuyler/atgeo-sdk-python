from __future__ import annotations

import pytest

from atgeo._region import center, query_bboxes
from atgeo.types import Bbox, Circle, Region


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
