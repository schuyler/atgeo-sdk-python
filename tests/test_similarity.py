from __future__ import annotations

from typing import Any

import pytest

from atgeo._text import jaro_winkler, text_score

from .helpers import fixture

CASES: list[dict[str, Any]] = fixture("jaro-winkler.json")
IDS = [f"{c['a']}/{c['b']}" for c in CASES]


class TestJaroWinkler:
    @pytest.mark.parametrize("case", CASES, ids=IDS)
    def test_scores(self, case: dict[str, Any]) -> None:
        assert abs(jaro_winkler(case["a"], case["b"]) - case["score"]) <= 5e-7

    @pytest.mark.parametrize("case", CASES, ids=IDS)
    def test_is_symmetric(self, case: dict[str, Any]) -> None:
        assert jaro_winkler(case["a"], case["b"]) == jaro_winkler(case["b"], case["a"])


class TestUtf16CodeUnits:
    """Characters above U+FFFF, which the fixture does not reach (Python spec §6). Values are V8's, exactly."""

    @pytest.mark.parametrize(
        ("a", "b", "score"),
        [
            ("😀😁", "😀😂", 0.8833333333333334),
            ("pizza", "pizza 🍕", 0.925),
            ("a😀", "a", 0.7999999999999999),
            ("\ud800x", "\ud800y😀", 0.625),  # a lone surrogate, which JSON can carry
        ],
    )
    def test_matches_the_reference(self, a: str, b: str, score: float) -> None:
        assert jaro_winkler(a, b) == score
        assert jaro_winkler(b, a) == score

    def test_runs_an_empty_match_window(self) -> None:
        assert 0 <= jaro_winkler("starbux", "bar") < 1


class TestTextScore:
    """Contract §7's scoring examples."""

    @pytest.mark.parametrize(
        ("name", "score"), [("Starbucks", 0.9048), ("Star Bakery", 0.8740), ("Stardust Bar", 0.8530)]
    )
    def test_starbux(self, name: str, score: float) -> None:
        assert round(text_score("starbux", {"name": name}), 4) == score

    def test_drops_home_depot(self) -> None:
        assert text_score("starbux", {"name": "Home Depot"}) < 0.6

    def test_matches_a_variant(self) -> None:
        assert text_score("munich", {"name": "München", "variants": [{"name": "Munich"}]}) == 1

    def test_skips_unreadable_names(self) -> None:
        value = {"name": 5, "variants": [None, 3, {"name": ["x"]}, {"name": "  "}, {"name": "Munich"}]}
        assert text_score("munich", value) == 1
        assert text_score("munich", {"variants": "Munich"}) == 0
