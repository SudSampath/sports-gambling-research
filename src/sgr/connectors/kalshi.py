from __future__ import annotations

from datetime import datetime, timezone

from sgr.config import settings
from sgr.connectors.base import APIConnector
from sgr.models import MarketSnapshot


class KalshiConnector(APIConnector):
    def __init__(self) -> None:
        headers = {"Accept": "application/json"}
        if settings.kalshi_api_key:
            headers["KALSHI-ACCESS-KEY"] = settings.kalshi_api_key
        super().__init__(base_url=settings.kalshi_api_base_url, headers=headers)

    async def list_markets(self, limit: int = 25) -> list[MarketSnapshot]:
        payload = await self.get_json("markets", params={"limit": limit})
        markets = payload.get("markets", [])
        snapshots: list[MarketSnapshot] = []
        now = datetime.now(timezone.utc)
        for row in markets:
            yes_ask = float(row.get("yes_ask", 0) or 0)
            no_ask = float(row.get("no_ask", 0) or 0)
            snapshots.append(
                MarketSnapshot(
                    market_id=str(row.get("id", "")),
                    ticker=str(row.get("ticker", "")),
                    title=str(row.get("title", "")),
                    yes_price=yes_ask / 100.0 if yes_ask > 1 else yes_ask,
                    no_price=no_ask / 100.0 if no_ask > 1 else no_ask,
                    volume=float(row.get("volume", 0) or 0),
                    ts=now,
                )
            )
        return snapshots
