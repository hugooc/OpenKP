# OpenKP

A patient-directed MCP server that bridges Claude and Kaiser Permanente's patient portal.

Inspired by [Open Record](https://github.com/Fan-Pier-Labs/openrecord) by Ryan Hughes / Fan Pier Labs. Open Record targets vanilla Epic MyChart, which Kaiser is not. OpenKP implements the same idea (let Claude drive your medical record through MCP) against Kaiser's Ping-fronted portal, with independently written code.

## Status

**Phase 2 (read-only) closed. Phase 3 (writes) in progress.** As of 2026-05-04:

- **22 MCP tools** registered: 3 housekeeping, 17 reads, 2 writes (mail-order refill, non-urgent message).
- **527 tests** passing.
- **NorCal region only** — see "Regional support" below.

Live-verified end-to-end against real Kaiser data: profile, messages, lab results (incl. PDF download), medications, problems, allergies, appointments (upcoming + past), visit notes, AVS, refill *preview*, send-message *preview*, refill order tracking. Commit paths for `request_refill` and `send_message` are unit-tested but not yet exercised live; see the "Write tools — preview vs commit" section.

The repo is private during this phase but tracks public-release readiness — see `docs/release-checklist.md` at the workspace root.

## Regional support

**OpenKP is only tested against Kaiser's Northern California (NorCal) region.** Kaiser operates 8 regions: NorCal, SoCal, Hawaii, Northwest, Colorado, Georgia, Mid-Atlantic, and Washington. They share a common portal front door at `healthy.kaiserpermanente.org`, but region codes, pharmacy endpoints, and data shapes differ. If you're a KP member outside NorCal, expect breakage on at least the medication and refill tools. The architecture leaves room for per-region adapters (see `DESIGN.md` §12), but none exist yet. Issues and HAR captures from other regions are welcome.

## Why this exists

Kaiser's FHIR patient API is read-only and limited to USCDI. That's a policy choice, not a technical necessity. Your record contains more than what the API surfaces, and you have every right to act on it (refill meds, message your team, view labs) through any interface you choose, including an AI agent. OpenKP is that interface.

This is a **personal research tool**. It logs in with your own credentials, on your own machine, and nothing leaves your laptop except requests to Kaiser.

## How it works

```
Claude Desktop
      │  (MCP over stdio)
      ▼
openkp.mcp_server                 (FastMCP)
      │
      ▼
scrapers/<topic>.py               (per-endpoint logic + pydantic models)
      │
      ▼
scrapers/request.py               (authenticated HTTP, retry on session expiry)
      │
      ▼
scrapers/session.py               (cookie persistence + httpx keepalive probe)
      ↑
scrapers/auth.py                  (Playwright + Ping OAuth, interactive on first login)
      │
      ▼
healthy.kaiserpermanente.org       Epic / MyChart endpoints behind Ping
apims.kaiserpermanente.org         pharmacy BFF microservices
```

Authentication uses a persistent Chromium profile driven by Playwright. **First login is interactive** — Chromium pops up, you enter your KP username and password, complete MFA. Subsequent sessions are silent as long as Kaiser's device-trust cookie holds (typically a few weeks). Once authenticated, cookies are handed to an `httpx` client for fast JSON calls.

## Install

You need:

- macOS (tested) or Linux (untested).
- Python 3.11 or newer.
- A Kaiser Permanente NorCal member account.

### 1. Bootstrap the venv (one-time, ~2 minutes)

From the workspace root (`~/OpenKP`):

```bash
bash scripts/setup-dev.sh
```

That creates `openkp/.venv`, installs OpenKP in editable mode with dev extras, downloads the Playwright Chromium binary, and runs the test suite. Idempotent — re-run it any time after a dependency bump.

### 2. Configure credentials (one-time)

```bash
cd openkp
cp .env.example .env
```

Edit `.env` and set `KP_USERNAME` to your kp.org login. Leave `KP_PASSWORD` empty.

Then store the password in your macOS Keychain (more secure than the `.env` file):

```bash
.venv/bin/python -c 'import keyring; keyring.set_password("openkp", input("KP username: "), input("KP password: "))'
```

If you accept the `keyring` prompt for keychain access, you're done. The password is now retrievable by OpenKP's process and nothing else.

### 3. Verify the install (one-time, ~30 seconds)

From `~/OpenKP/openkp`:

```bash
.venv/bin/pytest -q
```

You should see `527 passed`. If anything fails, stop and investigate before going further.

### 4. First authenticated run (one-time, ~2 minutes)

The first MCP call that needs a session will trigger an interactive Chromium login. Run the server in stdio mode and call `session_check`:

```bash
.venv/bin/openkp
```

That blocks waiting for stdio input. From a separate terminal, you can pipe an MCP `tools/call` request, but the easier path is to skip ahead to step 5 and let Claude Desktop drive the first login.

### 5. Connect Claude Desktop

Open `~/Library/Application Support/Claude/claude_desktop_config.json` and merge this into your `mcpServers` block (creating the file if it doesn't exist):

```json
{
  "mcpServers": {
    "openkp": {
      "command": "/Users/<YOUR_USERNAME>/OpenKP/openkp/.venv/bin/openkp"
    }
  }
}
```

Replace `<YOUR_USERNAME>` with your actual macOS username. The path must be absolute.

Fully quit Claude Desktop (Cmd+Q, not just close-window) and relaunch. In a new chat, ask:

> Use openkp's session_check tool.

The first time, a Chromium window pops up showing the kp.org login. Sign in (including MFA). When kp.org's home page loads, OpenKP captures the session and the window closes automatically. Subsequent calls are silent.

## First things to try

Once `session_check` returns `status: alive`, try these prompts in Claude Desktop:

- **"What's my next Kaiser appointment?"** — exercises `list_appointments`.
- **"How many Kaiser appointments did I have in 2025?"** — exercises `list_past_visits` with the `until_iso` cursor.
- **"What did my cardiologist say at my last visit?"** — chains `list_past_visits` → `read_visit_notes` and summarizes.
- **"What labs did I have last month?"** — exercises `list_lab_results`.
- **"Show me my active medications."** — exercises `list_medications`.
- **"Summarize the unread messages in my Kaiser inbox."** — exercises `list_messages` + `read_message`.

For write operations:

- **"Preview a refill for one of my mailable prescriptions."** — uses `request_refill` with the default `confirm=False`. Returns a preview with no Kaiser side effect. Add `confirm=True` only after you've reviewed it.

## Tool inventory

### Housekeeping

| Tool | Description |
| --- | --- |
| `ping` | Smoke test. Returns `pong`. |
| `whoami` | Returns the configured KP username and data dir. Never returns the password. |
| `session_check` | Verifies end-to-end auth. Triggers the interactive Chromium login on first call. |

### Reads

| Tool | Description |
| --- | --- |
| `get_profile` | Demographics, contact info, insurance, PCP, emergency contacts, healthcare agents. |
| `list_messages` | Message-center thread list. Supports `folder`, `search`, `before_iso`, and `deep_search` for archival pagination. |
| `read_message` | One thread in full, all messages, HTML-stripped to plain text. |
| `download_message_attachment` | Saves a message attachment (PDF/JPG/etc.) to `~/.openkp/downloads/`. |
| `list_lab_results` | Lab, imaging, and other test results, newest-first. |
| `read_lab_result` | One result with components, values, reference ranges, abnormal flags, narrative. |
| `download_lab_result_pdf` | Saves the kp.org-generated PDF to disk. Surfaces `generation_in_progress` for lazy-built PDFs. |
| `list_medications` | Active and recent prescriptions with dose, prescriber, refills, copay, mailable / auto-refill flags. |
| `list_problems` | Active diagnoses (name + date noted). Kaiser intentionally hides ICD codes from patients. |
| `list_allergies` | Allergy list. Handles "no known allergies" as a first-class state. |
| `list_appointments` | Upcoming + in-progress visits. |
| `list_past_visits` | Past visits, paginated. Supports `max_pages`, `page_size` (default 50, cap 78), and `until_iso`. |
| `read_visit_notes` | Clinical notes (provider chart notes, progress notes) + rendered After Visit Summary. |
| `download_visit_avs_pdf` | Saves the canonical AVS PDF to `~/.openkp/downloads/`. |
| `track_refill_order` | Read-side companion to `request_refill`. Surfaces order status, shipping, and tracking. |
| `list_message_recipients` | Providers and pools you can message. |
| `list_message_topics` | "Reason for Message" catalog (5 topics). |

### Writes

Write tools follow a **two-call confirm pattern**: the first call (with `confirm=False`, the default) returns a preview without touching Kaiser. The second call (with `confirm=True`) commits. See "Write tools — preview vs commit" below.

| Tool | Description |
| --- | --- |
| `request_refill` | Mail-order prescription refill. v1 supports mail only; local pickup is deferred. |
| `send_message` | Non-urgent medical-advice message to a provider or care team pool. v1 starts new conversations only. |

## Write tools — preview vs commit

Every write tool ships with a hard guard: the default mode is preview-only. You must pass `confirm=True` explicitly to actually commit a change to your Kaiser account. Both the preview and the commit are recorded in `~/.openkp/audit.log` (JSONL format, gitignored, lives outside the repo).

The audit log records:

- **Intent** — what the tool was asked to do (recipient name, medication, etc.). Subject and body of messages are NOT logged.
- **Result** — Kaiser's response on success.
- **Error** — failure mode and reason on the way out.

If you ever want to reconstruct what OpenKP did on your behalf, `tail ~/.openkp/audit.log` is the source of truth.

## Project layout

```
openkp/
├── pyproject.toml              ← deps, entry point, build config
├── .env.example                ← template for credentials
├── README.md                   ← this file
├── LICENSE                     ← MIT
├── src/openkp/
│   ├── __init__.py
│   ├── config.py               ← credential loader (env + keychain)
│   ├── mcp_server.py           ← FastMCP server, all tools registered here
│   └── scrapers/
│       ├── auth.py             ← Ping/OAuth login via Playwright
│       ├── session.py          ← cookie persistence + keepalive
│       ├── request.py          ← authenticated HTTP wrapper
│       ├── csrf.py             ← shared anti-forgery token fetch
│       ├── profile.py          ← demographics + PCP + emergency contacts
│       ├── messages.py         ← message center read + send
│       ├── labs.py             ← lab results + PDF download
│       ├── medications.py      ← prescription list (pharmacy BFF)
│       ├── problems.py         ← active health issues
│       ├── allergies.py        ← allergy list
│       ├── appointments.py     ← upcoming + past visits
│       ├── visit_notes.py      ← clinical notes + AVS
│       └── refill.py           ← mail-order refill commit + tracking
├── scripts/
│   └── recon_*.py              ← per-endpoint reconnaissance scripts
└── tests/
    └── test_*.py               ← 527 tests, mock httpx via _patch_http
```

## Privacy

- Credentials live in your OS keychain or in a gitignored `.env` file. The repo enforces this via `.gitignore`.
- Browser profile and session cookies live in `~/.openkp`. Nothing is uploaded.
- PHI passes only between Kaiser, OpenKP on your Mac, and Claude Desktop on your Mac. No OpenKP-owned server exists.
- HAR captures and recon dumps with PHI live in `docs/research/captures/` (gitignored, never committed).
- Recent session journals (`session-*.md`) live outside the repo entirely, in `~/Desktop/OpenKP Documentation/recon/`.

## Legal

- Kaiser's terms of service almost certainly prohibit automated access. Personal use on your own account is a gray zone. This is a research tool.
- Open Record's license is source-available and prohibits commercial redistribution. OpenKP is MIT-licensed and does not use Open Record's code, only its public architecture as inspiration.

## Credits

- Ryan Hughes / Fan Pier Labs for [Open Record](https://github.com/Fan-Pier-Labs/openrecord), which demonstrated the idea.
- Brendan Keeler for ["The Scrapers At MyChart's Gate"](https://healthapiguy.substack.com/p/the-scrapers-at-mycharts-gate).
