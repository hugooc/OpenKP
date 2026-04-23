"""Session lifecycle for OpenKP.

Wraps `KaiserSession` with persistence, keepalive, and re-auth logic.

Responsibilities:
- Persist cookies to `data_dir/session.json` (chmod 0600) after an interactive
  login so we can reuse them silently on next startup.
- On startup: load the persisted cookies, probe /mychartcn/keepalive.asp via
  httpx. If the probe succeeds, we're alive without a browser. If it fails,
  fall back to `login_interactive`.
- Pulse /mychartcn/keepalive.asp in the background so Kaiser doesn't kill the
  session between MCP tool calls.
- Detect expiry (401 / 302-to-Ping on any request) and clear state.
- Expose `get_session()` to the rest of the code as the single source of truth.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from openkp.scrapers.auth import KaiserSession, login_interactive

logger = logging.getLogger(__name__)

KP_BASE = "https://healthy.kaiserpermanente.org"
PING_REDIRECT_HOST = "identityauth.kaiserpermanente.org"
KEEPALIVE_PATH = "/mychartcn/keepalive.asp"
# MyChart's UI pulses every ~30s. Matching that avoids looking anomalous.
KEEPALIVE_INTERVAL_SECONDS = 30.0


def cookies_to_httpx(cookies: list[dict]) -> httpx.Cookies:
    """Convert Playwright's cookie shape into an httpx jar."""
    jar = httpx.Cookies()
    for c in cookies:
        jar.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))
    return jar


class SessionStore:
    """Singleton-ish session holder. One instance per MCP server process."""

    # Override in tests to avoid real 30s waits.
    keepalive_interval_seconds: float = KEEPALIVE_INTERVAL_SECONDS

    def __init__(self, data_dir: Path, username: str, password: str) -> None:
        self.data_dir = data_dir
        self.username = username
        self.password = password
        self._session: KaiserSession | None = None
        self._lock = asyncio.Lock()
        self._keepalive_task: asyncio.Task[None] | None = None

    async def get_session(self) -> KaiserSession:
        """Return a live session, authenticating if needed."""
        async with self._lock:
            if self._session is None:
                self._session = await self._authenticate()
                self._ensure_keepalive_running()
            return self._session

    async def invalidate(self) -> None:
        """Call when a request returns 401 or hits the Ping login redirect."""
        async with self._lock:
            self._session = None
            self._stop_keepalive()
            self._remove_persisted_session()

    def _remove_persisted_session(self) -> None:
        try:
            self._session_file().unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("Could not delete persisted session: %s", exc)

    async def _authenticate(self) -> KaiserSession:
        """Try the persisted session first, fall back to interactive login.

        Silent refresh used to open a headless Playwright probe, but Ping
        invalidates the session on a headed→headless fingerprint change
        (see auth.md section 9). Persisting cookies to disk and probing via
        httpx avoids that entirely: httpx has no PingOne-visible fingerprint,
        and cookies captured by a headed browser work fine when replayed.
        """
        session = self._load_persisted_session()
        if session is not None and await self._probe_session_alive(session):
            logger.info("Persisted session is alive, reusing")
            return session

        if session is not None:
            logger.info("Persisted session is dead, launching interactive login")
        else:
            logger.info("No persisted session, launching interactive login")

        session = await login_interactive(self.data_dir, self.username, self.password)
        try:
            session.save_to(self._session_file())
        except OSError as exc:
            logger.warning("Could not persist session to disk: %s", exc)
        return session

    def _session_file(self) -> Path:
        return self.data_dir / "session.json"

    def _load_persisted_session(self) -> KaiserSession | None:
        return KaiserSession.load_from(self._session_file())

    async def _probe_session_alive(self, session: KaiserSession) -> bool:
        """Pulse keepalive.asp with the given session's cookies."""
        try:
            return await self._pulse_keepalive(session, 0)
        except Exception as exc:
            logger.warning("Probe for persisted session raised %s, treating as dead", exc)
            return False

    # --- keepalive ---------------------------------------------------------

    def _ensure_keepalive_running(self) -> None:
        """Start the keepalive task if it isn't already running."""
        if self._keepalive_task is not None and not self._keepalive_task.done():
            return
        self._keepalive_task = asyncio.create_task(
            self._keepalive_loop(), name="openkp-keepalive"
        )
        logger.info(
            "Keepalive loop started (interval %.0fs)", self.keepalive_interval_seconds
        )

    def _stop_keepalive(self) -> None:
        """Cancel the keepalive task if running. Safe to call from anywhere except the task itself."""
        task = self._keepalive_task
        self._keepalive_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _keepalive_loop(self) -> None:
        """Pulse keepalive.asp until cancelled or the pulse fails."""
        counter = 0
        while True:
            try:
                await asyncio.sleep(self.keepalive_interval_seconds)
            except asyncio.CancelledError:
                logger.info("Keepalive loop cancelled")
                raise

            counter += 1
            session_snapshot = self._session
            if session_snapshot is None:
                # Session was cleared between iterations; nothing to pulse.
                logger.debug("Keepalive tick #%d: no session, exiting loop", counter)
                return

            try:
                alive = await self._pulse_keepalive(session_snapshot, counter)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pulse must never kill the loop silently
                logger.warning("Keepalive pulse #%d raised %s, treating as dead", counter, exc)
                alive = False

            if not alive:
                # Clear session under the lock so the next get_session() re-auths.
                # Don't call invalidate() — that would cancel this task mid-run.
                async with self._lock:
                    self._session = None
                    # Null out so _ensure_keepalive_running restarts fresh next time.
                    self._keepalive_task = None
                    self._remove_persisted_session()
                return

    async def _pulse_keepalive(self, session: KaiserSession, counter: int) -> bool:
        """Hit /mychartcn/keepalive.asp. Returns True on 200, False otherwise."""
        url = f"{KP_BASE}{KEEPALIVE_PATH}?cnt={counter}"
        headers = {
            "Accept": "*/*",
            "Referer": f"{KP_BASE}/mychartcn/Home?lang=en-US",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": session.user_agent,
        }
        async with httpx.AsyncClient(
            cookies=cookies_to_httpx(session.cookies),
            headers=headers,
            follow_redirects=False,
            timeout=15.0,
        ) as client:
            response = await client.get(url)

        if response.status_code == 200:
            logger.debug("Keepalive #%d ok", counter)
            return True

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location", "")
            if PING_REDIRECT_HOST in location:
                logger.info("Keepalive #%d bounced to Ping — session expired", counter)
                return False

        logger.info(
            "Keepalive #%d got unexpected status %d — treating as dead",
            counter,
            response.status_code,
        )
        return False
