from __future__ import annotations

from typing import Any
import httpx


class APIConnector:
    def __init__(self, base_url: str, headers: dict[str, str] | None = None, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.headers = headers or {}
        self.timeout = timeout

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
