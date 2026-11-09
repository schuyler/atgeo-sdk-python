from __future__ import annotations

import math
from typing import Any

import pytest

from atgeo._rank import rank, round3
from atgeo._text import jaro_winkler, match_tier, normalize, text_score
from atgeo._wire import TileRecord
from atgeo.types import Bbox, SortMode

from .conftest import Make
from .fake_server import FakeServer, FakeTile, overture
from .helpers import bbox_location, fixture, place, place_origin, point, record, region_of

SCENARIOS: list[dict[str, Any]] = fixture("ranking-scenarios.json")
TIERS: list[dict[str, Any]] = fixture("match-tiers.json")
LONE_SURROGATES: list[dict[str, Any]] = fixture("match-tiers-lone-surrogates.json")

REGION = Bbox(-1, -1, 1, 1)


class TestRound3:
    @pytest.mark.parametrize(
        ("x", "expected"),
        [
            (0.0625, 0.063),  # exactly representable: round(x, 3) gives 0.062, Math.round half up 0.063
            (0.0025, 0.003),
            (0.0015, 0.002),
            (0.9048, 0.905),
            (0.49999999999999994, 0.5),
            (1.0, 1.0),
            (0.0, 0.0),
        ],
    )
    def test_rounds_half_up_like_math_round(self, x: float, expected: float) -> None:
        assert round3(x) == expected


class TestScoring:
    def test_keeps_a_fuzzy_match_at_exactly_0_8_and_drops_one_below(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The query "q" is a prefix of neither name: tier 2, which needs 0.8 (contract §7).
        records = [TileRecord(place(i, i, [point(0, 0)])) for i in ("keep", "drop", "weak")]
        scores = {"keep": 0.8, "drop": 0.7999999, "weak": 0.6}
        monkeypatch.setattr("atgeo._rank.text_score", lambda _q, v: scores[v["name"]])
        assert [h.name for h in rank(records, REGION, "q", SortMode.FUSED)] == ["keep"]

    def test_keeps_a_word_match_at_any_score(self, monkeypatch: pytest.MonkeyPatch) -> None:
        records = [TileRecord(place(i, i, [point(0, 0)])) for i in ("qa", "qb", "zz")]
        scores = {"qa": 0.41, "qb": 0.0, "zz": 0.79}
        monkeypatch.setattr("atgeo._rank.text_score", lambda _q, v: scores[v["name"]])
        assert sorted(h.name or "" for h in rank(records, REGION, "q", SortMode.FUSED)) == ["qa", "qb"]

    def test_keeps_the_old_threshold_for_a_query_whose_words_all_fold_away(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        records = [TileRecord(place(i, i, [point(0, 0)])) for i in ("keep", "drop")]
        monkeypatch.setattr("atgeo._rank.text_score", lambda _q, v: 0.6 if v["name"] == "keep" else 0.5999999)
        assert [h.name for h in rank(records, REGION, "&", SortMode.FUSED)] == ["keep"]

    def test_scores_real_names_by_tier(self) -> None:
        # `in n out` against `In-N-Out Burger` scores about 0.70 but is a word match; `xqzjvkwpfhgy` scores 0.64
        # against `Qwizify` and is not kept.
        burger = TileRecord(place("burger", "In-N-Out Burger", [point(0, 0)]))
        qwizify = TileRecord(place("qwizify", "Qwizify", [point(0, 0)]))
        assert [h.name for h in rank([burger], REGION, "in n out", SortMode.FUSED)] == ["In-N-Out Burger"]
        assert rank([qwizify], REGION, "xqzjvkwpfhgy", SortMode.FUSED) == []

    def test_scores_only_the_first_twelve_query_words_one_at_a_time(self) -> None:
        words = [f"w{i}" for i in range(12)]
        name = " ".join(words)
        query = f"{name} zzzzzz"  # a thirteenth word that matches nothing
        expected = 0.5 * jaro_winkler(query, name) + 0.5 * 1.0  # every one of the twelve matches exactly
        assert text_score(query, {"name": name}) == expected

    def test_ties_scores_that_differ_past_the_third_decimal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        records = [
            TileRecord(place("a", "a", [point(0, 0)]), importance=1),
            TileRecord(place("b", "b", [point(0, 0)]), importance=9),
        ]
        monkeypatch.setattr("atgeo._rank.text_score", lambda _q, v: 0.9001 if v["name"] == "a" else 0.9004)
        assert [h.name for h in rank(records, REGION, "q", SortMode.FUSED)] == ["b", "a"]
        monkeypatch.setattr("atgeo._rank.text_score", lambda _q, v: 0.9014 if v["name"] == "a" else 0.9004)
        assert [h.name for h in rank(records, REGION, "q", SortMode.FUSED)] == ["a", "b"]


class TestSortKeys:
    def test_orders_distance_sort_by_importance_before_score(self, monkeypatch: pytest.MonkeyPatch) -> None:
        records = [
            TileRecord(place("a", "qa", [point(0, 0)]), importance=1),
            TileRecord(place("b", "qb", [point(0, 0)]), importance=9),
        ]
        monkeypatch.setattr("atgeo._rank.text_score", lambda _q, v: 0.9 if v["name"] == "qa" else 0.7)
        assert [h.name for h in rank(records, REGION, "q", SortMode.DISTANCE)] == ["qb", "qa"]

    def test_gives_a_place_with_a_point_no_size_even_when_it_has_a_box_too(self) -> None:
        # Both contain the centre (distance 0); the point-and-huge-box place is a point, so it is smaller.
        both = place("both", "Both", [bbox_location(80, -80, 170, -170), point(0, 0)])
        small = place("small", "Small", [bbox_location(0.1, -0.1, 0.1, -0.1)])
        names = [h.name for h in rank([TileRecord(small), TileRecord(both)], REGION, "", SortMode.DISTANCE)]
        assert names == ["Both", "Small"]

    def test_puts_a_null_origin_after_every_string(self) -> None:
        anon = place("x", "Same", [point(0, 0)], {"origin": None})
        records = [
            TileRecord(anon),
            TileRecord(place("b", "Same", [point(0, 0)])),
            TileRecord(place("a", "Same", [point(0, 0)])),
        ]
        assert [h.origin for h in rank(records, REGION, "", SortMode.FUSED)] == [
            place_origin("a"),
            place_origin("b"),
            None,
        ]

    def test_orders_origins_by_utf16_code_units(self) -> None:
        # Code-point order puts U+FF61 before U+10000; UTF-16 order puts it after.
        low, high = "urn:x:\uff61", "urn:x:\U00010000"
        records = [TileRecord(place("_", "Same", [point(0, 0)], {"origin": o})) for o in (low, high)]
        assert [h.origin for h in rank(records, REGION, "", SortMode.FUSED)] == [high, low]

    def test_sorts_a_nan_distance_last(self) -> None:
        # Out-of-range coordinates are kept as parsed (contract §5); these two make haversine NaN.
        nan_place = place("nan", "N", [point(95, 180)])
        near = place("near", "Near", [point(85, 1)])
        hits = rank([TileRecord(nan_place), TileRecord(near)], Bbox(-1, 85, 1, 85), "", SortMode.DISTANCE)
        assert [h.origin for h in hits] == [place_origin("near"), place_origin("nan")]
        assert math.isnan(hits[1].distance)

    def test_ties_negative_zero_with_zero_and_keeps_input_order(self) -> None:
        # Neither has an origin, so they tie on every key: input order stands.
        one = place("1", "One", [point(0, 0)], {"origin": None})
        two = place("2", "Two", [point(-0.0, -0.0)], {"origin": None})
        names = [h.name for h in rank([TileRecord(one), TileRecord(two)], REGION, "", SortMode.DISTANCE)]
        assert names == ["One", "Two"]

    def test_negates_huge_importances_without_overflow(self) -> None:
        recs = [TileRecord(place(i, "Same", [point(0, 0)]), importance=imp) for i, imp in (("a", -1), ("b", 2**62))]
        assert [h.importance for h in rank(recs, REGION, "", SortMode.FUSED)] == [2**62, -1]


class TestMatchTiers:
    def test_has_every_case(self) -> None:
        assert len(TIERS) == 36

    @pytest.mark.parametrize("c", TIERS, ids=[c["name"] for c in TIERS])
    def test_case(self, c: dict[str, Any]) -> None:
        # `place` is `{ name, variants? }`, with `variants` a list of `{ name }`: a place value as served.
        assert match_tier(normalize(c["query"]), c["place"]) == c["tier"]

    def test_has_every_lone_surrogate_case(self) -> None:
        assert len(LONE_SURROGATES) == 4

    @pytest.mark.parametrize("c", LONE_SURROGATES, ids=[c["name"] for c in LONE_SURROGATES])
    def test_lone_surrogate_case(self, c: dict[str, Any]) -> None:
        # Python strings hold lone surrogates, so this port runs the fixture (contract §7).
        assert match_tier(normalize(c["query"]), c["place"]) == c["tier"]

    def test_prefixes_by_utf16_code_units(self) -> None:
        # A query ending in a lone high surrogate prefixes the pair it starts; str.startswith says no.
        assert match_tier("a\ud83d", {"name": "a\U0001f600"}) == 1
        assert match_tier("a\ud83e", {"name": "a\U0001f600"}) == 2

    def test_needs_every_word_in_one_name(self) -> None:
        value = {"name": "Blue Bottle", "variants": [{"name": "Cafe"}]}
        assert match_tier("blue cafe", value) == 2
        assert match_tier("blue bot", value) == 1


class TestTiers:
    # "starbucks" scores well against both, but only one name has it word for word.
    EXACT = place("exact", "Starbucks Reserve", [point(0.5, 0.5)])
    FUZZY = place("fuzzy", "Starbux", [point(0, 0)])

    @pytest.mark.parametrize("sort", list(SortMode))
    def test_puts_tier_1_first_in_every_mode(self, sort: SortMode) -> None:
        records = [TileRecord(self.FUZZY, importance=9), TileRecord(self.EXACT)]
        assert [h.name for h in rank(records, REGION, "starbucks", sort)] == ["Starbucks Reserve", "Starbux"]

    def test_drops_tier_2_from_an_overview_answer_with_text(self) -> None:
        records = [TileRecord(self.FUZZY), TileRecord(self.EXACT)]
        assert [h.name for h in rank(records, REGION, "starbucks", SortMode.FUSED, overview=True)] == [
            "Starbucks Reserve"
        ]

    def test_drops_nothing_from_an_overview_answer_without_text(self) -> None:
        records = [TileRecord(self.FUZZY), TileRecord(self.EXACT)]
        assert len(rank(records, REGION, "", SortMode.FUSED, overview=True)) == 2


class TestRankingScenarios:
    @pytest.mark.parametrize("s", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
    def test_scenario(self, make: Make, s: dict[str, Any]) -> None:
        records = [record(r["value"], r["importance"]) for r in s["records"]]
        url = "https://places.atgeo.org/tiles/t0.json"
        # A scenario may say its getTiles answer is an overview, which exercises the fuzzy drop (contract §8).
        tiles_for = (lambda _b: {"tiles": [url], "overview": True}) if s.get("overview") else None
        fake = FakeServer(tilesets=[overture([FakeTile(url, records)])], tiles_for=tiles_for)
        # Each scenario names the dataset and collection it is a search of (contract §8).
        kwargs: dict[str, Any] = {"dataset": s["dataset"], "collection": s["collection"], "text": s.get("text")}
        if "sort" in s:
            kwargs["sort"] = s["sort"]
        places = make(fake).search_nearby(region_of(s["region"]), **kwargs).places
        origins = [p.origin for p in places]
        for top in s["expect"]["top"]:
            for origin in top["origins"]:
                assert origin in origins[: top["k"]], (top["k"], origin, origins)
        for origin in s["expect"].get("absent", []):
            assert origin not in origins
        if not s["expect"]["top"]:
            assert origins == []
