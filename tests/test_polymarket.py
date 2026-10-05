import asyncio
from datetime import timedelta

import httpx
import pytest

from paper_fixtures import NOW, book_payload, contract, row
from sgr.connectors.polymarket import BudgetExhausted, PolymarketConnector, RequestBudget, SchemaDrift
from sgr.connectors.polymarket_feed import BookFeed
from sgr.research.contracts import match_contract
from sgr.research.storage import ResearchStore


def test_pagination_identity_and_budget(tmp_path):
    async def run():
        requests = []
        def provider(request):
            requests.append(request)
            cursor = request.url.params.get("after_cursor")
            return httpx.Response(200, json={"markets": [row(1), row(1)] if cursor is None else [row(2)], "next_cursor": "page2" if cursor is None else None})
        store = ResearchStore(tmp_path)
        async with PolymarketConnector(store, interval=0, transport=httpx.MockTransport(provider), clock=lambda: NOW) as connector:
            first = await connector.page()
            second = await connector.page(first.next_cursor)
            await connector.page()
            assert first.exclusions == ("duplicate_condition",)
            assert first.markets[0].condition_id != second.markets[0].condition_id
            assert len(store.load_all("polymarket_market")) == 2
            assert connector.budget.used == 2 and connector.budget.cache_hits == 1
            assert "offset" not in requests[1].url.params and requests[1].url.params["after_cursor"] == "page2"
    asyncio.run(run())


def test_provider_retries_are_bounded(tmp_path):
    async def run():
        budget = RequestBudget(maximum=2)
        async with PolymarketConnector(ResearchStore(tmp_path), interval=0, budget=budget, transport=httpx.MockTransport(lambda r: httpx.Response(503)), clock=lambda: NOW) as connector:
            with pytest.raises(BudgetExhausted):
                await connector.page()
            assert budget.used == 2
    asyncio.run(run())


def test_ambiguous_and_unsupported_contracts_stay_unknown():
    market, game, definition = contract()
    assert match_contract(market, [game], [definition], decision_at=NOW).eligible
    assert not match_contract(market, [game], [], decision_at=NOW).eligible
    changed = market.model_copy(update={"rule_version": "1"*64})
    assert match_contract(changed, [game], [definition], decision_at=NOW).reason == "contract_definition_changed"
    unsupported = market.model_copy(update={"market_type": "spread"})
    assert match_contract(unsupported, [game], [definition], decision_at=NOW).reason == "unsupported_outcome"
    from sgr.models import NFLSeasonType
    regular = game.model_copy(update={"season_type": NFLSeasonType.REGULAR})
    assert match_contract(market, [regular], [definition], decision_at=NOW).reason == "regular_season_tie_probability_unknown"
    future = game.model_copy(update={"retrieved_at": NOW + timedelta(seconds=1)})
    assert not match_contract(market, [future], [definition], decision_at=NOW).eligible


def test_book_identity_fees_ticks_and_feed_gap(tmp_path):
    async def run():
        market, _, _ = contract()
        def provider(request):
            data = {"/book": book_payload(), "/fee-rate": {"base_fee": 500}, "/tick-size": {"minimum_tick_size": .01}}
            return httpx.Response(200, json=data[request.url.path])
        store = ResearchStore(tmp_path)
        async with PolymarketConnector(store, interval=0, transport=httpx.MockTransport(provider), clock=lambda: NOW) as connector:
            book = await connector.book(market, "101")
            assert book.fee_rate_bps == 500 and len(book.source_snapshots) == 3
            feed = BookFeed([book])
            message = {"event_type": "book", **book_payload()}
            feed.ingest(message, received_at=NOW)
            assert feed.coherent("101", NOW)
            feed.persist(store, "101", now=NOW)
            feed.gap()
            with pytest.raises(ValueError, match="Feed gap"):
                feed.persist(store, "101", now=NOW)
            feed.ingest(message, received_at=NOW)
            assert not feed.coherent("101", NOW + timedelta(seconds=20))
            with pytest.raises(ValueError):
                await connector._get("https://clob.polymarket.com", "/orders", {})
    asyncio.run(run())


def test_nonadvancing_cursor_is_not_partial_success(tmp_path):
    async def run():
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"markets": [], "next_cursor": "same"}))
        async with PolymarketConnector(ResearchStore(tmp_path), transport=transport, interval=0) as connector:
            with pytest.raises(SchemaDrift):
                await connector.page("same")
    asyncio.run(run())
