import asyncio
import httpx
import pytest
from pytest_bdd import given, scenarios, then, when

from paper_fixtures import NOW, book_payload, contract, row
from sgr.connectors.polymarket import BudgetExhausted, PolymarketConnector, RequestBudget
from sgr.connectors.polymarket_feed import BookFeed
from sgr.research.contracts import match_contract
from sgr.research.storage import ResearchStore

pytestmark = pytest.mark.bdd
scenarios("../features/polymarket_connector.feature")


@given("a public Polymarket fixture", target_fixture="public")
def public_fixture(tmp_path):
    return {"store": ResearchStore(tmp_path), "requests": []}


@when("keyset discovery resumes and a page is retried")
def discover(public):
    async def run():
        def provider(request):
            public["requests"].append(request)
            if len(public["requests"]) == 1:
                return httpx.Response(503)
            cursor = request.url.params.get("after_cursor")
            return httpx.Response(200, json={"markets": [row(1), row(1)] if cursor is None else [row(2)], "next_cursor": "p2" if cursor is None else None})
        async with PolymarketConnector(public["store"], interval=0, transport=httpx.MockTransport(provider), clock=lambda: NOW) as connector:
            public["first"] = await connector.page()
            public["second"] = await connector.page(public["first"].next_cursor)
            public["cached"] = await connector.page()
            public["budget"] = connector.budget
    asyncio.run(run())


@then("public identities and raw snapshots are preserved")
def preserved(public):
    assert public["first"].exclusions == ("duplicate_condition",)
    assert public["first"] == public["cached"]
    assert public["second"].markets[0].condition_id != public["first"].markets[0].condition_id
    assert len(public["store"].load_all("polymarket_market")) == 2
    assert public["budget"].used == 3 and public["budget"].retries == 1
    assert public["requests"][-1].url.params["after_cursor"] == "p2"
    assert (public["store"].root / public["first"].source.path).exists()


@when("the provider fails repeatedly")
def fails(public):
    async def run():
        budget = RequestBudget(maximum=2)
        async with PolymarketConnector(public["store"], interval=0, budget=budget, transport=httpx.MockTransport(lambda r: httpx.Response(503))) as connector:
            try:
                await connector.page()
            except BudgetExhausted as error:
                public["error"] = error
            public["budget"] = budget
    asyncio.run(run())


@then("retries stop at the campaign request budget")
def bounded(public):
    assert isinstance(public["error"], BudgetExhausted)
    assert public["budget"].used == 2
    assert not public["store"].database_path.exists()


@when("settlement and outcome definitions are checked")
def definitions(public):
    market, game, definition = contract()
    public["matches"] = [
        match_contract(market, [game], [definition], decision_at=NOW),
        match_contract(market, [game], [], decision_at=NOW),
        match_contract(market.model_copy(update={"market_type": "spread"}), [game], [definition], decision_at=NOW),
        match_contract(market.model_copy(update={"asset_ids": market.asset_ids[::-1]}), [game], [definition], decision_at=NOW),
    ]


@then("only an exact supported outcome can be priced")
def exact(public):
    assert [x.eligible for x in public["matches"]] == [True, False, False, False]
    assert public["matches"][1].reason == "ambiguous_or_unaudited_settlement_rules"
    assert public["matches"][2].reason == "unsupported_outcome"
    assert all(x.definition is None for x in public["matches"][1:])


@when("a selected asset feed disconnects")
def disconnect(public):
    async def run():
        def provider(request):
            return httpx.Response(200, json={"/book": book_payload(), "/fee-rate": {"base_fee": 500}, "/tick-size": {"minimum_tick_size": .01}}[request.url.path])
        async with PolymarketConnector(public["store"], interval=0, transport=httpx.MockTransport(provider), clock=lambda: NOW) as connector:
            book = await connector.book(contract()[0], "101")
        public["feed"] = BookFeed([book])
        public["message"] = {"event_type": "book", **book_payload()}
        public["feed"].ingest(public["message"], received_at=NOW)
        public["feed"].ingest({"event_type": "best_bid_ask", "asset_id": "101"}, received_at=NOW)
        public["before"] = public["feed"].coherent("101", NOW)
        public["feed"].gap()
    asyncio.run(run())


@then("coherent book publication stops until resynchronization")
def stopped(public):
    assert public["before"]
    with pytest.raises(ValueError, match="Feed gap"):
        public["feed"].persist(public["store"], "101", now=NOW)
    public["feed"].ingest(public["message"], received_at=NOW)
    assert public["feed"].coherent("101", NOW)
    assert public["feed"].persist(public["store"], "101", now=NOW).feed_ok
