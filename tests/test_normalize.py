from __future__ import annotations

from typing import Any

import pytest

from atgeo._text import normalize

from .helpers import fixture

CASES: list[dict[str, Any]] = fixture("normalize.json")


class TestNormalize:
    @pytest.mark.parametrize("case", CASES, ids=[c["input"] for c in CASES])
    def test_normalizes(self, case: dict[str, Any]) -> None:
        # Equal str means equal code points, and so equal UTF-16: == cannot hide a skipped NFKD.
        assert normalize(case["input"]) == case["expected"]


class TestContractExamples:
    """Contract §7 examples the fixture does not carry."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("①", "1"),
            ("½", "1⁄2"),
            ("a⃝", "a⃝"),  # Me marks stay
            ("a\u200bb", "a\u200bb"),  # zero-width space is not whitespace
            ("a\u0085b", "a\u0085b"),  # nor is NEL
            ("a﻿b", "a b"),  # but the BOM is
            ("a\u001cb", "a\u001cb"),  # str.isspace() says yes; JavaScript's \s says no
            ("  ZANZIBÁR   quokka ", "zanzibar quokka"),
            # Step 1 matters: NFKD first would split ㎏ before Σ is lowercased, so Σ would not be word-final.
            ("ΑΣ㎏", "αςkg"),
            ("ΑΣ™", "αςtm"),
            ("", ""),
        ],
    )
    def test_example(self, text: str, expected: str) -> None:
        assert normalize(text) == expected
