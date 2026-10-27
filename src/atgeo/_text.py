"""Normalization, Jaro-Winkler and text scoring (contract §7), and UTF-16 ordering."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

JW_THRESHOLD = 0.6
MAX_QUERY_TOKENS = 12

# JavaScript's \s, exactly. None of Python's predicates (str.isspace, str.split(), str.strip, re's \s)
# is this set: they include U+001C to U+001F and U+0085 and exclude U+FEFF. Escapes, so that no
# editor can lose an invisible character.
_WHITESPACE = frozenset("\u0009\u000a\u000b\u000c\u000d                  　﻿")


def normalize(s: str) -> str:
    # str.lower() is Unicode's default full case mapping with Final_Sigma, independent of locale.
    folded = unicodedata.normalize("NFKD", s.lower()).lower()  # 1. lowercase, 2. NFKD, 3. lowercase again
    out: list[str] = []
    pending_space = False
    for ch in folded:
        if unicodedata.category(ch) == "Mn":  # 4. drop nonspacing marks
            continue
        if ch in _WHITESPACE:  # 5. collapse runs and trim both ends
            pending_space = bool(out)
            continue
        if pending_space:
            out.append(" ")
            pending_space = False
        out.append(ch)
    return "".join(out)


def utf16(s: str) -> bytes:
    """A sort key that orders strings by UTF-16 code units, as JavaScript does (contract §4.3)."""
    return s.encode("utf-16-be", "surrogatepass")  # big-endian bytes compare in code-unit order


def _units(s: str) -> list[int]:
    return memoryview(s.encode("utf-16-le", "surrogatepass")).cast("H").tolist()


def _pair(a: str, b: str) -> tuple[Sequence[object], Sequence[object]]:
    # The contract compares UTF-16 code units. Without a character above U+FFFF on either side the code
    # points are the code units, so compare the strings as they are. The decision is for the pair: a str
    # against a list of ints would compare unequal everywhere.
    both = a + b
    if both.isascii() or max(map(ord, both)) <= 0xFFFF:
        return a, b
    return _units(a), _units(b)


def _jaro(a: Sequence[object], b: Sequence[object]) -> float:
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if not la or not lb:
        return 0.0
    reach = max(0, max(la, lb) // 2 - 1)
    a_matched = [False] * la
    b_matched = [False] * lb
    matches = 0
    for i in range(la):
        # Runs zero times when the window starts past its end (starbux against bar).
        for j in range(max(0, i - reach), min(lb - 1, i + reach) + 1):
            if not b_matched[j] and a[i] == b[j]:
                a_matched[i] = b_matched[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    transpositions = 0
    j = 0
    for i in range(la):
        if not a_matched[i]:
            continue
        while not b_matched[j]:
            j += 1
        if a[i] != b[j]:
            transpositions += 1
        j += 1
    # transpositions can be odd: halve it in floating point.
    return (matches / la + matches / lb + (matches - transpositions / 2) / matches) / 3


def jaro_winkler(a: str, b: str) -> float:
    ua, ub = _pair(a, b)
    sim = _jaro(ua, ub)
    prefix = 0
    while prefix < 4 and prefix < len(ua) and prefix < len(ub) and ua[prefix] == ub[prefix]:
        prefix += 1
    return sim + prefix * 0.1 * (1 - sim)


def _score_name(query: str, tokens: list[str], name: str) -> float:
    name_tokens = name.split(" ")  # never split() with no argument: its whitespace is not the contract's
    token_score = sum(max(jaro_winkler(t, n) for n in name_tokens) for t in tokens) / len(tokens)
    return 0.5 * jaro_winkler(query, name) + 0.5 * token_score


def _names(value: Mapping[str, Any]) -> list[str]:
    """A place's normalized, non-empty ``name`` and ``variants[].name``: what a query is scored and tiered against."""
    raw: list[object] = [value.get("name")]
    variants = value.get("variants")
    if type(variants) is list:
        raw.extend(v.get("name") for v in variants if type(v) is dict)
    return [name for n in raw if type(n) is str and (name := normalize(n))]


def text_score(query: str, value: Mapping[str, Any]) -> float:
    """The best score of the place's names against an already-normalized, non-empty query."""
    tokens = query.split(" ")[:MAX_QUERY_TOKENS]
    return max((_score_name(query, tokens, name) for name in _names(value)), default=0.0)


_APOSTROPHES = str.maketrans("", "", "'\u2019\u02bc")


def _fold(word: str) -> list[bytes]:
    """The words of a normalized name or query word for tier matching (contract §7), as UTF-16 sort keys:
    apostrophes vanish, and each run of punctuation (general category P*) separates words. Symbols do not."""
    spaced = "".join(" " if unicodedata.category(c)[0] == "P" else c for c in word.translate(_APOSTROPHES))
    return [utf16(t) for t in spaced.split(" ") if t]


def match_tier(query: str, value: Mapping[str, Any]) -> int:
    """1 if every query word begins a word of one single name, else 2 (contract §7); 1 for a query that folds to
    no words. ``query`` is already normalized and non-empty; call it for a record that scored at least 0.6.

    The prefix test is on UTF-16 code units, as the contract asks, so a query ending in a lone high surrogate
    prefixes a name holding the whole pair, where ``str.startswith`` would say no."""
    words = [t for w in query.split(" ")[:MAX_QUERY_TOKENS] for t in _fold(w)]
    if not words:
        return 1
    for name in _names(value):
        name_words = _fold(name)
        if all(any(n.startswith(q) for n in name_words) for q in words):
            return 1
    return 2
