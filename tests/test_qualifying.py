"""qualifying.test.ts, ported: fixtures/qualifying-keywords.json (contract §17), the predicate that decides whether
the fallback and the autocompleter send a keyword."""

from __future__ import annotations

from typing import Any

import pytest

from atgeo._text import qualifies

from .helpers import fixture

CASES: list[dict[str, Any]] = fixture("qualifying-keywords.json")


class TestQualifyingKeywords:
    def test_has_every_case(self) -> None:
        assert len(CASES) == 23

    @pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
    def test_case(self, case: dict[str, Any]) -> None:
        assert qualifies(case["keyword"]) is case["qualifies"]

    def test_counts_code_points_not_utf16_units(self) -> None:
        assert qualifies("\U0001f600") is False  # one character, two UTF-16 units
        assert qualifies("\U0001f600\U0001f601") is True

    def test_reads_a_surrogate_pair_as_the_one_character_it_encodes(self) -> None:
        # Python strings can hold the two halves apart; JavaScript's cannot, and counts one character.
        assert qualifies("\ud83d\ude00") is False
        assert qualifies("\ud83d\ude00\ud83d\ude01") is True

    def test_counts_a_lone_surrogate_as_one_character(self) -> None:
        assert qualifies("\ud800") is False
        assert qualifies("\ud800\ud800") is True  # two lone high surrogates

    def test_takes_only_the_first_twelve_distinct_words(self) -> None:
        eleven = " ".join("abcdefghijk")  # eleven distinct one-letter words
        assert qualifies(f"{eleven} l ab") is False  # "ab" is the thirteenth distinct word
        assert qualifies(f"{eleven} ab") is True  # the twelfth
        assert qualifies(f"{eleven} a a a ab") is True  # repeats do not count: "ab" is still the twelfth

    def test_does_not_use_match_tiers_tokenization(self) -> None:
        # Tier matching takes twelve words before deduplicating; here the twelfth distinct word is the thirteenth.
        words = " ".join(["a"] * 12 + ["ab"])
        assert qualifies(words) is True

    def test_never_raises_for_a_keyword_with_lone_surrogates_and_marks(self) -> None:
        assert qualifies("का \ud800") is False
