"""Tests for KaiserRequest: URL resolution, retry-on-expiry, header forwarding."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openkp.scrapers.auth import KaiserSession
from openkp.scrapers.request import KP_BASE, KaiserRequest


def _session() -> KaiserSession:
    return KaiserSession(
        cookies=[{"name": "k", "value": "v", "domain": ".kp.org", "path": "/"}],
        user_agent="ua",
    )


def _make_store() -> MagicMock:
    store = MagicMock()
    store.get_session = AsyncMock(return_value=_session())
    store.invalidate = AsyncMock()
    return store


def _patch_http(responses: list[httpx.Response]) -> AsyncMock:
    """Patch httpx.AsyncClient so each .request() call returns the next queued response.

    Returns the mock client so tests can inspect call args.
    """
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(side_effect=responses)
    patched = patch("openkp.scrapers.request.httpx.AsyncClient")
    client_cls = patched.start()
    client_cls.return_value.__aenter__.return_value = mock_client
    client_cls.return_value.__aexit__.return_value = None
    return mock_client, patched


@pytest.mark.asyncio
async def test_200_passes_through_no_retry():
    store = _make_store()
    mock_client, p = _patch_http([httpx.Response(200, text="ok")])
    try:
        resp = await KaiserRequest(store).get("/mychartcn/keepalive.asp")
    finally:
        p.stop()

    assert resp.status_code == 200
    mock_client.request.assert_awaited_once()
    store.invalidate.assert_not_awaited()
    # get_session was called exactly once
    store.get_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_401_triggers_invalidate_and_retry():
    store = _make_store()
    mock_client, p = _patch_http([httpx.Response(401), httpx.Response(200, text="ok")])
    try:
        resp = await KaiserRequest(store).get("/mychartcn/keepalive.asp")
    finally:
        p.stop()

    assert resp.status_code == 200
    assert mock_client.request.await_count == 2
    store.invalidate.assert_awaited_once()
    assert store.get_session.await_count == 2  # initial + post-invalidate


@pytest.mark.asyncio
async def test_ping_redirect_triggers_retry():
    store = _make_store()
    redirect = httpx.Response(
        302,
        headers={"Location": "https://identityauth.kaiserpermanente.org/as/authorization.oauth2"},
    )
    mock_client, p = _patch_http([redirect, httpx.Response(200, text="ok")])
    try:
        resp = await KaiserRequest(store).get("/mychartcn/keepalive.asp")
    finally:
        p.stop()

    assert resp.status_code == 200
    assert mock_client.request.await_count == 2
    store.invalidate.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_ping_redirect_is_passed_through():
    """302 to somewhere other than Ping is a normal redirect, not an expiry signal."""
    store = _make_store()
    redirect = httpx.Response(
        302, headers={"Location": "https://healthy.kaiserpermanente.org/elsewhere"}
    )
    mock_client, p = _patch_http([redirect])
    try:
        resp = await KaiserRequest(store).get("/mychartcn/keepalive.asp")
    finally:
        p.stop()

    assert resp.status_code == 302
    mock_client.request.assert_awaited_once()
    store.invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_relative_path_gets_kp_base_prefix():
    store = _make_store()
    mock_client, p = _patch_http([httpx.Response(200)])
    try:
        await KaiserRequest(store).get("/mychartcn/foo")
    finally:
        p.stop()

    call_args = mock_client.request.await_args
    assert call_args.args[1] == f"{KP_BASE}/mychartcn/foo"


@pytest.mark.asyncio
async def test_absolute_url_is_used_as_is():
    store = _make_store()
    mock_client, p = _patch_http([httpx.Response(200)])
    try:
        await KaiserRequest(store).get("https://apims.kaiserpermanente.org/v1/thing")
    finally:
        p.stop()

    call_args = mock_client.request.await_args
    assert call_args.args[1] == "https://apims.kaiserpermanente.org/v1/thing"


@pytest.mark.asyncio
async def test_kwargs_are_forwarded_to_httpx():
    store = _make_store()
    mock_client, p = _patch_http([httpx.Response(200)])
    custom_headers = {"X-apiKey": "some-key", "Accept": "application/json"}
    try:
        await KaiserRequest(store).get("/foo", headers=custom_headers, params={"q": "1"})
    finally:
        p.stop()

    call_args = mock_client.request.await_args
    assert call_args.kwargs["headers"] == custom_headers
    assert call_args.kwargs["params"] == {"q": "1"}


@pytest.mark.asyncio
async def test_post_method_is_forwarded():
    store = _make_store()
    mock_client, p = _patch_http([httpx.Response(200)])
    try:
        await KaiserRequest(store).post("/foo", json={"hi": 1})
    finally:
        p.stop()

    call_args = mock_client.request.await_args
    assert call_args.args[0] == "POST"
