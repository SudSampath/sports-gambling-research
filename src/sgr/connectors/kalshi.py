from __future__ import annotations

import asyncio
import ssl
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from sgr.config import Settings, settings
from sgr.connectors.kalshi_storage import KalshiSnapshotRef, KalshiSnapshotStore


class KalshiDataError(RuntimeError):
    """Base class for safe, redacted read-only Kalshi failures."""


class KalshiTransportError(KalshiDataError):
    """Raised after bounded retries for a public API failure."""


class KalshiSchemaError(KalshiDataError):
    """Raised when a public response no longer matches the expected contract."""


class KalshiQuoteRejected(KalshiDataError):
    """Raised when an orderbook cannot produce a safe executable quote."""


class KalshiReadOnlyViolation(KalshiDataError):
    """Raised before a non-public or state-changing path can be requested."""


class KalshiMarketSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(min_length=1)
    event_ticker: str = Field(min_length=1)
    participant_side: str = Field(min_length=1)
    title: str = Field(min_length=1)
    status: str = Field(min_length=1)
    rules_primary: str = Field(min_length=1)
    occurrence_at: datetime
    close_at: datetime
    yes_bid_dollars: Decimal = Field(ge=0, le=1)
    yes_ask_dollars: Decimal = Field(ge=0, le=1)
    no_bid_dollars: Decimal = Field(ge=0, le=1)
    no_ask_dollars: Decimal = Field(ge=0, le=1)
    yes_bid_size_fp: Decimal = Field(ge=0)
    yes_ask_size_fp: Decimal = Field(ge=0)
    volume_fp: Decimal = Field(ge=0)
    open_interest_fp: Decimal = Field(ge=0)
    fee_type: str = Field(min_length=1)
    retrieved_at: datetime
    raw_snapshot: KalshiSnapshotRef | None = None

    @field_validator("occurrence_at", "close_at", "retrieved_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Kalshi timestamps must be timezone-aware.")
        return value

    # Compatibility for the existing research CLI while downstream canonical
    # schemas migrate to the explicit dollar/fixed-point names.
    @property
    def market_id(self) -> str:
        return self.ticker

    @property
    def yes_price(self) -> float:
        return float(self.yes_ask_dollars)

    @property
    def no_price(self) -> float:
        return float(self.no_ask_dollars)

    @property
    def volume(self) -> float:
        return float(self.volume_fp)

    @property
    def ts(self) -> datetime:
        return self.retrieved_at


class ExecutableQuote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(min_length=1)
    yes_bid_dollars: Decimal = Field(ge=0, le=1)
    yes_ask_dollars: Decimal = Field(ge=0, le=1)
    no_bid_dollars: Decimal = Field(ge=0, le=1)
    no_ask_dollars: Decimal = Field(ge=0, le=1)
    yes_bid_size_fp: Decimal = Field(gt=0)
    yes_ask_size_fp: Decimal = Field(gt=0)
    no_bid_size_fp: Decimal = Field(gt=0)
    no_ask_size_fp: Decimal = Field(gt=0)
    retrieved_at: datetime

    @field_validator("retrieved_at")
    @classmethod
    def retrieval_time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Kalshi retrieval timestamps must be timezone-aware.")
        return value


class KalshiDiscovery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    series: dict[str, Any]
    events: tuple[dict[str, Any], ...]
    markets: tuple[KalshiMarketSnapshot, ...]


class _Response:
    def __init__(
        self,
        payload: dict[str, Any],
        retrieved_at: datetime,
        snapshot: KalshiSnapshotRef | None,
    ) -> None:
        self.payload = payload
        self.retrieved_at = retrieved_at
        self.snapshot = snapshot


Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], datetime]


class KalshiConnector:
    """Unauthenticated, GET-only Kalshi market-data connector.

    The class intentionally exposes no general request method and no order,
    portfolio, balance, transfer, fund, cancel, or mutation operation.
    """

    SERIES_TICKER = "KXNFLGAME"
    NORMALIZATION_VERSION = "kalshi-market-v1"
    _RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        configured_settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        snapshot_store: KalshiSnapshotStore | None = None,
        timeout: float = 15.0,
        max_attempts: int = 3,
        sleep: Sleep = asyncio.sleep,
        clock: Clock | None = None,
    ) -> None:
        active_settings = configured_settings or settings
        if max_attempts < 1 or max_attempts > 5:
            raise ValueError("max_attempts must be between 1 and 5")
        self.base_url = active_settings.kalshi_api_base_url.rstrip("/")
        self._transport = transport
        self._snapshot_store = snapshot_store
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # httpx otherwise prefers its bundled CA file. The Windows trust store
        # is required in managed environments with a locally trusted TLS root;
        # hostname and certificate verification remain fully enabled.
        self._ssl_context = ssl.create_default_context()

    @staticmethod
    def _ticker(value: str) -> str:
        cleaned = value.strip().upper()
        if not cleaned or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in cleaned):
            raise KalshiReadOnlyViolation("Kalshi ticker contains unsupported characters.")
        return quote(cleaned, safe="-_")

    @staticmethod
    def _assert_public_path(path: str) -> None:
        normalized = str(PurePosixPath("/" + path.lstrip("/")))
        parts = normalized.strip("/").split("/")
        allowed = (
            (parts[0] == "series" and len(parts) == 2)
            or (parts[0] == "events" and len(parts) in {1, 2})
            or (
                parts[0] == "markets"
                and (
                    len(parts) == 1
                    or (len(parts) == 2 and parts[1] == "orderbooks")
                    or (len(parts) == 2 and parts[1] != "orderbooks")
                    or (len(parts) == 3 and parts[2] == "orderbook")
                )
            )
            or parts == ["historical", "cutoff"]
            or (parts[:2] == ["historical", "markets"] and len(parts) in {2, 3})
        )
        forbidden = {
            "portfolio",
            "orders",
            "order",
            "balance",
            "transfer",
            "transfers",
            "fund",
            "funds",
            "cancel",
        }
        if not allowed or forbidden.intersection(parts):
            raise KalshiReadOnlyViolation("Requested path is outside the public read-only market-data surface.")

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> _Response:
        self._assert_public_path(path)
        url = f"{self.base_url}/{path.lstrip('/')}"
        last_status: int | None = None
        for attempt in range(self._max_attempts):
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout,
                    transport=self._transport,
                    headers={"Accept": "application/json"},
                    verify=self._ssl_context,
                ) as client:
                    response = await client.get(url, params=params)
            except httpx.RequestError as error:
                if attempt + 1 == self._max_attempts:
                    raise KalshiTransportError(
                        f"Kalshi public GET failed after {self._max_attempts} attempts ({type(error).__name__})."
                    ) from None
                await self._sleep(min(0.25 * (2**attempt), 2.0))
                continue

            last_status = response.status_code
            if response.status_code in self._RETRYABLE_STATUS:
                if attempt + 1 < self._max_attempts:
                    await self._sleep(min(0.25 * (2**attempt), 2.0))
                    continue
                raise KalshiTransportError(
                    f"Kalshi public GET returned HTTP {response.status_code} after {self._max_attempts} attempts."
                )
            if response.is_error:
                raise KalshiTransportError(f"Kalshi public GET returned HTTP {response.status_code}.")
            try:
                payload = response.json()
            except ValueError:
                raise KalshiSchemaError("Kalshi public GET returned malformed JSON.") from None
            if not isinstance(payload, dict):
                raise KalshiSchemaError("Kalshi public GET returned a non-object response.")
            retrieved_at = self._clock()
            snapshot = (
                self._snapshot_store.persist(
                    endpoint="/" + path.lstrip("/"),
                    payload=payload,
                    retrieved_at=retrieved_at,
                    normalization_version=self.NORMALIZATION_VERSION,
                )
                if self._snapshot_store
                else None
            )
            return _Response(payload, retrieved_at, snapshot)
        raise KalshiTransportError(f"Kalshi public GET failed with HTTP {last_status}.")

    async def _paginate(
        self,
        path: str,
        collection_key: str,
        params: dict[str, Any],
        *,
        max_items: int | None = None,
    ) -> list[tuple[dict[str, Any], datetime, KalshiSnapshotRef | None]]:
        rows: list[tuple[dict[str, Any], datetime, KalshiSnapshotRef | None]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(100):
            page_limit = min(int(params.get("limit", 100)), 1000)
            if max_items is not None:
                page_limit = min(page_limit, max_items - len(rows))
            page_params = {**params, "limit": page_limit}
            if cursor:
                page_params["cursor"] = cursor
            response = await self._get(path, page_params)
            page_rows = response.payload.get(collection_key)
            if not isinstance(page_rows, list):
                raise KalshiSchemaError(f"Kalshi response is missing the {collection_key!r} list.")
            for row in page_rows:
                if not isinstance(row, dict):
                    raise KalshiSchemaError(f"Kalshi {collection_key!r} contains a non-object row.")
                rows.append((row, response.retrieved_at, response.snapshot))
                if max_items is not None and len(rows) >= max_items:
                    return rows
            next_cursor = response.payload.get("cursor")
            if next_cursor in (None, ""):
                return rows
            if not isinstance(next_cursor, str) or next_cursor in seen:
                raise KalshiSchemaError("Kalshi pagination returned an invalid or repeated cursor.")
            seen.add(next_cursor)
            cursor = next_cursor
        raise KalshiSchemaError("Kalshi pagination exceeded the 100-page safety bound.")

    async def get_series(self, series_ticker: str = SERIES_TICKER) -> dict[str, Any]:
        response = await self._get(f"series/{self._ticker(series_ticker)}")
        series = response.payload.get("series")
        if not isinstance(series, dict):
            raise KalshiSchemaError("Kalshi series response is missing the series object.")
        return series

    async def list_events(self, series_ticker: str = SERIES_TICKER) -> list[dict[str, Any]]:
        rows = await self._paginate("events", "events", {"series_ticker": self._ticker(series_ticker)})
        return [row for row, _, _ in rows]

    async def list_markets(
        self,
        limit: int = 100,
        *,
        series_ticker: str = SERIES_TICKER,
        status: str | None = "open",
        fee_type: str | None = None,
    ) -> list[KalshiMarketSnapshot]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if fee_type is None:
            series = await self.get_series(series_ticker)
            fee_type = str(series.get("fee_type") or "").strip()
            if not fee_type:
                raise KalshiSchemaError("Kalshi series response is missing fee_type.")
        params: dict[str, Any] = {"series_ticker": self._ticker(series_ticker), "limit": limit}
        if status:
            params["status"] = status
        rows = await self._paginate("markets", "markets", params, max_items=limit)
        return [self.normalize_market(row, fee_type, retrieved_at, snapshot) for row, retrieved_at, snapshot in rows]

    async def discover_nfl_markets(self, status: str | None = "open") -> KalshiDiscovery:
        series = await self.get_series(self.SERIES_TICKER)
        fee_type = str(series.get("fee_type") or "").strip()
        if not fee_type:
            raise KalshiSchemaError("KXNFLGAME series response is missing fee_type.")
        events = await self.list_events(self.SERIES_TICKER)
        markets = await self.list_markets(
            series_ticker=self.SERIES_TICKER,
            status=status,
            fee_type=fee_type,
        )
        return KalshiDiscovery(series=series, events=tuple(events), markets=tuple(markets))

    async def get_orderbook(self, ticker: str) -> tuple[dict[str, Any], datetime]:
        response = await self._get(f"markets/{self._ticker(ticker)}/orderbook")
        return response.payload, response.retrieved_at

    async def get_orderbooks(self, tickers: list[str]) -> list[dict[str, Any]]:
        if not 1 <= len(tickers) <= 100:
            raise ValueError("Kalshi batch orderbooks require between 1 and 100 tickers.")
        response = await self._get(
            "markets/orderbooks",
            {"tickers": [self._ticker(ticker) for ticker in tickers]},
        )
        orderbooks = response.payload.get("orderbooks")
        if not isinstance(orderbooks, list):
            raise KalshiSchemaError("Kalshi batch orderbook response is missing orderbooks.")
        return orderbooks

    async def get_historical_cutoff(self) -> datetime:
        response = await self._get("historical/cutoff")
        return self._datetime(response.payload, "market_settled_ts")

    async def get_market_for_research(self, ticker: str, settled_at: datetime) -> dict[str, Any]:
        cutoff = await self.get_historical_cutoff()
        if settled_at.tzinfo is None or settled_at.utcoffset() is None:
            raise ValueError("settled_at must be timezone-aware")
        prefix = "historical/markets" if settled_at < cutoff else "markets"
        response = await self._get(f"{prefix}/{self._ticker(ticker)}")
        market = response.payload.get("market")
        if not isinstance(market, dict):
            raise KalshiSchemaError("Kalshi market response is missing the market object.")
        return market

    @classmethod
    def normalize_market(
        cls,
        row: dict[str, Any],
        fee_type: str,
        retrieved_at: datetime,
        snapshot: KalshiSnapshotRef | None = None,
    ) -> KalshiMarketSnapshot:
        try:
            return KalshiMarketSnapshot(
                ticker=cls._required_text(row, "ticker"),
                event_ticker=cls._required_text(row, "event_ticker"),
                participant_side=cls._required_text(row, "primary_participant_key", "yes_sub_title"),
                title=cls._required_text(row, "title"),
                status=cls._required_text(row, "status"),
                rules_primary=cls._required_text(row, "rules_primary"),
                occurrence_at=cls._datetime(row, "occurrence_datetime"),
                close_at=cls._datetime(row, "close_time"),
                yes_bid_dollars=cls._decimal(row, "yes_bid_dollars"),
                yes_ask_dollars=cls._decimal(row, "yes_ask_dollars"),
                no_bid_dollars=cls._decimal(row, "no_bid_dollars"),
                no_ask_dollars=cls._decimal(row, "no_ask_dollars"),
                yes_bid_size_fp=cls._decimal(row, "yes_bid_size_fp"),
                yes_ask_size_fp=cls._decimal(row, "yes_ask_size_fp"),
                volume_fp=cls._decimal(row, "volume_fp"),
                open_interest_fp=cls._decimal(row, "open_interest_fp"),
                fee_type=fee_type,
                retrieved_at=retrieved_at,
                raw_snapshot=snapshot,
            )
        except KalshiSchemaError:
            raise
        except (ValueError, TypeError) as error:
            raise KalshiSchemaError(f"Kalshi market failed strict normalization ({type(error).__name__}).") from None

    @classmethod
    def derive_executable_quote(
        cls,
        ticker: str,
        payload: dict[str, Any],
        retrieved_at: datetime,
        *,
        as_of: datetime | None = None,
        max_age: timedelta = timedelta(seconds=30),
    ) -> ExecutableQuote:
        as_of = as_of or datetime.now(timezone.utc)
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise KalshiQuoteRejected("Orderbook retrieval time must be timezone-aware.")
        age = as_of - retrieved_at
        if age < timedelta(0) or age > max_age:
            raise KalshiQuoteRejected("Orderbook is stale or has an invalid retrieval time.")
        book = payload.get("orderbook_fp")
        if not isinstance(book, dict):
            raise KalshiQuoteRejected("Orderbook is missing orderbook_fp.")
        yes_bid, yes_bid_size = cls._best_bid(book.get("yes_dollars"), "YES")
        no_bid, no_bid_size = cls._best_bid(book.get("no_dollars"), "NO")
        yes_ask = Decimal("1") - no_bid
        no_ask = Decimal("1") - yes_bid
        if yes_bid > yes_ask or no_bid > no_ask:
            raise KalshiQuoteRejected("Orderbook is crossed and cannot produce an executable quote.")
        return ExecutableQuote(
            ticker=cls._ticker(ticker),
            yes_bid_dollars=yes_bid,
            yes_ask_dollars=yes_ask,
            no_bid_dollars=no_bid,
            no_ask_dollars=no_ask,
            yes_bid_size_fp=yes_bid_size,
            yes_ask_size_fp=no_bid_size,
            no_bid_size_fp=no_bid_size,
            no_ask_size_fp=yes_bid_size,
            retrieved_at=retrieved_at,
        )

    @classmethod
    def _best_bid(cls, levels: Any, side: str) -> tuple[Decimal, Decimal]:
        if not isinstance(levels, list) or not levels:
            raise KalshiQuoteRejected(f"{side} orderbook is empty.")
        parsed: list[tuple[Decimal, Decimal]] = []
        for level in levels:
            if not isinstance(level, list) or len(level) != 2:
                raise KalshiQuoteRejected(f"{side} orderbook contains a malformed price level.")
            try:
                price, size = Decimal(str(level[0])), Decimal(str(level[1]))
            except InvalidOperation:
                raise KalshiQuoteRejected(f"{side} orderbook contains a non-decimal price level.") from None
            if not Decimal("0") <= price <= Decimal("1") or size <= 0:
                raise KalshiQuoteRejected(f"{side} orderbook contains an invalid price or size.")
            parsed.append((price, size))
        return max(parsed, key=lambda item: item[0])

    @staticmethod
    def _required_text(row: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        raise KalshiSchemaError(f"Kalshi response is missing required field {keys[0]!r}.")

    @staticmethod
    def _decimal(row: dict[str, Any], key: str) -> Decimal:
        if key not in row:
            raise KalshiSchemaError(f"Kalshi response is missing current fixed-point field {key!r}.")
        try:
            return Decimal(str(row[key]))
        except (InvalidOperation, TypeError):
            raise KalshiSchemaError(f"Kalshi field {key!r} is not a fixed-point decimal string.") from None

    @staticmethod
    def _datetime(row: dict[str, Any], key: str) -> datetime:
        value = row.get(key)
        if not isinstance(value, str):
            raise KalshiSchemaError(f"Kalshi response is missing timestamp {key!r}.")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise KalshiSchemaError(f"Kalshi timestamp {key!r} is malformed.") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise KalshiSchemaError(f"Kalshi timestamp {key!r} is not timezone-aware.")
        return parsed
