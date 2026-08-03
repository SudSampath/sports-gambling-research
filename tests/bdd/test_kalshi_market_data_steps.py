from __future__ import annotations

import asyncio
import json
import ssl
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pytest_bdd import given, scenarios, then, when

from sgr.config import Settings
from sgr.connectors.kalshi import (
    KalshiConnector,
    KalshiQuoteRejected,
    KalshiReadOnlyViolation,
    KalshiSchemaError,
    KalshiTransportError,
)
from sgr.connectors.kalshi_storage import KalshiSnapshotStore


scenarios("../features/kalshi_market_data.feature")

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "kalshi_api_2026.json"
NOW = datetime(2026, 8, 3, 4, 0, tzinfo=timezone.utc)


@pytest.fixture
def context() -> dict[str, object]:
    return {}


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        kalshi_api_base_url="https://external-api.kalshi.com/trade-api/v2",
    )


@given("current public KXNFLGAME API fixtures across cursor pages")
def current_public_fixtures(context):
    context["fixture"] = _fixture()
    context["requests"] = []


@when("the read-only connector discovers the NFL series")
def discover_nfl_series(context):
    fixture = context["fixture"]

    def handler(request: httpx.Request) -> httpx.Response:
        context["requests"].append(request)
        path = request.url.path
        cursor = request.url.params.get("cursor")
        if path.endswith("/series/KXNFLGAME"):
            payload = fixture["series"]
        elif path.endswith("/events"):
            payload = fixture["events_page_2"] if cursor else fixture["events_page_1"]
        elif path.endswith("/markets"):
            payload = fixture["markets_page_2"] if cursor else fixture["markets_page_1"]
        else:
            return httpx.Response(404, json={"code": "not_found"})
        return httpx.Response(200, json=payload)

    connector = KalshiConnector(
        _settings(),
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    context["discovery"] = asyncio.run(connector.discover_nfl_markets())


@then("every event and market page is collected without authentication headers")
def collected_without_authentication(context):
    discovery = context["discovery"]
    assert len(discovery.events) == 2
    assert len(discovery.markets) == 2
    for request in context["requests"]:
        assert not any(name.lower().startswith("kalshi-access-") for name in request.headers)


def test_connector_keeps_tls_hostname_and_certificate_verification_enabled():
    connector = KalshiConnector(_settings())

    assert connector._ssl_context.check_hostname is True
    assert connector._ssl_context.verify_mode == ssl.CERT_REQUIRED


@then("the recommended external-api production host is used")
def recommended_host_used(context):
    assert {request.url.host for request in context["requests"]} == {"external-api.kalshi.com"}


@given("a current 2026 Kalshi market response")
def current_market_response(context):
    context["row"] = _fixture()["markets_page_1"]["markets"][0]


@when("the market is normalized")
def normalize_market(context):
    context["market"] = KalshiConnector.normalize_market(
        context["row"],
        fee_type="quadratic",
        retrieved_at=NOW,
    )


@then("price count rules timing participant fee and retrieval fields are retained")
def retained_market_fields(context):
    market = context["market"]
    assert market.ticker == "KXNFLGAME-26SEP10DALPHI-DAL"
    assert market.event_ticker == "KXNFLGAME-26SEP10DALPHI"
    assert market.participant_side == "DAL"
    assert market.rules_primary.startswith("Resolves Yes")
    assert market.occurrence_at == datetime(2026, 9, 11, 0, 20, tzinfo=timezone.utc)
    assert market.close_at == datetime(2026, 9, 11, 3, 30, tzinfo=timezone.utc)
    assert market.yes_bid_dollars == Decimal("0.4600")
    assert market.yes_ask_size_fp == Decimal("80.00")
    assert market.volume_fp == Decimal("1520.50")
    assert market.open_interest_fp == Decimal("910.25")
    assert market.fee_type == "quadratic"
    assert market.retrieved_at == NOW


@then("a response containing only removed legacy fields is rejected")
def legacy_fields_rejected(context):
    legacy = dict(context["row"])
    for key in list(legacy):
        if key.endswith("_dollars") or key.endswith("_fp"):
            legacy.pop(key)
    legacy.update({"yes_bid": 46, "yes_ask": 48, "volume": 1520})
    with pytest.raises(KalshiSchemaError, match="fixed-point field"):
        KalshiConnector.normalize_market(legacy, "quadratic", NOW)


@given("a current YES and NO bid orderbook")
def current_orderbook(context):
    context["orderbook"] = _fixture()["orderbook"]


@when("executable bid and ask prices are derived")
def derive_prices(context):
    context["quote"] = KalshiConnector.derive_executable_quote(
        "KXNFLGAME-26SEP10DALPHI-DAL",
        context["orderbook"],
        NOW,
        as_of=NOW + timedelta(seconds=5),
    )


@then("the opposite-side bid determines each ask and its available size")
def reciprocal_prices(context):
    quote = context["quote"]
    assert quote.yes_bid_dollars == Decimal("0.4600")
    assert quote.yes_ask_dollars == Decimal("0.4800")
    assert quote.no_bid_dollars == Decimal("0.5200")
    assert quote.no_ask_dollars == Decimal("0.5400")
    assert quote.yes_ask_size_fp == Decimal("80.00")
    assert quote.no_ask_size_fp == Decimal("125.00")


@then("empty crossed and stale orderbooks are rejected")
def unsafe_books_rejected(context):
    empty = {"orderbook_fp": {"yes_dollars": [], "no_dollars": [["0.52", "1.00"]]}}
    crossed = {
        "orderbook_fp": {
            "yes_dollars": [["0.60", "1.00"]],
            "no_dollars": [["0.50", "1.00"]],
        }
    }
    with pytest.raises(KalshiQuoteRejected, match="empty"):
        KalshiConnector.derive_executable_quote("TICKER", empty, NOW, as_of=NOW)
    with pytest.raises(KalshiQuoteRejected, match="crossed"):
        KalshiConnector.derive_executable_quote("TICKER", crossed, NOW, as_of=NOW)
    with pytest.raises(KalshiQuoteRejected, match="stale"):
        KalshiConnector.derive_executable_quote(
            "TICKER",
            context["orderbook"],
            NOW,
            as_of=NOW + timedelta(seconds=31),
        )


@given("a current historical cutoff and an older settled market")
def historical_fixture(context, tmp_path):
    context["fixture"] = _fixture()
    context["requests"] = []
    context["store"] = KalshiSnapshotStore(tmp_path / "kalshi")


@when("the market is requested for research")
def request_historical_market(context):
    fixture = context["fixture"]

    def handler(request: httpx.Request) -> httpx.Response:
        context["requests"].append(request)
        if request.url.path.endswith("/historical/cutoff"):
            return httpx.Response(200, json=fixture["historical_cutoff"])
        if request.url.path.endswith("/historical/markets/KXNFLGAME-24FEB11SFKC-SF"):
            return httpx.Response(200, json=fixture["historical_market"])
        return httpx.Response(404, json={"code": "not_found"})

    connector = KalshiConnector(
        _settings(),
        transport=httpx.MockTransport(handler),
        snapshot_store=context["store"],
        clock=lambda: NOW,
    )
    context["historical_market"] = asyncio.run(
        connector.get_market_for_research(
            "KXNFLGAME-24FEB11SFKC-SF",
            datetime(2024, 2, 12, tzinfo=timezone.utc),
        )
    )


@then("the historical market endpoint is used")
def historical_endpoint_used(context):
    paths = [request.url.path for request in context["requests"]]
    assert paths[-1].endswith("/historical/markets/KXNFLGAME-24FEB11SFKC-SF")
    assert context["historical_market"]["status"] == "settled"


@then("raw responses and normalization metadata are persisted for replay")
def snapshots_persisted(context):
    root = context["store"].root
    raw = list((root / "raw").glob("*.json"))
    metadata = list((root / "metadata").glob("*.json"))
    assert len(raw) == len(metadata) == 2
    for path in metadata:
        item = json.loads(path.read_text(encoding="utf-8"))
        assert item["normalization_version"] == "kalshi-market-v1"
        assert item["retrieved_at"] == NOW.isoformat()


@given("a public API that rate limits before succeeding")
def rate_limited_api(context):
    context["attempts"] = 0
    context["delays"] = []


@when("the read-only connector retries the request")
def retry_request(context):
    fixture = _fixture()

    def handler(_: httpx.Request) -> httpx.Response:
        context["attempts"] += 1
        if context["attempts"] < 3:
            return httpx.Response(429, json={"error": "too many requests"})
        return httpx.Response(200, json=fixture["series"])

    async def record_sleep(delay: float) -> None:
        context["delays"].append(delay)

    connector = KalshiConnector(
        _settings(),
        transport=httpx.MockTransport(handler),
        sleep=record_sleep,
    )
    context["series"] = asyncio.run(connector.get_series())
    context["connector"] = connector


@then("exponential backoff is bounded and the response succeeds")
def bounded_backoff(context):
    assert context["attempts"] == 3
    assert context["delays"] == [0.25, 0.5]
    assert context["series"]["ticker"] == "KXNFLGAME"


@then("malformed responses and API failures return typed redacted errors")
def typed_redacted_failures():
    def malformed(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    def unavailable(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"details": "private-upstream-diagnostic"})

    malformed_connector = KalshiConnector(
        _settings(), transport=httpx.MockTransport(malformed), max_attempts=1
    )
    unavailable_connector = KalshiConnector(
        _settings(), transport=httpx.MockTransport(unavailable), max_attempts=1
    )
    with pytest.raises(KalshiSchemaError, match="malformed JSON"):
        asyncio.run(malformed_connector.get_series())
    with pytest.raises(KalshiTransportError) as error:
        asyncio.run(unavailable_connector.get_series())
    assert "private-upstream-diagnostic" not in str(error.value)


@then("portfolio order cancel balance transfer and fund paths are unreachable")
def trading_paths_unreachable(context):
    connector = context["connector"]
    for method in ("create_order", "cancel_order", "get_balance", "transfer_funds"):
        assert not hasattr(connector, method)
    for path in ("portfolio/orders", "orders", "markets/orders", "balance", "transfers", "funds"):
        with pytest.raises(KalshiReadOnlyViolation):
            connector._assert_public_path(path)


@given("a fixture derived from the published OpenAPI schema and changelog")
def published_contract_fixture(context):
    context["contract_fixture"] = _fixture()


@then("current market orderbook pagination and historical contract fields are covered")
def contract_fields_covered(context):
    fixture = context["contract_fixture"]
    contract = fixture["contract"]
    market = fixture["markets_page_1"]["markets"][0]
    assert contract["openapi_url"] == "https://docs.kalshi.com/openapi.yaml"
    assert set(contract["required_fields"]).issubset(market)
    assert fixture["markets_page_1"]["cursor"]
    assert {"yes_dollars", "no_dollars"} <= set(fixture["orderbook"]["orderbook_fp"])
    assert {
        "market_settled_ts",
        "trades_created_ts",
        "orders_updated_ts",
    } <= set(fixture["historical_cutoff"])
    assert "/markets/orderbooks" in contract["required_paths"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/markets/orderbooks"):
            return httpx.Response(200, json=fixture["batch_orderbooks"])
        if request.url.path.endswith("/orderbook"):
            return httpx.Response(200, json=fixture["orderbook"])
        return httpx.Response(404, json={"code": "not_found"})

    connector = KalshiConnector(_settings(), transport=httpx.MockTransport(handler), clock=lambda: NOW)
    single, retrieved_at = asyncio.run(
        connector.get_orderbook("KXNFLGAME-26SEP10DALPHI-DAL")
    )
    batch = asyncio.run(connector.get_orderbooks(["KXNFLGAME-26SEP10DALPHI-DAL"]))
    assert single == fixture["orderbook"]
    assert retrieved_at == NOW
    assert batch == fixture["batch_orderbooks"]["orderbooks"]
