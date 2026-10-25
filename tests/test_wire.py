"""The lenient readers, the caches and the pure helpers, directly. No reference counterpart."""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable
from typing import Any

import pytest

from atgeo._cache import ParsedTiles, TileCache, TileEntry, TtlCache
from atgeo._locations import coordinate, locate
from atgeo._urls import is_http_url, prepare_url
from atgeo._wire import (
    TileList,
    TileRecord,
    error_from,
    importance_of,
    parse_body,
    read_place,
    read_service_description,
    read_tile_list,
    read_tile_payload,
    read_tileset,
    truthy,
)
from atgeo.errors import AtgeoDecodingError
from atgeo.types import (
    Attribution,
    CategoryCount,
    CollectionAttribution,
    CollectionInfo,
    CollectionRef,
    Extent,
    ServedTileset,
    ServiceDescription,
    Tileset,
)

from .helpers import fixture


def j(text: str) -> Any:
    return parse_body(text.encode())


class TestPrimitives:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("5", 5),
            ("-5", -5),
            ("1.9", 1),
            ("-1.9", -1),
            ("9223372036854774784", 9223372036854774784),  # the largest double below 2^63, exact
            ("9007199254740993", 9007199254740992),  # precision lost through the double, as in JavaScript
            ("9223372036854775807", None),  # rounds to 2^63
            ("-9223372036854775808", None),  # -2^63: the check is symmetric
            ("1e400", None),
            ("1" + "0" * 400, None),  # an integer literal past the largest double
            ('"5"', None),
            ("true", None),
            ("null", None),
            ("[5]", None),
            ("{}", None),
        ],
    )
    def test_importance_and_counts(self, text: str, expected: int | None) -> None:
        assert importance_of(j(text)) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("null", False), ("false", False), ("0", False), ("-0", False), ("0.0", False), ("-0.0", False),
            ('""', False), ("true", True), ("1", True), ("-1", True), ('"0"', True), ('"false"', True),
            ("[]", True), ("{}", True),
        ],
    )  # fmt: skip
    def test_overview_truthiness(self, text: str, expected: bool) -> None:
        assert truthy(j(text)) is expected

    def test_an_absent_overview_is_false(self) -> None:
        assert truthy() is False
        assert read_tile_list(j('{"tiles": []}')).overview is False


# fixtures/lenient-readers.json (contract §13): a body for each of the five readers and what a port's reader
# keeps of it, in JSON terms. The readers return dataclasses, and a field that is None is a missing key.
LENIENT: list[dict[str, Any]] = fixture("lenient-readers.json")
READERS: dict[str, Callable[[object], object]] = {
    "getTiles": read_tile_list,
    "tilePayload": read_tile_payload,
    "getPlace": read_place,
    "describeService": read_service_description,
    "describeTileset": read_tileset,
}


def camel(name: str) -> str:
    return re.sub(r"_([a-z])", lambda m: m.group(1).upper(), name)


def to_json(v: object) -> object:
    """What a reader returned, in the fixture's terms: camelCase keys, None as a missing key, lists for tuples."""
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return {
            camel(f.name): to_json(getattr(v, f.name)) for f in dataclasses.fields(v) if getattr(v, f.name) is not None
        }
    if isinstance(v, (list, tuple)):
        return [to_json(x) for x in v]
    return v


def same(a: object, b: object) -> bool:
    """Equal in JSON terms, strictly: True is not 1, 1 is not 1.0, and a missing key is not a null."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b, strict=True))
    return a == b


class TestLenientReaders:
    def test_has_a_case_for_every_reader(self) -> None:
        assert sorted({c["reader"] for c in LENIENT}) == sorted(READERS)

    @pytest.mark.parametrize("case", LENIENT, ids=[f"{c['reader']}: {c['name']}" for c in LENIENT])
    def test_reads_the_body_as_the_fixture_says(self, case: dict[str, Any]) -> None:
        read = READERS[case["reader"]]
        if case.get("decodingError"):
            with pytest.raises(AtgeoDecodingError):
                read(case["body"])
        else:
            got = to_json(read(case["body"]))
            assert same(got, case["result"]), (got, case["result"])

    def test_compares_strictly(self) -> None:
        assert same({"a": [1, "x"]}, {"a": [1, "x"]})
        assert not same(True, 1)
        assert not same(1, 1.0)
        assert not same({"a": None}, {})


class TestJson:
    """CONFORMANCE.md's JSON margins, and the places a port might get wrong where it agrees."""

    @pytest.mark.parametrize("text", ["NaN", "[Infinity]", '{"a": -Infinity}', "", "{", "{'a': 1}", "1 2"])
    def test_rejects_what_json_parse_rejects(self, text: str) -> None:
        with pytest.raises(ValueError, match=r"."):
            parse_body(text.encode())

    def test_drops_a_leading_bom_and_replaces_bad_utf8(self) -> None:
        assert parse_body(b'\xef\xbb\xbf{"a": "x\xffy"}') == {"a": "x�y"}

    def test_agrees_with_javascript_at_the_margins(self) -> None:
        assert parse_body(b"1e400") == float("inf")
        assert parse_body(b'{"a": 1, "a": 2}') == {"a": 2}
        assert parse_body(b'"\\ud800"') == "\ud800"

    def test_keeps_key_order_and_exact_integers(self) -> None:
        value: Any = parse_body(b'{"b": 1, "a": 123456789012345678901234567890}')
        assert list(value) == ["b", "a"]
        assert value["a"] == 123456789012345678901234567890

    def test_raises_recursion_error_or_value_error_past_the_limits(self) -> None:
        with pytest.raises(RecursionError):
            parse_body(b"[" * 200000 + b"]" * 200000)
        with pytest.raises(ValueError, match="digits"):
            parse_body(b"1" * 5000)


class TestErrorBodies:
    @pytest.mark.parametrize(
        ("body", "code", "message"),
        [
            ('{"error": "BboxTooLarge", "message": "too big"}', "BboxTooLarge", "too big"),
            ('{"error": "PlaceNotFound"}', "PlaceNotFound", "HTTP 404"),
            ('{"message": "m"}', "HttpError", "m"),
            ('{"error": 7, "message": {}}', "HttpError", "HTTP 404"),
            ("null", "HttpError", "HTTP 404"),
            ("[1]", "HttpError", "HTTP 404"),
            ("", "HttpError", "HTTP 404"),
            ("<html>", "HttpError", "HTTP 404"),
        ],
    )
    def test_judges_each_field_on_its_own(self, body: str, code: str, message: str) -> None:
        e = error_from(404, body.encode())
        assert (e.code, e.message, e.status) == (code, message, 404)

    def test_pickles_and_prints(self) -> None:
        import pickle  # noqa: PLC0415

        e = pickle.loads(pickle.dumps(error_from(400, b'{"error": "X", "message": "m"}')))
        assert (e.code, e.message, e.status, str(e)) == ("X", "m", 400, "m (X)")


class TestTilePayloads:
    def test_keeps_value_uri_and_importance_and_nothing_else(self) -> None:
        rec = '{"value": {"b": 1, "a": {"z": [2.50]}}, "uri": "u", "importance": 3, "x": 4}'
        records = read_tile_payload(j('{"publishedAt": "x", "records": [' + rec + "]}")).records
        assert records == [TileRecord({"b": 1, "a": {"z": [2.5]}}, "u", 3)]
        assert list(records[0].value) == ["b", "a"]

    def test_skips_records_that_are_not_objects_or_have_no_object_value_and_keeps_the_tile(self) -> None:
        payload = read_tile_payload(
            j('{"records": [7, "s", [1], null, {"value": null}, {}, {"value": 5}, {"value": {"name": "ok"}}]}')
        )
        assert [r.value["name"] for r in payload.records] == ["ok"]

    def test_reads_a_wrong_typed_uri_or_importance_as_absent(self) -> None:
        payload = read_tile_payload(
            j('{"records": [{"value": {}, "uri": {}, "importance": "7"}, {"value": {}, "uri": 9, "importance": [1]}]}')
        )
        assert payload.records == [TileRecord({}), TileRecord({})]

    def test_reads_the_attribution_beside_the_records_and_none_when_it_is_not_an_array(self) -> None:
        entry = '{"dataset": "d", "collection": "c", "licenses": ["l", 1], "release": 5}'
        payload = read_tile_payload(j('{"records": [], "attribution": [' + entry + ', {"dataset": "d"}, null]}'))
        assert payload.attribution == (Attribution(dataset="d", collection="c", licenses=("l",)),)
        assert read_tile_payload(j('{"records": [], "attribution": []}')).attribution == ()
        for text in ('{"records": []}', '{"records": [], "attribution": {}}', '{"records": [], "attribution": null}'):
            assert read_tile_payload(j(text)).attribution is None

    @pytest.mark.parametrize("text", ["{}", "[]", "null", "5", '{"records": {}}', '{"records": "x"}'])
    def test_raises_without_a_records_list(self, text: str) -> None:
        with pytest.raises(AtgeoDecodingError):
            read_tile_payload(j(text))


class TestGetTiles:
    def test_skips_non_string_entries(self) -> None:
        assert read_tile_list(j('{"tiles": ["a", 1, null, {}, "b"]}')) == TileList(["a", "b"], False)

    @pytest.mark.parametrize("text", ["{}", "[]", "null", '{"tiles": "x"}', '{"tiles": null}'])
    def test_raises_without_a_tiles_list(self, text: str) -> None:
        with pytest.raises(AtgeoDecodingError):
            read_tile_list(j(text))


class TestGetPlace:
    def test_reads_the_value_importance_and_attribution_not_the_uri_or_url(self) -> None:
        body = read_place(j('{"uri": 5, "url": 6, "value": {"name": "x"}, "importance": 2.7, "attribution": []}'))
        assert (body.value, body.importance, body.attribution) == ({"name": "x"}, 2, ())

    def test_reads_the_attribution_as_served_unsorted_duplicates_included(self) -> None:
        entries = ", ".join(f'{{"dataset": "{d}", "collection": "c"}}' for d in "zaz")
        body = read_place(j('{"value": {}, "attribution": [' + entries + "]}"))
        assert [e.dataset for e in body.attribution] == ["z", "a", "z"]

    @pytest.mark.parametrize("text", ["{}", "null", "[]", '{"value": null}', '{"value": [1]}', '{"value": "x"}'])
    def test_raises_without_an_object_value(self, text: str) -> None:
        with pytest.raises(AtgeoDecodingError):
            read_place(j(text))

    def test_raises_for_the_earlier_shape_with_a_record_and_no_value(self) -> None:
        with pytest.raises(AtgeoDecodingError):
            read_place(j('{"uri": "x", "record": {"name": "X"}}'))

    @pytest.mark.parametrize("text", ["{}", "null", "[]", '{"record": null}', '{"record": [1]}', '{"record": "x"}'])
    def test_does_not_fall_back_to_a_record(self, text: str) -> None:
        with pytest.raises(AtgeoDecodingError):
            read_place(j(text))


class TestDescribe:
    def test_maps_a_complete_describe_service(self) -> None:
        body = j(
            '{"did": "did:web:x", "tilesets": [{"slug": "s", "collections": [{"dataset": "d", "collection": "c"}]}]}'
        )
        expected = ServiceDescription(
            did="did:web:x",
            tilesets=(ServedTileset(slug="s", collections=(CollectionRef(dataset="d", collection="c"),)),),
        )
        assert read_service_description(body) == expected

    def test_reads_missing_and_wrong_typed_fields_as_absent_and_required_lists_as_empty(self) -> None:
        assert read_service_description(j('{"did": 7, "tilesets": "x"}')) == ServiceDescription()
        body = j('{"tilesets": [{}, 5, null, {"slug": "s", "collections": {}}]}')
        assert read_service_description(body) == ServiceDescription(tilesets=(ServedTileset(), ServedTileset(slug="s")))

    def test_maps_a_describe_tileset_leniently(self) -> None:
        body = j(
            """
            {"slug": "s", "publishedAt": "2026-01-01T00:00:00Z",
             "extent": {"north": "1", "south": 2, "east": "3"},
             "collections": [{"dataset": "d", "collection": "c", "recordCount": 12.9, "licenses": ["a", 1, "b"],
                              "locationFormats": "x", "categories": [{"value": "v", "count": "3"}, 4, {}],
                              "attributes": []}, "junk"],
             "references": [{"dataset": "r", "licenses": null}]}
            """
        )
        assert read_tileset(body) == Tileset(
            slug="s",
            published_at="2026-01-01T00:00:00Z",
            extent=Extent(north="1", east="3"),
            collections=(
                CollectionInfo(
                    dataset="d",
                    collection="c",
                    record_count=12,
                    licenses=("a", "b"),
                    categories=(CategoryCount(value="v"), CategoryCount()),
                    attributes=(),
                ),
            ),
            references=(CollectionAttribution(dataset="r"),),
        )

    def test_reads_an_extent_that_is_not_an_object_as_absent_and_a_missing_collections_list_as_empty(self) -> None:
        assert read_tileset(j('{"extent": [1], "collections": null}')) == Tileset()

    @pytest.mark.parametrize("text", ["null", "[]", "5", '"x"', "false"])
    def test_raises_for_a_body_that_is_not_an_object(self, text: str) -> None:
        with pytest.raises(AtgeoDecodingError):
            read_service_description(j(text))
        with pytest.raises(AtgeoDecodingError):
            read_tileset(j(text))


class TestCoordinates:
    """CONFORMANCE.md's number table, read through locate()."""

    @staticmethod
    def latitude_of(raw: object) -> float | None:
        loc = {"$type": "community.lexicon.location.geo", "latitude": raw, "longitude": "0"}
        p = locate({"locations": [loc]}).point
        return p[0] if p else None

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (" 1.5", None), ("1.5 ", None), ("1.5abc", None), ("1e", None), ("1_000", None), ("0x10", None),
            ("١٢", None), ("inf", None), ("Infinity", None), ("NaN", None), ("", None), ("1.5f", None),
            ("+1.5", 1.5), (".5", 0.5), ("1.", 1.0), ("-0", -0.0), ("1e3", 1000.0), ("1E-2", 0.01),
        ],
    )  # fmt: skip
    def test_string_forms(self, text: str, expected: float | None) -> None:
        assert self.latitude_of(text) == expected

    def test_a_json_number_works_and_other_types_do_not(self) -> None:
        assert self.latitude_of(37.5) == 37.5
        assert self.latitude_of(37) == 37.0
        raw: object
        for raw in ([0.5], True, None, {}, 10**400):
            assert self.latitude_of(raw) is None
        assert coordinate(float("inf")) is None


def entry(*keys: str, size: int = 1) -> TileEntry:
    records = [{"value": {"origin": k}} for k in keys]
    body = json.dumps({"records": records}).encode()
    body += b" " * max(0, size - len(body))
    return TileEntry(body, frozenset(keys))


class TestTileCache:
    def test_reads_mark_a_tile_most_recently_used(self) -> None:
        cache = TileCache(10_000)
        for url in "abc":
            cache.set(url, entry())
        cache.get("a")
        assert cache.urls() == ["b", "c", "a"]

    def test_one_insert_evicts_several_least_recently_used(self) -> None:
        cache = TileCache(400)
        for url in "abc":
            cache.set(url, entry(size=100))
        cache.get("a")
        cache.set("d", entry(size=250))  # 550: b and c go, and the recently read a stays
        assert (cache.urls(), cache.total) == (["a", "d"], 350)

    def test_a_tile_larger_than_the_budget_evicts_every_other_tile_and_then_itself(self) -> None:
        cache = TileCache(200)
        cache.set("a", entry(size=100))
        cache.set("big", entry(size=201))
        assert (cache.urls(), cache.total) == ([], 0)

    def test_keeps_a_total_of_exactly_the_budget(self) -> None:
        cache = TileCache(300)
        for url in "abc":
            cache.set(url, entry(size=100))
        assert (cache.urls(), cache.total) == (["a", "b", "c"], 300)

    def test_replacing_a_url_subtracts_the_old_size(self) -> None:
        cache = TileCache(1000)
        cache.set("a", entry(size=600))
        cache.set("a", entry(size=700))
        assert (cache.urls(), cache.total) == (["a"], 700)

    def test_find_takes_the_lowest_url_by_code_units_and_leaves_recency_alone(self) -> None:
        cache = TileCache(10_000)
        low, high = "https://t/\U00010000", "https://t/｡"  # code-point order says the reverse
        cache.set(high, entry("k"))
        cache.set(low, entry("k"))
        cache.set("https://t/other", entry("j"))
        before = cache.urls()
        found = cache.find("k")
        assert found is not None
        assert found[0] == low
        assert cache.urls() == before
        assert cache.find("missing") is None


class TestParsedTiles:
    def test_serves_a_memo_only_while_the_cache_holds_the_same_entry(self) -> None:
        memo = ParsedTiles()
        e1, e2 = entry("k"), entry("k")
        records = e1.records()
        memo.replace({"a": (e1, records)})
        assert memo.get("a", e1) is records
        assert memo.get("a", e2) is None
        assert memo.get("b", e1) is None


class TestTtlCache:
    def test_is_fresh_until_exactly_an_hour_after_set(self) -> None:
        now = [1000.0]
        cache: TtlCache[str] = TtlCache(lambda: now[0])
        cache.set("k", "v")
        now[0] = 1000 + 3599.999
        assert cache.get("k") == "v"
        now[0] = 1000 + 3600
        assert cache.get("k") is None

    def test_prunes_expired_entries_on_set(self) -> None:
        now = [0.0]
        cache: TtlCache[int] = TtlCache(lambda: now[0])
        for i in range(5):
            cache.set(str(i), i)
        now[0] = 3600
        cache.set("new", 9)
        assert len(cache) == 1


class TestPrepareUrl:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://h/t/a.json", "https://h/t/a.json"),
            (" https://h/t/a.json \n", "https://h/t/a.json"),  # WHATWG trims C0 controls and spaces
            ("https://h/t/a b\tc.json", "https://h/t/a%20bc.json"),
            ("https://h/café.json", "https://h/caf%C3%A9.json"),
            ("https://h/%2c%7E", "https://h/%2c%7E"),
        ],
    )
    def test_prepares_as_whatwg_does_for_these_characters(self, url: str, expected: str) -> None:
        assert prepare_url(url) == expected

    @pytest.mark.parametrize("url", ["https://u:p@h/x", "https://u@h/x", "file:///x", "h/x", "https://:0/"])
    def test_refuses_what_is_not_an_http_url_without_credentials(self, url: str) -> None:
        assert is_http_url(url) is False
