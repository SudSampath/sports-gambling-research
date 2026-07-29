from __future__ import annotations

from typing import Any

from sgr.config import settings
from sgr.connectors.base import APIConnector


class SportsDataConnector(APIConnector):
    def __init__(self) -> None:
        headers = {"Ocp-Apim-Subscription-Key": settings.sportsdata_api_key}
        super().__init__(base_url=settings.sportsdata_api_base_url, headers=headers)

    async def get_scores(self, sport_path: str = "mlb/scores/json/ScoresByDate", date_key: str = "2026-JUL-21") -> list[dict[str, Any]]:
        payload = await self.get_json(f"{sport_path}/{date_key}")
        if isinstance(payload, list):
            return payload
        return []
