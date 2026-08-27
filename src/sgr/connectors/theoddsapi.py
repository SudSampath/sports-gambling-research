from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sgr.config import settings
from sgr.connectors.base import APIConnector, APIRequestError
from sgr.models import SportsEventOdds

# The Odds API returns one of these HTTP statuses when a key is valid but the
# account's plan does not include historical access -- verified against the
# provider's own historical-odds documentation (only paid plans can call
# this endpoint at all). Distinguished from a generic APIRequestError so a
# caller can react to "you need a different plan" separately from "the
# request itself was malformed" or "the network failed."
_ENTITLEMENT_HTTP_STATUSES = frozenset({401, 402, 403})

HISTORICAL_MARKETS = ("h2h", "spreads", "totals")


class TheOddsAPIEntitlementError(RuntimeError):
    """Raised when historical odds are requested without a paid-plan credential.

    Never purchase a plan or prompt for a credential in response to this --
    it means the configured key (if any) does not currently have historical
    entitlement. See https://the-odds-api.com/historical-odds-data/.
    """


@dataclass(frozen=True)
class HistoricalOddsSnapshot:
    """One historical-odds API response, kept intact for checksumming and
    for the previous/next timestamp metadata the provider returns."""

    payload: dict
    requested_at: datetime
    retrieved_at: datetime
    sha256: str
    source_url: str


class TheOddsAPIConnector(APIConnector):
    def __init__(self) -> None:
        super().__init__(base_url=settings.theodds_api_base_url)

    async def historical_odds(
        self,
        sport: str,
        snapshot_at: datetime,
        *,
        regions: str = "us",
        markets: tuple[str, ...] = HISTORICAL_MARKETS,
        odds_format: str = "decimal",
    ) -> HistoricalOddsSnapshot:
        """Fetch the closest historical odds snapshot at or before snapshot_at.

        Requires a paid-plan credential (raises TheOddsAPIEntitlementError
        before any data is written if the account lacks historical
        entitlement, or ConfigurationError if no key is configured at all --
        both fail before this function returns anything, so no partial
        canonical data is ever produced). Never purchase a plan or request a
        credential to satisfy this; this repository does not have one.
        """
        if snapshot_at.tzinfo is None or snapshot_at.utcoffset() is None:
            raise ValueError("snapshot_at must be timezone-aware.")

        path = f"historical/sports/{sport}/odds"
        params = {
            "apiKey": settings.require_theodds_api_key(),
            "regions": regions,
            "markets": ",".join(markets),
            "oddsFormat": odds_format,
            "dateFormat": "iso",
            "date": snapshot_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        try:
            payload = await self.get_json(path, params=params)
        except APIRequestError as error:
            if error.status_code in _ENTITLEMENT_HTTP_STATUSES:
                raise TheOddsAPIEntitlementError(
                    "Historical odds require an Odds API plan with historical entitlement. "
                    "This is not a code defect: set THEODDS_API_KEY to a key on a plan that "
                    "includes historical data, or skip historical retrieval. See "
                    "https://the-odds-api.com/historical-odds-data/ for plan details."
                ) from None
            raise

        retrieved_at = datetime.now(timezone.utc)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return HistoricalOddsSnapshot(
            payload=payload,
            requested_at=snapshot_at,
            retrieved_at=retrieved_at,
            sha256=hashlib.sha256(canonical).hexdigest(),
            source_url=f"{self.base_url}/{path}",
        )

    async def get_h2h_odds(self, sport: str = "baseball_mlb", regions: str = "us") -> list[SportsEventOdds]:
        payload = await self.get_json(
            f"sports/{sport}/odds",
            params={
                "apiKey": settings.require_theodds_api_key(),
                "regions": regions,
                "markets": "h2h",
                "oddsFormat": "decimal",
            },
        )
        rows: list[SportsEventOdds] = []
        for event in payload:
            home_team = event.get("home_team", "")
            away_team = ""
            teams = event.get("away_team")
            if teams:
                away_team = str(teams)
            bookmakers = event.get("bookmakers", [])
            for book in bookmakers:
                markets = book.get("markets", [])
                if not markets:
                    continue
                outcomes = markets[0].get("outcomes", [])
                home_price = None
                away_price = None
                for outcome in outcomes:
                    if outcome.get("name") == home_team:
                        home_price = outcome.get("price")
                    else:
                        away_price = outcome.get("price")
                if home_price and away_price:
                    rows.append(
                        SportsEventOdds(
                            event_id=str(event.get("id", "")),
                            home_team=home_team,
                            away_team=away_team,
                            commence_time=datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00")),
                            book=str(book.get("key", "")),
                            home_decimal_odds=float(home_price),
                            away_decimal_odds=float(away_price),
                        )
                    )
        return rows
