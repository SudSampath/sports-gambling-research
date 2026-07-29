from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx


class APIConnector:
    def __init__(self, base_url: str, headers: dict[str, str] | None = None, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.headers = headers or {}
        self.timeout = timeout

    def request_headers(self, method: str, path: str) -> dict[str, str]:
        """Headers for a single request.

        Static by default. Subclasses override this when authentication depends on
        the request itself -- Kalshi signs the timestamp, method, and path, so its
        headers cannot be built once and reused.

        ``path`` is the full URL path with the query string excluded.
        """
        return self.headers

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        # Query parameters stay out of `url` so the signed path excludes them.
        headers = self.request_headers("GET", urlsplit(url).path)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            return response.json()
