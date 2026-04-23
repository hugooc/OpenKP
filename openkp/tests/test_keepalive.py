"""Tests for SessionStore's keepalive loop."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from openkp.scrapers.auth import KaiserSession
from openkp.scrapers.session import SessionStore


def _make_session() -> KaiserSession:
    return KaiserSession(cookies=[{"name": "k", "value": "v", "domain": ".kp.org", "path": "/"}], user_agent="ua")


def _alive_response() -> httpx.Response:
    return httpx.Response(200, text="ok")


def _ping_response() -> httpx.Response:
    return httpx.Response(
        302,
        headers={"Location": "https://identityauth.kaiserpermanente.org/as/authorization.oauth2"},
    )


class _Store(SessionStore):
    """Test subclass that skips real auth and runs the loop fast."""

    keepalive_interval_seconds = 0.01

    def __init__(self, session: KaiserSession) -> None:
        super().__init__(Path("/tmp"), "u", "p")
        self._preset = session

    async def _authenticate(self) -> KaiserSession:  # type: ignore[override]
        return self._preset


@pytest.mark.asyncio
async def test_keepalive_starts_after_auth_and_pulses():
    store = _Store(_make_session())
    pulse = AsyncMock(return_value=True)
    with patch.object(store, "_pulse_keepalive", pulse):
        await store.get_session()
        # Give the loop a chance to pulse several times.
        await asyncio.sleep(0.05)
        await store.invalidate()

    assert pulse.await_count >= 2, f"expected multiple pulses, got {pulse.await_count}"
    assert store._session is None
    assert store._keepalive_task is None or store._keepalive_task.done()


@pytest.mark.asyncio
async def test_keepalive_clears_session_when_pulse_reports_dead():
    store = _Store(_make_session())
    pulse = AsyncMock(return_value=False)  # every pulse says session is dead
    with patch.object(store, "_pulse_keepalive", pulse):
        await store.get_session()
        # One interval is enough — the first failed pulse should clear the session.
        for _ in range(50):
            await asyncio.sleep(0.005)
            if store._session is None:
                break

    assert store._session is None
    assert pulse.await_count >= 1


@pytest.mark.asyncio
async def test_keepalive_survives_exception_from_pulse():
    """A raised exception should be treated as 'dead', not leak out of the task."""
    store = _Store(_make_session())
    pulse = AsyncMock(side_effect=RuntimeError("network boom"))
    with patch.object(store, "_pulse_keepalive", pulse):
        await store.get_session()
        for _ in range(50):
            await asyncio.sleep(0.005)
            if store._session is None:
                break

    assert store._session is None  # exception cleared the session, didn't crash


@pytest.mark.asyncio
async def test_pulse_keepalive_reads_200_as_alive():
    store = _Store(_make_session())
    session = _make_session()

    # Patch httpx.AsyncClient.get on the instance inside the context manager.
    with patch("openkp.scrapers.session.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_alive_response())
        client_cls.return_value.__aenter__.return_value = client

        ok = await store._pulse_keepalive(session, 1)

    assert ok is True


@pytest.mark.asyncio
async def test_pulse_keepalive_reads_ping_redirect_as_dead():
    store = _Store(_make_session())
    session = _make_session()

    with patch("openkp.scrapers.session.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_ping_response())
        client_cls.return_value.__aenter__.return_value = client

        ok = await store._pulse_keepalive(session, 1)

    assert ok is False


@pytest.mark.asyncio
async def test_invalidate_cancels_keepalive():
    store = _Store(_make_session())
    pulse = AsyncMock(return_value=True)
    with patch.object(store, "_pulse_keepalive", pulse):
        await store.get_session()
        task = store._keepalive_task
        assert task is not None and not task.done()
        await store.invalidate()
        # Give the event loop a tick to process the cancellation.
        await asyncio.sleep(0)

    assert task.cancelled() or task.done()
    assert store._keepalive_task is None
