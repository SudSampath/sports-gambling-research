"""Shared synthetic scenario setup; assertions live in Then/tests."""
from datetime import timedelta
from decimal import Decimal

from paper_fixtures import NOW, contract, game, source
from sgr.models import NFLSeasonType
from sgr.paper.ledger import Ledger
from sgr.paper.models import PaperPolicy
from sgr.research.opportunities import Analyzer, PointInTimeInputs
from sgr.research.schemas import TokenBookSnapshot, PriceLevel, stable_record_id


def public_http_vertical_slice(tmp_path):
    """Real connector path with synthetic HTTP responses, explicitly not live evidence."""
    import asyncio
    import httpx
    from paper_fixtures import row, book_payload
    from sgr.connectors.polymarket import PolymarketConnector
    from sgr.research.storage import ResearchStore
    c = setup_paper(tmp_path)
    requests = []
    def provider(request):
        requests.append((request.method, request.url.path))
        if request.url.path == "/markets/keyset":
            payload = {"markets": [row()], "next_cursor": None}
        elif request.url.path == "/fee-rate":
            payload = {"base_fee": 500}
        elif request.url.path == "/tick-size":
            payload = {"minimum_tick_size": ".01"}
        elif request.url.path == "/book":
            payload = book_payload(request.url.params["token_id"])
        else:
            raise AssertionError("Unexpected public request")
        return httpx.Response(200, json=payload)
    async def fetch():
        async with PolymarketConnector(ResearchStore(tmp_path/"public-http"), interval=0,
                                      clock=lambda: NOW, transport=httpx.MockTransport(provider)) as connector:
            page = await connector.page()
            market = page.markets[0]
            books = [await connector.book(market, asset) for asset in market.asset_ids]
            return market, books
    market, books = asyncio.run(fetch())
    decision, forecast = Analyzer(c["inputs"], c["policy"], [c["definition"]]).analyze(market, books, now=NOW)
    assert decision.eligible and forecast is not None
    ledger = c["ledger"]
    assert ledger.reserve(decision, now=NOW)["status"] == "reserved"
    book = next(b for b in books if b.asset_id == decision.asset_id)
    fill = ledger.execute(decision, book, now=NOW)
    assert fill == ledger.execute(decision, book, now=NOW)
    ledger.cancel(decision.id)
    ledger.settle(decision.id, payout=Decimal(1), settled_at=NOW+timedelta(days=2),
                  rule_version=decision.rule_version,
                  source={"url": "https://example.org/synthetic-http-final", "sha256": "1"*64,
                          "condition_id": decision.condition_id})
    report = ledger.reconcile()
    return {"requests": requests, "forecast": forecast, "decision": decision, "report": report}


def setup_paper(tmp_path, policy=None, depth="200"):
    market, target, definition = contract()
    history = [game(100+i, completed=True, year=2025, season_type=NFLSeasonType.REGULAR,
                    when=NOW-timedelta(days=100-i*7), retrieved=NOW-timedelta(days=1)) for i in range(10)]
    inputs = PointInTimeInputs({"game": [target, *history], "availability_report": [], "player_game_statline": []}, NOW)
    policy = policy or PaperPolicy()
    books = []
    for index, asset in enumerate(market.asset_ids):
        price = Decimal(".40") if index == 0 else Decimal(".55")
        books.append(TokenBookSnapshot(
            id=stable_record_id("token_book_snapshot", asset, "synthetic"),
            provider_ids={"asset": asset}, event_time=NOW, retrieved_at=NOW, source_snapshots=(source(),),
            market_id=market.id, condition_id=market.condition_id, asset_id=asset,
            observed_at=NOW, available_at=NOW, metadata_at=NOW,
            bids=(PriceLevel(price_dollars=price-Decimal(".01"), contracts=Decimal(depth)),),
            asks=(PriceLevel(price_dollars=price, contracts=Decimal(depth)),),
            tick_size=Decimal(".01"), minimum_order_size=Decimal("1"), fee_rate_bps=500,
        ))
    analyzer = Analyzer(inputs, policy, [definition])
    decision, forecast = analyzer.analyze(market, books, now=NOW)
    return {"market": market, "game": target, "definition": definition, "history": history, "inputs": inputs,
            "policy": policy, "analyzer": analyzer, "decision": decision, "forecast": forecast,
            "book": next((b for b in books if b.asset_id == decision.asset_id), books[0]),
            "books": books, "ledger": Ledger(tmp_path, policy=policy)}


def competing_decision(original, index, *, group=None):
    return original.model_copy(update={
        "id": stable_record_id("analysis", "competing", index), "condition_id": "contract-"+str(index),
        "event_id": "event-"+str(index), "groups": (group or "team-"+str(index),),
    })
