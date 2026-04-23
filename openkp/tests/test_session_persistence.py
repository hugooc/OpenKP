"""Tests for KaiserSession persistence and SessionStore._authenticate's disk path."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from openkp.scrapers.auth import KaiserSession
from openkp.scrapers.session import SessionStore


def _make_session() -> KaiserSession:
    return KaiserSession(
        cookies=[{"name": "k", "value": "v", "domain": ".kp.org", "path": "/"}],
        user_agent="test-ua",
    )


def test_save_and_load_roundtrip(tmp_path: Path):
    path = tmp_path / "session.json"
    original = _make_session()
    original.save_to(path)

    loaded = KaiserSession.load_from(path)
    assert loaded == original


def test_save_uses_0600_permissions(tmp_path: Path):
    path = tmp_path / "session.json"
    _make_session().save_to(path)

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_save_creates_parent_directory(tmp_path: Path):
    path = tmp_path / "nested" / "dir" / "session.json"
    _make_session().save_to(path)
    assert path.exists()


def test_load_returns_none_when_missing(tmp_path: Path):
    assert KaiserSession.load_from(tmp_path / "nope.json") is None


def test_load_returns_none_when_corrupt(tmp_path: Path):
    path = tmp_path / "session.json"
    path.write_text("{not valid json")
    assert KaiserSession.load_from(path) is None


def test_load_returns_none_when_schema_wrong(tmp_path: Path):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"something_else": 1}))
    assert KaiserSession.load_from(path) is None


class _Store(SessionStore):
    """Test subclass that mocks interactive login."""

    keepalive_interval_seconds = 3600  # don't pulse in these tests

    def __init__(self, data_dir: Path, login_result: KaiserSession) -> None:
        super().__init__(data_dir, "u", "p")
        self._login_result = login_result
        self.login_called = 0


@pytest.mark.asyncio
async def test_authenticate_reuses_live_persisted_session(tmp_path: Path):
    persisted = _make_session()
    persisted.save_to(tmp_path / "session.json")

    fresh = _make_session()
    store = _Store(tmp_path, login_result=fresh)

    async def fake_login(*args, **kwargs):
        store.login_called += 1
        return fresh

    with patch("openkp.scrapers.session.login_interactive", side_effect=fake_login):
        with patch.object(store, "_probe_session_alive", AsyncMock(return_value=True)):
            session = await store.get_session()

    assert session == persisted
    assert store.login_called == 0


@pytest.mark.asyncio
async def test_authenticate_falls_back_to_login_when_persisted_session_dead(tmp_path: Path):
    dead = _make_session()
    dead.save_to(tmp_path / "session.json")

    fresh = KaiserSession(
        cookies=[{"name": "new", "value": "x", "domain": ".kp.org", "path": "/"}],
        user_agent="fresh-ua",
    )
    store = _Store(tmp_path, login_result=fresh)

    async def fake_login(*args, **kwargs):
        store.login_called += 1
        return fresh

    with patch("openkp.scrapers.session.login_interactive", side_effect=fake_login):
        with patch.object(store, "_probe_session_alive", AsyncMock(return_value=False)):
            session = await store.get_session()

    assert session == fresh
    assert store.login_called == 1
    # Fresh session should be persisted to disk for the next run.
    on_disk = KaiserSession.load_from(tmp_path / "session.json")
    assert on_disk == fresh


@pytest.mark.asyncio
async def test_authenticate_falls_back_to_login_when_no_persisted_session(tmp_path: Path):
    fresh = _make_session()
    store = _Store(tmp_path, login_result=fresh)

    async def fake_login(*args, **kwargs):
        store.login_called += 1
        return fresh

    with patch("openkp.scrapers.session.login_interactive", side_effect=fake_login):
        session = await store.get_session()

    assert session == fresh
    assert store.login_called == 1
    assert (tmp_path / "session.json").exists()


@pytest.mark.asyncio
async def test_invalidate_removes_persisted_session(tmp_path: Path):
    _make_session().save_to(tmp_path / "session.json")
    store = _Store(tmp_path, login_result=_make_session())
    await store.invalidate()
    assert not (tmp_path / "session.json").exists()
