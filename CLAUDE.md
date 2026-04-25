# OpenKP — notes for Claude Code

This file is auto-loaded by Claude Code when it opens `~/OpenKP/`. It's the on-ramp. Read it first, then follow the pointers into the real docs.

## What OpenKP is

A local MCP server that bridges Claude and Kaiser Permanente's patient portal. Single-user, runs on Hugo's Mac. All credentials and PHI stay on the machine. MIT licensed. No hosted service. See `DESIGN.md` §1-2 for the full "why."

## v1 audience and distribution

OpenKP v1 ships as an open-source GitHub project for **technically-curious KP members and patient-advocacy peers** — people who have Claude Code installed (or will install it) and can follow a Claude-Code-guided setup. We are deliberately not building a non-technical-user installer in v1. The `.dxt` + bundled-runtime + GUI-credential-entry work is parked at Phase 4.5 and only happens if real demand emerges.

What this means for current work:

- Keep the architecture unchanged. Local-first, MCP-over-stdio, Mac-first is fine.
- The README must read well for a curious human AND be structured enough for Claude Code to walk a user through install end-to-end.
- Error messages should be clear, but they don't need to be tuned for non-technical users yet.
- Lead positioning with the CAIHL frame: patient-directed AI on patient-owned data, not "AI reads my chart."

See `DESIGN.md` §1 (audience), §5 (Phase 4 / 4.5), §10 (distribution strategy).

## Current state (2026-04-24)

- **Phase 0 scaffold:** complete.
- **Phase 1 auth:** complete. Silent session reuse via `~/.openkp/session.json` + httpx probe to `/mychartcn/keepalive.asp`. Interactive first-run Chromium, silent after. See ADR-005 and `docs/recon/session-2.md`.
- **Phase 2 read tools:** in progress.
  - `get_profile` ✅ shipped + live-verified. Demographics, contact info, insurance plans, PCP. `emergency_contacts` still a structured placeholder. See `docs/recon/session-4.md`.
  - `list_messages` + `read_message` ✅ shipped + live-verified. Message center list, single-thread read, search. See `docs/recon/session-5.md`.
  - `list_lab_results` + `read_lab_result` + `download_lab_result_pdf` ✅ shipped + live-verified. Test results (labs, imaging, cardiac device reports) plus PDF download to `~/.openkp/downloads/`. The PDF tool surfaces four statuses: `downloaded`, `generation_in_progress` (Kaiser builds large PDFs on demand, retry in ~30s), `no_pdf_available` (no doc exists), `error`. See `docs/research/endpoints/labs.md` and `docs/recon/session-7.md`.
  - Next: `list_medications`, then `list_problems`, then `list_allergies`, then finish `emergency_contacts` on `get_profile`.
- **Phase 3 write tools:** queued.

**Tests:** 169 passing. Run with `.venv/bin/pytest -q` from `openkp/`.

## Read these first

- `DESIGN.md` — vision, principles, architecture, roadmap, tool inventory, safety patterns. Single source of truth.
- `docs/recon/session-3.md` — what we just shipped, why, and where to pick up.
- `docs/adr/README.md` — architectural decisions index. ADRs 001-006 live here.
- `docs/research/endpoints/` — per-endpoint request/response maps. Start with `profile.md`.

## Work pattern for a new read tool

Per DESIGN.md §5 and the shape of `scrapers/profile.py`:

1. Navigate to the page in Chrome DevTools, capture a focused HAR → `docs/research/captures/kp-<topic>-N.har`.
2. Write the endpoint map in `docs/research/endpoints/<topic>.md`.
3. Implement `openkp/src/openkp/scrapers/<topic>.py` using `KaiserRequest`.
4. Parse response into a pydantic model. Parser must never raise on missing fields — return partial data with nulls.
5. Register the MCP tool in `openkp/src/openkp/mcp_server.py`.
6. Add tests in `openkp/tests/test_<topic>.py` modeled on `test_profile.py`. Mock `httpx.AsyncClient` via `_patch_http`. Always bind a `request` to mocked responses so `raise_for_status()` works.
7. Run `.venv/bin/pytest -q`.
8. Hugo restarts Claude Desktop to pick up the new MCP tool. Call it live to verify.
9. Record the session in `docs/recon/session-N.md`.

## Code conventions

- Python 3.11+. FastMCP, httpx, Playwright, pydantic, keyring.
- Four-layer scraper architecture: `auth.py` → `session.py` → `request.py` → `mcp_server.py`. Endpoint modules (`profile.py`, `labs.py`, ...) sit next to the core layers.
- MCP tool returns are `dict` (not pydantic models) — use `.model_dump()`.
- No PHI in logs. No PHI in error messages returned from MCP tools.
- No `em dashes` or `semicolons` in prose. Short paragraphs. Contractions are fine.
- Never mention Claude Code's implementation or internal tooling to Hugo in docs or comments.

## Key endpoint facts (so you don't re-discover them)

- **Session probe:** `/mychartcn/keepalive.asp`. Do **NOT** use `/mycare/v1.0/user` as a generic probe — it's pharmacy-scoped and returns 502 without the full header contract.
- **Profile data:** `/mycare/v1.0/user` with the pharmacy `X-apiKey`/`X-appName`/`X-componentName`/`X-inclusionJsonPath` header contract. Rich response (name, DOB, addresses, phones, insurance, MRN, GUID). See ADR-006 for the trust-boundary rationale.
- **KPDL `/mycare/v1.0/uidatalayer/s/profile` is a write-through data layer, not an authoritative source.** Cold calls return empty shells. Don't use it.
- **Kaiser data quirks (handled in `profile.py`):**
  - Dates carry trailing `Z` (`"1970-01-01Z"`) → `_clean_date()` strips.
  - Coverage end uses year-4000 sentinel for "no end" → `_clean_date(allow_sentinel=True)` maps to `None`.
  - Field named `emailAddresseInfos` (Kaiser's spelling, not a typo).
  - Phone numbers are `{area, exchange, subscriber}` objects → format as `AAA-EEE-SSSS`.
  - Region fields can ALL return a type code (`"MRN"`) instead of a real region — including `primaryRegion`, `accountRoleRegion`, and `membershipAccountInfo.region`. Apply the bad-value filter at every source and return `None` when no clean value is found.
  - Phones may all return `primaryIndicator: false` AND the list order varies between calls. Don't invent a primary — report all as `is_primary: false` honestly and let callers pick via `type`/`label`.

## Live-testing workflow

The MCP server runs as a subprocess under Claude Desktop, configured in `~/Library/Application Support/Claude/claude_desktop_config.json`. Hugo restarts Claude Desktop (Cmd+Q, relaunch) to pick up code changes. Unit tests cover most correctness questions and don't require a restart.

When Hugo wants to smoke-test a new tool live, he'll say "restart done, try it" and we call the tool from chat. Claude Code doesn't have the openkp MCP configured by default, so live testing happens in Claude Desktop (Cowork) or by running the server manually via `openkp` script and calling tools over stdio.

## Upstream reference — do NOT copy code

https://github.com/Fan-Pier-Labs/openrecord. Permissively licensed but we build fresh per ADR-001. Architectural patterns OK to borrow, implementation is independent.

## Hugo's style

Casual and direct. No em dashes, no semicolons. Contractions. Short paragraphs. Asks clarifying questions sparingly. Works in focused evenings, not full-time. Backward-reasons from outcomes. Wants assumptions surfaced.
