"""Tests for scrapers/csrf.py — the shared anti-forgery token helper used by
profile's CareTeam/Load and every messages.py endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openkp.scrapers.csrf import CSRF_PATH, fetch_csrf_token

_FAKE_REFERER = "https://healthy.kaiserpermanente.org/mychartcn/some/page"


def _csrf_html(token: str) -> str:
    return f'<input name="__RequestVerificationToken" type="hidden" value="{token}" />'


def _make_store() -> MagicMock:
    from openkp.scrapers.auth import KaiserSession

    store = MagicMock()
    store.get_session = AsyncMock(
        return_value=KaiserSession(
            cookies=[{"name": "k", "value": "v", "domain": ".kp.org", "path": "/"}],
            user_agent="ua",
        )
    )
    store.invalidate = AsyncMock()
    return store


def _bind_request(responses: list[httpx.Response]) -> list[httpx.Response]:
    req = httpx.Request("GET", f"https://healthy.kaiserpermanente.org{CSRF_PATH}")
    for r in responses:
        r.request = req
    return responses


def _patch_http(responses: list[httpx.Response]):
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(side_effect=_bind_request(responses))
    patched = patch("openkp.scrapers.request.httpx.AsyncClient")
    client_cls = patched.start()
    client_cls.return_value.__aenter__.return_value = mock_client
    client_cls.return_value.__aexit__.return_value = None
    return mock_client, patched


@pytest.mark.asyncio
async def test_fetch_csrf_token_extracts_token_value():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([httpx.Response(200, text=_csrf_html("abc-123-XYZ"))])
    try:
        token = await fetch_csrf_token(KaiserRequest(store), referer=_FAKE_REFERER)
    finally:
        p.stop()

    assert token == "abc-123-XYZ"
    call = mock_client.request.await_args
    assert call.args[0] == "GET"
    assert CSRF_PATH in call.args[1]
    # noCache query param present
    assert "noCache" in call.kwargs.get("params", {})
    # Referer passed through unchanged
    assert call.kwargs["headers"]["Referer"] == _FAKE_REFERER


@pytest.mark.asyncio
async def test_fetch_csrf_token_passes_different_referer_per_caller():
    """Each endpoint family passes its own Referer so the CSRF fetch looks
    like it came from the page that will post the token."""
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, text=_csrf_html("t1")),
        httpx.Response(200, text=_csrf_html("t2")),
    ])
    try:
        await fetch_csrf_token(KaiserRequest(store), referer="https://example.com/page-a")
        await fetch_csrf_token(KaiserRequest(store), referer="https://example.com/page-b")
    finally:
        p.stop()

    call1 = mock_client.request.await_args_list[0]
    call2 = mock_client.request.await_args_list[1]
    assert call1.kwargs["headers"]["Referer"] == "https://example.com/page-a"
    assert call2.kwargs["headers"]["Referer"] == "https://example.com/page-b"


@pytest.mark.asyncio
async def test_fetch_csrf_token_raises_when_input_missing():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([httpx.Response(200, text="<div>no token here</div>")])
    try:
        with pytest.raises(ValueError, match="CSRF token"):
            await fetch_csrf_token(KaiserRequest(store), referer=_FAKE_REFERER)
    finally:
        p.stop()


@pytest.mark.asyncio
async def test_fetch_csrf_token_propagates_http_error():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([httpx.Response(500, text="boom")])
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_csrf_token(KaiserRequest(store), referer=_FAKE_REFERER)
    finally:
        p.stop()
