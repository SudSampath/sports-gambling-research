"""Public market data only. No account, signer, wallet, or order API."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

import httpx

from sgr.connectors.base import APIRequestError
from sgr.research.schemas import (
    PolymarketEvent, PolymarketMarket, PriceLevel, RawSnapshotRef,
    TokenBookSnapshot, stable_record_id,
)
from sgr.research.storage import ResearchStore

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
MARKET_STREAM = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class BudgetExhausted(APIRequestError):
    pass


class SchemaDrift(APIRequestError):
    pass


@dataclass
class RequestBudget:
    maximum: int = 100
    used: int = 0
    retries: int = 0
    cache_hits: int = 0

    def claim(self) -> None:
        if self.used >= self.maximum:
            raise BudgetExhausted("Public request budget exhausted.")
        self.used += 1


@dataclass(frozen=True)
class MarketPage:
    markets: tuple[PolymarketMarket, ...]
    next_cursor: str | None
    raw_count: int
    exclusions: tuple[str, ...]
    source: RawSnapshotRef


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Provider timestamp is naive.")
    return parsed


def public_array(value) -> tuple[str, ...]:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, list) or not all(isinstance(x, str) and x for x in parsed):
        raise ValueError("Expected public string array.")
    return tuple(parsed)


def normalize_market(row: dict, source: RawSnapshotRef) -> tuple[PolymarketEvent, PolymarketMarket]:
    events = row["events"]
    if len(events) != 1:
        raise ValueError("Ambiguous event identity.")
    event = events[0]
    now = source.retrieved_at
    event_id = stable_record_id("polymarket_event", event["id"], source.sha256, now.isoformat())
    base = dict(event_time=now, retrieved_at=now, source_snapshots=(source,))
    canonical_event = PolymarketEvent(
        id=event_id, provider_ids={"gamma": str(event["id"])}, external_id=str(event["id"]),
        slug=event["slug"], title=event["title"], **base,
    )
    rules = row["description"]
    condition = row["conditionId"]
    market = PolymarketMarket(
        id=stable_record_id("polymarket_market", condition, source.sha256, now.isoformat()),
        provider_ids={"gamma": str(row["id"]), "condition": condition},
        external_id=str(row["id"]), condition_id=condition, event_id=event_id,
        slug=row["slug"], question=row["question"],
        outcomes=public_array(row["outcomes"]), asset_ids=public_array(row["clobTokenIds"]),
        rules=rules, rule_version=hashlib.sha256(rules.encode()).hexdigest(),
        resolution_source=row.get("resolutionSource") or event.get("resolutionSource", ""),
        deadline_at=parse_time(row["endDate"]),
        game_start_at=parse_time(row["gameStartTime"]) if row.get("gameStartTime") else None,
        available_at=now, market_type=row.get("sportsMarketType") or "unknown",
        active=bool(row.get("active")) and not bool(row.get("closed")),
        accepting_orders=bool(row.get("acceptingOrders")),
        negative_risk=bool(row.get("negRisk")), fee_type=row.get("feeType") or "unknown",
        **base,
    )
    return canonical_event, market


class PolymarketConnector:
    """Campaign-scoped client: backpressure, budgets, archive, TTL cache.

    Requests are spaced at <=5/sec by default (well below published ceilings).
    Retries consume the same budget. Redirects and arbitrary provider URLs are
    disabled. Failed responses never fabricate empty successful pages.
    """

    def __init__(
        self, store: ResearchStore, *, budget: RequestBudget | None = None,
        concurrency: int = 4, interval: float = .2,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        if not 1 <= concurrency <= 16 or interval < 0:
            raise ValueError("Invalid public-data concurrency/rate.")
        self.store, self.budget, self.clock = store, budget or RequestBudget(), clock
        self.interval = interval
        self.client = httpx.AsyncClient(timeout=15, transport=transport, follow_redirects=False)
        self.semaphore = asyncio.Semaphore(concurrency)
        self.rate_lock = asyncio.Lock()
        self.last_request = 0.0
        self.cache: dict[str, tuple[float, dict | list, RawSnapshotRef]] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.client.aclose()

    async def _get(self, host: str, path: str, params: dict, *, ttl: float = 0):
        allowed = {GAMMA: {"/markets/keyset", "/sports"}, CLOB: {"/book", "/fee-rate", "/tick-size"}}
        if path not in allowed.get(host, set()):
            raise ValueError("Endpoint is outside the public market-data allowlist.")
        key = json.dumps([host, path, params], sort_keys=True)
        cached = self.cache.get(key)
        if ttl > 0 and cached and time.monotonic() - cached[0] <= ttl:
            self.budget.cache_hits += 1
            return cached[1], cached[2]
        async with self.semaphore:
            for attempt in range(3):
                async with self.rate_lock:
                    delay = self.interval - (time.monotonic() - self.last_request)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    self.budget.claim()
                    self.last_request = time.monotonic()
                try:
                    response = await self.client.get(host + path, params=params)
                    if response.status_code == 429 or response.status_code >= 500:
                        raise httpx.HTTPStatusError("Temporary public provider failure.", request=response.request, response=response)
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, (dict, list)):
                        raise SchemaDrift("Public response has an unexpected shape.")
                    # Store public request parameters in the evidence envelope;
                    # source URLs omit query parameters under the canonical URL policy.
                    source = self.store.retain_raw_snapshot(
                        "polymarket", {"request": params, "response": payload},
                        source_url=host + path, retrieved_at=self.clock(),
                    )
                    if ttl > 0:
                        if len(self.cache) >= 128 and key not in self.cache:
                            self.cache.pop(next(iter(self.cache)))
                        self.cache[key] = (time.monotonic(), payload, source)
                    return payload, source
                except (httpx.HTTPError, json.JSONDecodeError) as error:
                    retryable = not isinstance(error, httpx.HTTPStatusError) or error.response.status_code == 429 or error.response.status_code >= 500
                    if not retryable or attempt == 2:
                        raise APIRequestError("Public Polymarket data unavailable; no execution permitted.") from error
                    self.budget.retries += 1
                    delay = .25 * 2**attempt
                    if isinstance(error, httpx.HTTPStatusError):
                        try:
                            delay = max(delay, min(5, float(error.response.headers.get("Retry-After", 0))))
                        except ValueError:
                            pass
                    await asyncio.sleep(delay)
        raise AssertionError("Unreachable")

    async def nfl_tag(self) -> int:
        payload, _ = await self._get(GAMMA, "/sports", {}, ttl=3600)
        if not isinstance(payload, list) or not all(isinstance(x, dict) for x in payload):
            raise SchemaDrift("Sports discovery response is malformed.")
        matches = [x for x in payload if x.get("sport") == "nfl"]
        if len(matches) != 1:
            raise SchemaDrift("NFL discovery tag is ambiguous.")
        try:
            return int(matches[0]["primaryTagId"])
        except (KeyError, TypeError, ValueError) as error:
            raise SchemaDrift("NFL primary tag is missing.") from error

    async def page(self, cursor: str | None = None, *, limit: int = 100, tag_id: int | None = None) -> MarketPage:
        if not 1 <= limit <= 100:
            raise ValueError("Keyset limit must be 1..100.")
        params: dict = {"limit": limit, "closed": "false"}
        if cursor:
            params["after_cursor"] = cursor
        if tag_id is not None:
            params["tag_id"] = tag_id
        payload, source = await self._get(GAMMA, "/markets/keyset", params, ttl=30)
        if not isinstance(payload, dict) or not isinstance(payload.get("markets"), list) or "next_cursor" not in payload:
            raise SchemaDrift("Keyset response is missing markets/cursor.")
        next_cursor = payload["next_cursor"] or None
        if next_cursor is not None and (not isinstance(next_cursor, str) or next_cursor == cursor):
            raise SchemaDrift("Keyset cursor failed to advance.")
        records, markets, exclusions, seen = [], [], [], set()
        for row in payload["markets"]:
            try:
                event, market = normalize_market(row, source)
                if market.condition_id in seen:
                    exclusions.append("duplicate_condition")
                    continue
                seen.add(market.condition_id)
                records.extend((event, market))
                markets.append(market)
            except (KeyError, TypeError, ValueError, AttributeError):
                exclusions.append("schema_or_identity_ambiguity")
        if records:
            self.store.write(records)
        return MarketPage(tuple(markets), next_cursor, len(payload["markets"]), tuple(exclusions), source)

    async def book(self, market: PolymarketMarket, asset_id: str) -> TokenBookSnapshot:
        if asset_id not in market.asset_ids:
            raise ValueError("Asset does not belong to contract.")
        # Always fetch a new book; metadata may be cached only briefly.
        tasks = [asyncio.create_task(self._get(CLOB, path, {"token_id": asset_id}, ttl=ttl))
                 for path, ttl in (("/book", 0), ("/fee-rate", 30), ("/tick-size", 30))]
        try:
            (payload, source), (fees, fee_source), (ticks, tick_source) = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        try:
            if payload["asset_id"] != asset_id or payload["market"] != market.condition_id:
                raise ValueError("Provider book identity mismatch.")
            raw_time = Decimal(payload["timestamp"])
            # Current CLOB timestamps are milliseconds; tolerate documented seconds.
            observed = datetime.fromtimestamp(float(raw_time / (1000 if raw_time > 10**11 else 1)), timezone.utc)
            if not 0 <= (source.retrieved_at - observed).total_seconds() <= 10:
                raise ValueError("Provider book is stale or future-dated.")
            if payload["neg_risk"] != market.negative_risk:
                raise ValueError("Negative-risk parameters changed.")
            tick = Decimal(str(ticks["minimum_tick_size"]))
            if tick != Decimal(payload["tick_size"]):
                raise ValueError("Tick parameters disagree.")
            def levels(side):
                return tuple(sorted(
                    (PriceLevel(price_dollars=Decimal(x["price"]), contracts=Decimal(x["size"])) for x in payload[side]),
                    key=lambda x: x.price_dollars, reverse=side == "bids",
                ))
            book = TokenBookSnapshot(
                id=stable_record_id("token_book_snapshot", asset_id, source.sha256, source.retrieved_at.isoformat()),
                provider_ids={"asset": asset_id, "condition": market.condition_id},
                event_time=observed, retrieved_at=source.retrieved_at,
                source_snapshots=(source, fee_source, tick_source),
                market_id=market.id, condition_id=market.condition_id, asset_id=asset_id,
                observed_at=observed, available_at=max(s.retrieved_at for s in (source, fee_source, tick_source)),
                bids=levels("bids"), asks=levels("asks"), tick_size=tick,
                minimum_order_size=Decimal(payload["min_order_size"]), fee_rate_bps=fees["base_fee"],
                metadata_at=min(fee_source.retrieved_at, tick_source.retrieved_at),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise SchemaDrift("Book/fee/tick evidence is incoherent.") from error
        self.store.write([book])
        return book
