"""autocomplete.test.ts, ported: AsyncAtgeoClient.autocomplete_by_keyword (contract §19) and the caller's own
cancellation (§11). The blocking client has no autocompleter: it cannot cancel a call in flight."""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from atgeo import (
    AsyncAtgeoClient,
    AtgeoClient,
    AtgeoError,
    Bbox,
    Circle,
    Disclosure,
    KeywordAutocomplete,
    KeywordSearchResult,
)
from atgeo.transport import HttpResponse

from .fake_server import DEFAULT_SERVER, FakeServer, FakeTile, overture
from .helpers import OVERTURE, place, place_origin, point, record

OV: Any = OVERTURE
TILES = [FakeTile("https://tiles.example/p.json", [record(place("zc", "Zanzibar Cafe", [point(37.775, -122.418)]), 5)])]
NOTHING = KeywordSearchResult(places=[], attribution=(), disclosed=Disclosure(keyword=False, region=None))
SEARCH = f"{DEFAULT_SERVER}/xrpc/org.atgeo.searchPlaces"
REGION = Bbox(-122.42, 37.77, -122.41, 37.78)


def setup() -> tuple[FakeServer, AsyncAtgeoClient]:
    fake = FakeServer(tilesets=[overture(TILES)])
    return fake, AsyncAtgeoClient(transport=fake.async_transport)


def params_of(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query, keep_blank_values=True))


class Hanging:
    """An async transport whose searchPlaces requests wait until they are cancelled or released, so that a call
    stays in flight; everything else goes to the fake. `started` counts the requests that reached it."""

    def __init__(self, fake: FakeServer, *, release_first: bool = False) -> None:
        self.fake = fake
        self.started: list[str] = []
        self.cancelled = 0
        self.release_first = release_first
        self.release: asyncio.Event | None = None

    async def get(self, url: str) -> HttpResponse:
        if not url.startswith(SEARCH):
            return await self.fake.async_transport.get(url)
        self.started.append(url)
        if self.release_first and len(self.started) > 1:
            return await self.fake.async_transport.get(url)
        if self.release is None:
            self.release = asyncio.Event()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return await self.fake.async_transport.get(url)


def run(main: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    return asyncio.run(main())


class TestAutocomplete:
    # A one-character prefix, a Hangul syllable (one character, though NFKD makes it two), and a consonant with
    # a spacing vowel sign (one character once the sign is stripped).
    @pytest.mark.parametrize("prefix", ["a", "하", "का", "", "  ", "a b", "a."])
    def test_makes_no_request_for_a_keyword_that_cannot_qualify_and_resolves_an_empty_result(self, prefix: str) -> None:
        fake, client = setup()
        result = run(lambda: client.autocomplete_by_keyword(**OV).search_by_keyword(prefix))
        assert result == NOTHING
        assert fake.requests == []

    def test_sends_a_prefix_that_qualifies_as_typed_and_returns_the_keyword_hits(self) -> None:
        fake, client = setup()
        result = run(lambda: client.autocomplete_by_keyword(**OV).search_by_keyword("Zan"))
        assert [params_of(r) for r in fake.requests] == [{**OV, "q": "Zan"}]
        assert [h.origin for h in result.places] == [place_origin("zc")]
        assert result.disclosed == Disclosure(keyword=True, region=None)

    def test_sends_exactly_what_search_by_keyword_sends_for_every_string_prefixes_included(self) -> None:
        fake, client = setup()
        direct = FakeServer(tilesets=[overture(TILES)])

        async def main() -> None:
            typing = client.autocomplete_by_keyword(**OV, region=REGION)
            other = AsyncAtgeoClient(transport=direct.async_transport)
            for prefix in ("Za", "Zan", "Zanz", "  Zanzibar Caf", "Zanzibar Cafe"):
                await typing.search_by_keyword(prefix)
                await other.search_by_keyword(prefix, **OV, region=REGION)

        run(main)
        assert fake.requests == direct.requests
        assert len(fake.requests) == 5

    def test_carries_the_region_snapped_to_whole_degrees(self) -> None:
        fake, client = setup()
        result = run(lambda: client.autocomplete_by_keyword(**OV, region=REGION).search_by_keyword("Zan"))
        assert params_of(fake.requests[0])["bbox"] == "-123,37,-122,38"
        assert result.disclosed == Disclosure(keyword=True, region=1.0)

    def test_supersedes_the_previous_call_on_the_same_autocompleter_which_raises_cancelled_error(self) -> None:
        fake = FakeServer(tilesets=[overture(TILES)])
        transport = Hanging(fake, release_first=False)
        client = AsyncAtgeoClient(transport=transport)

        async def main() -> tuple[KeywordSearchResult, bool]:
            typing = client.autocomplete_by_keyword(**OV)
            first = asyncio.ensure_future(typing.search_by_keyword("Za"))
            await asyncio.sleep(0.01)
            assert transport.started  # the first call is in flight
            this = asyncio.current_task()
            assert this is not None
            transport.release_first = True
            second = await typing.search_by_keyword("Zan")
            with pytest.raises(asyncio.CancelledError):
                await first
            assert first.cancelled()
            return second, bool(this.cancelling())

        second, caller_cancelled = run(main)
        assert len(second.places) == 1
        assert not caller_cancelled  # a superseded call cancels nobody but itself
        assert transport.cancelled == 1  # the request in flight was cancelled, not left running

    def test_a_superseded_callers_task_is_not_cancelled(self) -> None:
        fake = FakeServer(tilesets=[overture(TILES)])
        transport = Hanging(fake)
        client = AsyncAtgeoClient(transport=transport)

        async def main() -> list[int]:
            typing = client.autocomplete_by_keyword(**OV)
            seen = []

            async def caller() -> None:
                try:
                    await typing.search_by_keyword("Za")
                except asyncio.CancelledError:
                    task = asyncio.current_task()
                    assert task is not None
                    seen.append(task.cancelling())  # 0: superseded, not cancelled; the caller may ignore it

            waiting = asyncio.ensure_future(caller())
            await asyncio.sleep(0.01)
            transport.release_first = True
            await typing.search_by_keyword("Zan")
            await waiting
            return seen

        assert run(main) == [0]

    def test_does_not_supersede_a_call_on_another_autocompleter_of_the_same_client(self) -> None:
        fake = FakeServer(tilesets=[overture(TILES)], delays={})
        transport = Hanging(fake, release_first=True)
        client = AsyncAtgeoClient(transport=transport)

        async def main() -> KeywordSearchResult:
            first = asyncio.ensure_future(client.autocomplete_by_keyword(**OV).search_by_keyword("Za"))
            await asyncio.sleep(0.01)
            await client.autocomplete_by_keyword(**OV).search_by_keyword("Zan")
            assert transport.release is not None
            transport.release.set()
            return await first

        assert run(main).disclosed.keyword

    def test_a_call_that_has_finished_is_not_disturbed_by_the_next(self) -> None:
        _, client = setup()

        async def main() -> list[KeywordSearchResult]:
            typing = client.autocomplete_by_keyword(**OV)
            return [await typing.search_by_keyword("Zan"), await typing.search_by_keyword("Zanz")]

        assert [len(r.places) for r in run(main)] == [1, 1]

    def test_a_keyword_that_cannot_qualify_still_supersedes_the_previous_call(self) -> None:
        fake = FakeServer(tilesets=[overture(TILES)])
        transport = Hanging(fake)
        client = AsyncAtgeoClient(transport=transport)

        async def main() -> KeywordSearchResult:
            typing = client.autocomplete_by_keyword(**OV)
            first = asyncio.ensure_future(typing.search_by_keyword("Za"))
            await asyncio.sleep(0.01)
            skipped = await typing.search_by_keyword("Z")
            with pytest.raises(asyncio.CancelledError):
                await first
            return skipped

        assert run(main) == NOTHING
        assert transport.cancelled == 1


class TestAutocompleteArguments:
    def test_is_a_method_of_the_async_client_only(self) -> None:
        assert hasattr(AsyncAtgeoClient, "autocomplete_by_keyword")
        assert not hasattr(AtgeoClient, "autocomplete_by_keyword")

    def test_returns_a_keyword_autocomplete(self) -> None:
        _, client = setup()
        assert isinstance(client.autocomplete_by_keyword(**OV), KeywordAutocomplete)

    def test_refuses_a_dataset_that_is_not_a_string_at_construction(self) -> None:
        _, client = setup()
        with pytest.raises(TypeError, match="dataset"):
            client.autocomplete_by_keyword(dataset=None)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            client.autocomplete_by_keyword()  # type: ignore[call-arg]

    def test_refuses_a_circle_region_at_construction(self) -> None:
        _, client = setup()
        with pytest.raises(TypeError, match="Bbox"):
            client.autocomplete_by_keyword(**OV, region=Circle(0, 0, 100))  # type: ignore[arg-type]

    def test_refuses_an_invalid_bbox_at_construction(self) -> None:
        _, client = setup()
        with pytest.raises(ValueError, match="invalid bbox"):
            client.autocomplete_by_keyword(**OV, region=Bbox(0, 10, 1, 5))

    def test_refuses_a_keyword_that_is_not_a_string_with_no_request(self) -> None:
        fake, client = setup()

        async def main() -> None:
            typing = client.autocomplete_by_keyword(**OV)
            for bad in (5, None, b"ab"):
                with pytest.raises(TypeError, match="keyword"):
                    await typing.search_by_keyword(bad)  # type: ignore[arg-type]

        run(main)
        assert fake.requests == []

    def test_supersedes_the_previous_call_before_it_refuses_a_keyword_that_is_not_a_string(self) -> None:
        fake = FakeServer(tilesets=[overture(TILES)])
        transport = Hanging(fake)
        client = AsyncAtgeoClient(transport=transport)

        async def main() -> None:
            typing = client.autocomplete_by_keyword(**OV)
            first = asyncio.ensure_future(typing.search_by_keyword("Za"))
            await asyncio.sleep(0.01)
            with pytest.raises(TypeError):
                await typing.search_by_keyword(5)  # type: ignore[arg-type]
            with pytest.raises(asyncio.CancelledError):
                await first

        run(main)

    def test_documents_what_it_sends_neutrally(self) -> None:
        doc = KeywordAutocomplete.search_by_keyword.__doc__ or ""
        for phrase in ("as typed", "prefixes", "keeps no log", "hostile operator", "Debouncing", "app's choice"):
            assert phrase in doc


class TestServerRefusesAKeywordTheSdkJudgedQualifying:
    """Contract §19, step 4: `ﬁ` is two characters to the SDK (NFKD) and one to the server, which answers
    KeywordRequired. That resolves empty, as a keyword the SDK skips does, except that this one went out."""

    def test_resolves_empty_disclosing_the_keyword_it_sent(self) -> None:
        fake, client = setup()
        result = run(lambda: client.autocomplete_by_keyword(**OV).search_by_keyword("ﬁ"))
        assert len(fake.requests) == 1
        assert result == KeywordSearchResult(places=[], attribution=(), disclosed=Disclosure(keyword=True, region=None))

    def test_reports_the_region_it_sent_with_the_refused_keyword(self) -> None:
        _, client = setup()
        result = run(lambda: client.autocomplete_by_keyword(**OV, region=REGION).search_by_keyword("ﬁ"))
        assert result.disclosed == Disclosure(keyword=True, region=1.0)

    def test_still_raises_for_any_other_refusal(self) -> None:
        fake, client = setup()
        fake.answer = lambda url: (
            HttpResponse(400, json.dumps({"error": "InvalidRequest", "message": "x"}).encode())
            if url.startswith(SEARCH)
            else None
        )
        with pytest.raises(AtgeoError) as raised:
            run(lambda: client.autocomplete_by_keyword(**OV).search_by_keyword("Zan"))
        assert raised.value.code == "InvalidRequest"


class TestCallersOwnCancellation:
    def test_an_already_cancelled_caller_sends_nothing_and_gets_cancelled_error(self) -> None:
        fake, client = setup()

        async def main() -> None:
            asyncio.current_task().cancel()  # type: ignore[union-attr]
            await client.autocomplete_by_keyword(**OV).search_by_keyword("Zan")

        with pytest.raises(asyncio.CancelledError):
            run(main)
        assert fake.requests == []

    def test_cancelling_the_caller_mid_flight_cancels_the_request(self) -> None:
        fake = FakeServer(tilesets=[overture(TILES)])
        transport = Hanging(fake)
        client = AsyncAtgeoClient(transport=transport)

        async def main() -> bool:
            call = asyncio.ensure_future(client.autocomplete_by_keyword(**OV).search_by_keyword("Zan"))
            await asyncio.sleep(0.01)
            assert len(transport.started) == 1
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            return call.cancelled()

        assert run(main)
        assert transport.cancelled == 1

    def test_a_cancel_after_supersession_changes_nothing(self) -> None:
        fake = FakeServer(tilesets=[overture(TILES)])
        transport = Hanging(fake, release_first=False)
        client = AsyncAtgeoClient(transport=transport)

        async def main() -> None:
            typing = client.autocomplete_by_keyword(**OV)
            first = asyncio.ensure_future(typing.search_by_keyword("Za"))
            await asyncio.sleep(0.01)
            transport.release_first = True
            await typing.search_by_keyword("Zan")
            assert first.done()  # superseded: already ended with the cancellation outcome
            first.cancel()  # a later abort by the caller is a no-op
            with pytest.raises(asyncio.CancelledError):
                await first

        run(main)
        assert transport.cancelled == 1

    def test_a_task_awaiting_the_call_is_not_marked_cancelled_by_supersession(self) -> None:
        # The contract's hazard in one line: task.cancelling() is the only way to tell the two apart.
        fake = FakeServer(tilesets=[overture(TILES)])
        transport = Hanging(fake)
        client = AsyncAtgeoClient(transport=transport)

        async def main() -> int:
            typing = client.autocomplete_by_keyword(**OV)

            async def caller() -> int:
                try:
                    await typing.search_by_keyword("Za")
                except asyncio.CancelledError:
                    return asyncio.current_task().cancelling()  # type: ignore[union-attr]
                return -1

            waiting = asyncio.ensure_future(caller())
            await asyncio.sleep(0.01)
            transport.release_first = True
            await typing.search_by_keyword("Zan")
            return await waiting

        assert run(main) == 0
