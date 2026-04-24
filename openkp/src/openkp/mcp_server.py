"""OpenKP MCP server.

Exposes Kaiser Permanente patient-portal actions as MCP tools so Claude can
read and (eventually) write to your medical record.

Current state: skeleton. Only the `ping` and `whoami` tools work. Real tools
arrive once the auth layer is live.

Run directly:
    python -m openkp.mcp_server

Or via the installed script:
    openkp

Connect from Claude Desktop by adding this to your MCP config:

    {
      "mcpServers": {
        "openkp": {
          "command": "/path/to/venv/bin/openkp"
        }
      }
    }
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from openkp import __version__
from openkp.config import load_config
from openkp.scrapers.messages import fetch_message, fetch_messages
from openkp.scrapers.profile import fetch_profile
from openkp.scrapers.request import KaiserRequest
from openkp.scrapers.session import SessionStore

logger = logging.getLogger("openkp")

mcp = FastMCP("OpenKP")

_session_store: SessionStore | None = None


def _get_session_store() -> SessionStore:
    """Lazy singleton. Built on first authenticated tool call, reused thereafter."""
    global _session_store
    if _session_store is None:
        cfg = load_config()
        _session_store = SessionStore(cfg.data_dir, cfg.username, cfg.password)
    return _session_store


@mcp.tool()
def ping() -> str:
    """Smoke test. Returns 'pong' if the MCP server is alive."""
    return "pong"


@mcp.tool()
def whoami() -> dict:
    """Return the configured Kaiser username and data directory. Does NOT return the password."""
    cfg = load_config()
    return {
        "username": cfg.username,
        "data_dir": str(cfg.data_dir),
        "version": __version__,
    }


@mcp.tool()
async def session_check() -> dict:
    """Verify end-to-end auth: can we reach an authenticated Kaiser endpoint?

    Triggers the full auth path on first call: silent session refresh if the
    persistent profile is still good, otherwise a visible Chromium window for
    interactive login (including any MFA). Subsequent calls are silent.

    Returns a summary of the probe, never PHI. Use `get_profile` (when it
    lands) for actual user data.
    """
    store = _get_session_store()
    client = KaiserRequest(store)

    probe_path = "/mychartcn/keepalive.asp?cnt=1"
    probe_headers = {
        "Accept": "*/*",
        "Referer": "https://healthy.kaiserpermanente.org/mychartcn/Home?lang=en-US",
        "X-Requested-With": "XMLHttpRequest",
    }

    try:
        response = await client.get(probe_path, headers=probe_headers)
    except Exception as exc:
        logger.exception("session_check failed")
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    session = await store.get_session()
    return {
        "status": "alive" if response.status_code == 200 else "unexpected",
        "probe_url": probe_path,
        "probe_status": response.status_code,
        "cookie_count": len(session.cookies),
    }


@mcp.tool()
async def get_profile() -> dict:
    """Return the patient's profile: demographics, contact info, insurance plans.

    Source: Kaiser's `/mycare/v1.0/user` endpoint, called with the pharmacy
    consumer identity. See ADR-006 for why.

    Returns a dict shaped like the `Profile` pydantic model in
    `openkp.scrapers.profile`. Fields that haven't been mapped yet
    (PCP, emergency contacts) return null / empty list — they'll be
    filled in as more endpoints are captured.

    See `docs/research/endpoints/profile.md`.
    """
    store = _get_session_store()
    client = KaiserRequest(store)
    profile = await fetch_profile(client)
    return profile.model_dump()


@mcp.tool()
async def list_messages(
    folder: str = "inbox",
    search: str | None = None,
    before_iso: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List message threads from the Kaiser message center.

    Args:
      folder: Which folder to list. One of "inbox", "archive", "bookmarked",
        "automated", "appointments". Defaults to "inbox".
      search: Optional search string. Kaiser matches against subject, body,
        and sender.
      before_iso: Pagination cursor. Pass the ISO timestamp of the oldest
        thread from a previous page to fetch older results.
      limit: Max threads to return. Clamped to 50 (Kaiser's per-page max).

    Returns a list of thread summaries, each shaped like the `MessageThread`
    pydantic model in `openkp.scrapers.messages`. The `id` field is the
    thread handle you pass to `read_message`.

    See `docs/research/endpoints/messages.md`.
    """
    store = _get_session_store()
    client = KaiserRequest(store)
    threads = await fetch_messages(
        client,
        folder=folder,
        search=search,
        before_iso=before_iso,
        limit=limit,
    )
    return [t.model_dump() for t in threads]


@mcp.tool()
async def read_message(thread_id: str) -> dict | None:
    """Read a single message thread in full, including every message's body.

    Args:
      thread_id: The `id` field from a `list_messages` result.

    Returns a dict shaped like the `MessageThreadDetail` pydantic model. The
    `messages` array is ordered most-recent-first per Kaiser's convention.
    Message bodies are HTML-stripped to plain text. Returns `None` if the
    thread cannot be found.

    See `docs/research/endpoints/messages.md`.
    """
    store = _get_session_store()
    client = KaiserRequest(store)
    thread = await fetch_message(client, thread_id)
    return thread.model_dump() if thread else None


# --- TODO: remaining Phase 2 read tools ----------------------------------------
# - list_medications()
# - list_allergies()
# - list_problems()
# - list_lab_results(since: str | None = None)
# - list_visits(limit: int = 10)
#
# Phase 3 writes:
# - send_message(to, subject, body)
# - reply_to_message(message_id, body)
# - request_refill(medication_id)
# - book_appointment(provider_id, slot_id)
# ---------------------------------------------------------------------------------


def main() -> None:
    """CLI entry point. Called by the `openkp` script defined in pyproject.toml."""
    cfg = load_config()
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("OpenKP %s starting MCP server (stdio)", __version__)
    mcp.run()


if __name__ == "__main__":
    main()
