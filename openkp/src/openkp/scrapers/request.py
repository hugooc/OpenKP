"""Authenticated HTTP client for Kaiser Permanente endpoints.

Every endpoint module (labs.py, medications.py, ...) calls `make_request`
instead of httpx directly. This keeps cookie injection, expiry handling,
and retries in one place.

Modeled on Open Record's scrapers/myChart/myChartRequest.ts.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from openkp.scrapers.session import KP_BASE, PING_REDIRECT_HOST, SessionStore, cookies_to_httpx

logger = logging.getLogger(__name__)


class KaiserRequest:
    """Thin wrapper around httpx that refreshes the session on 401 / Ping redirect."""

    def __init__(self, session_store: SessionStore) -> None:
        self.session_store = session_store

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._request("POST", path, **kwargs)

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        session = await self.session_store.get_session()
        url = path if path.startswith("http") else KP_BASE + path

        async with httpx.AsyncClient(
            cookies=cookies_to_httpx(session.cookies),
            headers={"User-Agent": session.user_agent},
            follow_redirects=False,
            timeout=30.0,
        ) as client:
            response = await client.request(method, url, **kwargs)

            if _is_session_expired(response):
                logger.info("Session expired (%s), invalidating", response.status_code)
                await self.session_store.invalidate()
                session = await self.session_store.get_session()
                # One retry with the fresh session
                async with httpx.AsyncClient(
                    cookies=cookies_to_httpx(session.cookies),
                    headers={"User-Agent": session.user_agent},
                    follow_redirects=False,
                    timeout=30.0,
                ) as retry_client:
                    response = await retry_client.request(method, url, **kwargs)

            return response


def _is_session_expired(response: httpx.Response) -> bool:
    """Detect the two common expiry signals: redirect to Ping, or 401."""
    if response.status_code == 401:
        return True
    if response.status_code in (301, 302, 303, 307, 308):
        location = response.headers.get("Location", "")
        if PING_REDIRECT_HOST in location:
            return True
    return False
