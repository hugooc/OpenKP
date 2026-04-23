# OpenKP

A patient-directed MCP server that bridges Claude and Kaiser Permanente's patient portal.

Inspired by [Open Record](https://github.com/Fan-Pier-Labs/openrecord) by Ryan Hughes / Fan Pier Labs. Open Record targets vanilla Epic MyChart, which Kaiser is not. OpenKP implements the same idea (let Claude drive your medical record through MCP) against Kaiser's Ping-fronted portal.

**Status:** auth complete. The MCP server runs and exposes `ping`, `whoami`, and `session_check` tools. `session_check` logs into Kaiser via a persistent Chromium profile, captures cookies, and confirms an authenticated endpoint responds. Real read tools (`list_medications`, `list_lab_results`, etc.) are the next phase.

## Why this exists

Kaiser's FHIR patient API is read-only and limited to USCDI. That's by design. Your record contains more than what the API surfaces, and you have every right to act on it (refill meds, message your team, view labs) through any interface you choose, including an AI agent. OpenKP is that interface.

This is a **personal research tool**. It logs in with your own credentials, on your own machine, and nothing leaves your laptop except requests to Kaiser.

## How it works

```
Claude Desktop
      │  (MCP over stdio)
      ▼
openkp.mcp_server  (FastMCP)
      │
      ▼
scrapers/request.py  (authenticated HTTP)
      │
      ▼
scrapers/session.py  ← scrapers/auth.py  (Playwright + Ping OAuth)
      │
      ▼
healthy.kaiserpermanente.org / Epic endpoints behind Ping
```

Authentication uses a persistent Chromium profile driven by Playwright. First login is interactive (you enter MFA). Subsequent sessions are silent as long as the device-trust cookie holds. Once authenticated, cookies are handed to an `httpx` client for fast JSON calls.

## Setup

You need Python 3.11+ and a Mac. From the workspace root (`~/OpenKP`):

```bash
bash scripts/setup-dev.sh
```

That script creates `openkp/.venv`, installs OpenKP in editable mode with dev extras, installs the Playwright Chromium browser, and runs the tests. Idempotent. Re-run it after a dependency bump.

Then configure credentials:

```bash
cd openkp
cp .env.example .env
# Edit .env: set KP_USERNAME. Then store the password in the OS keychain:
python -c 'import keyring; keyring.set_password("openkp", "YOUR_USERNAME", "YOUR_PASSWORD")'
```

## Verify the MCP server

From `~/OpenKP/openkp`:

```bash
source .venv/bin/activate
pytest
openkp   # stdio mode; Ctrl+C to stop
```

## Connect Claude Desktop

Open `~/Library/Application Support/Claude/claude_desktop_config.json` and merge this into your `mcpServers` block (creating it if it doesn't exist):

```json
{
  "mcpServers": {
    "openkp": {
      "command": "/Users/testuser/OpenKP/openkp/.venv/bin/openkp"
    }
  }
}
```

Fully quit and relaunch Claude Desktop (not just close the window). In a new chat, ask:

> Use the openkp tools to ping and tell me who I am.

You should see `pong` and your configured KP username. If you see that, the Phase 0 pipe is clean and we can move on to real auth.

## Project layout

```
openkp/
├── pyproject.toml          ← deps, entry point, build config
├── .env.example            ← template for credentials
├── README.md               ← this file
├── src/openkp/
│   ├── __init__.py
│   ├── config.py           ← credential loader (env + keychain)
│   ├── mcp_server.py       ← FastMCP server, tool registry
│   └── scrapers/
│       ├── __init__.py
│       ├── auth.py         ← Ping OAuth login via Playwright
│       ├── session.py      ← persistence + keepalive + refresh
│       └── request.py      ← authenticated HTTP wrapper with retry-on-expiry
├── fake_kp/                ← mock portal for dev (TODO)
└── tests/
    ├── test_config.py
    ├── test_keepalive.py
    ├── test_request.py
    └── test_session_persistence.py
```

## Roadmap

See `DESIGN.md` for the full per-phase spec.

**Phase 0 — scaffold.** ✅ MCP server runs, Claude can connect.

**Phase 1 — auth.** ✅ Interactive Ping/OAuth login via Playwright, cookie persistence at `~/.openkp/session.json`, httpx-based silent refresh on startup, 30 s keepalive loop holds the session open between tool calls.

**Phase 2 — read-only MVP.** Four read tools: `list_medications`, `list_lab_results`, `list_messages`, `list_visits`. Enough to prove the thesis.

**Phase 3 — writes.** `send_message`, `request_refill`, `book_appointment`.

**Phase 4 — polish.** Imaging, documents, letters. Consider packaging as a `.dxt` extension for one-click Claude Desktop install.

## Privacy

- Credentials live in your OS keychain or in a gitignored `.env` file. The repo enforces this via `.gitignore`.
- Browser profile and session cookies live in `~/.openkp`. Nothing is uploaded anywhere.
- PHI passes only between Kaiser, OpenKP on your Mac, and Claude Desktop on your Mac. No OpenKP-owned server exists.

## Legal

- Kaiser's terms of service almost certainly prohibit automated access. Personal use on your own account is a gray zone. This is a research tool.
- Open Record's license is source-available and prohibits commercial redistribution. OpenKP is MIT-licensed and does not use Open Record's code, only its public architecture as inspiration.

## Credits

- Ryan Hughes / Fan Pier Labs for [Open Record](https://github.com/Fan-Pier-Labs/openrecord), which demonstrated the idea.
- Brendan Keeler for ["The Scrapers At MyChart's Gate"](https://healthapiguy.substack.com/p/the-scrapers-at-mycharts-gate).
