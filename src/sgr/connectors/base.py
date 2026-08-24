from __future__ import annotations

import ssl
from typing import Any
from urllib.parse import urlsplit

import httpx


class APIRequestError(RuntimeError):
    """A provider failure that intentionally excludes request credentials."""


class APIConnector:
    def __init__(
        self,
        base_url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
        verify: ssl.SSLContext | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.headers = headers or {}
        self.timeout = timeout
        # httpx otherwise prefers its bundled CA file. A verified system
        # context supports managed Windows roots without disabling hostname or
        # certificate validation.
        self.verify = verify if verify is not None else ssl.create_default_context()

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
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.verify) as client:
            response = await client.get(url, params=params, headers=headers)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                # httpx includes the fully rendered URL in this exception. Some
                # providers require credentials in query parameters, so never
                # propagate it to a CLI, log, or traceback.
                raise APIRequestError(
                    f"GET {urlsplit(url).path} failed with HTTP status "
                    f"{error.response.status_code}."
                ) from None
            return response.json()
