from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from pydantic import SecretStr

from sgr.config import ConfigurationError, settings
from sgr.connectors.base import APIRequestError
from sgr.connectors.theoddsapi import TheOddsAPIConnector, TheOddsAPIEntitlementError

SNAPSHOT_AT = datetime(2024, 9, 8, 18, 0, tzinfo=timezone.utc)

SAMPLE_PAYLOAD = {
    "timestamp": "2024-09-08T17:55:00Z",
    "previous_timestamp": "2024-09-08T17:50:00Z",
    "next_timestamp": "2024-09-08T18:00:00Z",
    "data": [],
}


class _JsonResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _StatusErrorResponse:
    def __init__(self, status_code: int) -> None:
        self._status_code = status_code

    def raise_for_status(self) -> None:
        request = httpx.Request(
            "GET", f"https://api.the-odds-api.com/v4/historical/sports/americanfootball_nfl/odds?apiKey=secret"
        )
        response = httpx.Response(self._status_code, request=request)
        raise httpx.HTTPStatusError("boom", request=request, response=response)


class _FakeClient:
    response: object = None

    def __init__(self, **_: object) -> None:
        pass

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    async def get(self, url: str, params: dict | None = None, headers: dict | None = None):
        return type(self).response


def _configure_key(monkeypatch, key: str = "a-real-looking-key") -> None:
    monkeypatch.setattr(settings, "theodds_api_key", SecretStr(key))


def test_historical_odds_requires_a_configured_key():
    connector = TheOddsAPIConnector()

    with pytest.raises(ConfigurationError):
        asyncio.run(connector.historical_odds("americanfootball_nfl", SNAPSHOT_AT))


def test_historical_odds_rejects_naive_snapshot_timestamp(monkeypatch):
    _configure_key(monkeypatch)
    connector = TheOddsAPIConnector()

    with pytest.raises(ValueError, match="timezone-aware"):
        asyncio.run(connector.historical_odds("americanfootball_nfl", datetime(2024, 9, 8, 18, 0)))


@pytest.mark.parametrize("status_code", [401, 402, 403])
def test_entitlement_failure_statuses_raise_a_typed_error_without_credentials(monkeypatch, status_code):
    _configure_key(monkeypatch)
    _FakeClient.response = _StatusErrorResponse(status_code)
    monkeypatch.setattr("sgr.connectors.base.httpx.AsyncClient", _FakeClient)
    connector = TheOddsAPIConnector()

    with pytest.raises(TheOddsAPIEntitlementError) as error:
        asyncio.run(connector.historical_odds("americanfootball_nfl", SNAPSHOT_AT))

    assert "a-real-looking-key" not in str(error.value)
    assert "paid" in str(error.value).lower() or "plan" in str(error.value).lower()


def test_non_entitlement_http_error_is_not_swallowed(monkeypatch):
    _configure_key(monkeypatch)
    _FakeClient.response = _StatusErrorResponse(500)
    monkeypatch.setattr("sgr.connectors.base.httpx.AsyncClient", _FakeClient)
    connector = TheOddsAPIConnector()

    with pytest.raises(APIRequestError):
        asyncio.run(connector.historical_odds("americanfootball_nfl", SNAPSHOT_AT))


def test_successful_response_is_checksummed(monkeypatch):
    _configure_key(monkeypatch)
    _FakeClient.response = _JsonResponse(SAMPLE_PAYLOAD)
    monkeypatch.setattr("sgr.connectors.base.httpx.AsyncClient", _FakeClient)
    connector = TheOddsAPIConnector()

    snapshot = asyncio.run(connector.historical_odds("americanfootball_nfl", SNAPSHOT_AT))

    assert snapshot.payload == SAMPLE_PAYLOAD
    assert len(snapshot.sha256) == 64
    assert snapshot.requested_at == SNAPSHOT_AT
