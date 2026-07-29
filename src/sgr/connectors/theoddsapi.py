from __future__ import annotations

from datetime import datetime

from sgr.config import settings
from sgr.connectors.base import APIConnector
from sgr.models import SportsEventOdds


class TheOddsAPIConnector(APIConnector):
    def __init__(self) -> None:
        super().__init__(base_url=settings.theodds_api_base_url)

    async def get_h2h_odds(self, sport: str = "baseball_mlb", regions: str = "us") -> list[SportsEventOdds]:
        payload = await self.get_json(
            f"sports/{sport}/odds",
            params={
                "apiKey": settings.theodds_api_key,
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
