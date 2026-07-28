# OpenKP — notes for Claude Code

This file is auto-loaded by Claude Code when it opens `~/OpenKP/`. It's the on-ramp. Read it first, then follow the pointers into the real docs.

## What OpenKP is

A local MCP server that bridges Claude and Kaiser Permanente's patient portal. Single-user, runs on Hugo's Mac (and also tested on Windows — see `docs/install/windows.md`). All credentials and PHI stay on the machine. Licensed under PolyForm Noncommercial 1.0.0 (see ADR-007). No hosted service. See `DESIGN.md` §1-2 for the full "why."

## v1 audience and distribution

OpenKP v1 ships as a source-available GitHub project for **technically-curious KP members and patient-advocacy peers** — people who have Claude Code installed (or will install it) and can follow a Claude-Code-guided setup. We are deliberately not building a non-technical-user installer in v1. The `.dxt` + bundled-runtime + GUI-credential-entry work is parked at Phase 4.5 and only happens if real demand emerges.

What this means for current work:

- Keep the architecture unchanged. Local-first, MCP-over-stdio. Mac is the primary tested platform, Windows runs the same code with a handful of platform-specific setup steps (`docs/install/windows.md`).
- The README must read well for a curious human AND be structured enough for Claude Code to walk a user through install end-to-end.
- Error messages should be clear, but they don't need to be tuned for non-technical users yet.
- Lead positioning with the CAIHL frame: patient-directed AI on patient-owned data, not "AI reads my chart."

See `DESIGN.md` §1 (audience), §5 (Phase 4 / 4.5), §10 (distribution strategy).

## Current state and next work

Status log, next-session candidates, and the 2026-05-06 surface mapping now live
in `docs/status.md` (moved out of this file 2026-07-28 to keep the always-loaded
context lean). `WISHLIST.md` is the source of truth for what to build next —
each candidate has a fuller writeup there.

Phase 0/1/2 complete, Phase 3 write tools underway. 27 MCP tools shipped.
599 tests. Run with `.venv/bin/pytest -q` from `openkp/`.

**Both write tools now differ in maturity — don't treat them alike.**
`send_message`'s commit path was live-verified 2026-07-29 after two months of
never having worked. `request_refill`'s commit path is still unverified and
carries the same class of risk: its response shapes are inferred, and inferred
shapes have been wrong every time they were finally checked. See
`docs/postmortems/2026-07-29-send-message-compose-chain.md`.


## Read these first

- `DESIGN.md` — vision, principles, architecture, roadmap, tool inventory, safety patterns. Single source of truth.
- `docs/release-checklist.md` — pre-public-release todos. All hard blockers now closed: README, LICENSE, PHI history rewrite (via fresh-repo strategy), and website are all done. Repo is public at github.com/hugooc/OpenKP.
- **Recon journals live in the gitignored sidecar** at `private/documentation/recon/` (consolidated 2026-05-10 from `~/Desktop/OpenKP Documentation/`; the whole `private/` tree is gitignored). The last few are the most relevant context: session-20 (openkp.org site review + deploy + custom domain + repo-state reconciliation, 2026-05-11), session-19 (Codex audit + release hygiene + PHI rewrite + sidecar consolidation, 2026-05-10), session-18 (click-around recon, 2026-05-06), session-17 (PHI scrub + READMEs), session-16 (visit notes + AVS).
- `docs/adr/README.md` — architectural decisions index. ADRs 001-007 live here.
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

## Region scope

OpenKP is NorCal-only as tested. Region codes baked into the code (`"CN"`, `"NCA"`, NorCal ZIPs, NorCal pharmacy phone) reflect the only region we have HAR captures for. When working on new tools, prefer pulling region-shaped values from `profile.py` output (the user's own membership region) over hardcoding, even if today's only test data is NorCal. Anything you can't pull from session data, leave a clear `# NorCal-specific` comment so it's findable when someone tries to port to SoCal or NW.

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
  - GUID can be a JSON number rather than a string. `userIdentityInfo.guid` may come back as `1234567` (int), not `"1234567"`. Coerce with `str(value).strip()`, never `isinstance(str)`. Same applies to other identity fields likely.
  - **Single-element X-inclusionJsonPath returns a different envelope.** Asking for one path strips the `UserAccountData` wrapper; asking for many (joined by `;`) preserves it. Always use the multi-path form even when you only need one field. See `medications.py:_GUID_INCLUSION_PATHS`.
- **Kaiser returns application errors as HTTP 200.** `{"error": 2}` with a 200 status is a rejection; `raise_for_status()` sails past it. `error: 0` rides along with success, so the field is always present on the send endpoints. Use `_raise_for_kaiser_error` in `messages.py`. Assume any Epic/MyChart endpoint can fail this way.
- **Send-message chain (verified live 2026-07-29, after two months of never working):**
  - `GetComposeId` returns a **bare JSON string** — the whole 130-byte body is `"WP-<128 chars>"`. `response.json()` gives a `str`, not a dict. Not double-encoded. Parse with `_extract_token`, which accepts bare or wrapped.
  - `GetViewers` is **required**, not optional. The UI calls it between GetComposeId and SaveDraft. Skipping it leaves `viewers[0].wprId` empty and Kaiser rejects the draft. Its name field is `name`, not `displayName`.
  - The `recipient` object must carry **exactly five keys**: `displayName`, `userId`, `poolId`, `providerId`, `departmentId`. Do not echo the whole recipient row. `poolId` and `departmentId` are empty in the browser too.
  - `SaveDraft` returns an **object** `{"conversationId":"WP-…","error":0}`, unlike GetComposeId. Don't assume the two match.
  - Full archaeology in `docs/postmortems/2026-07-29-send-message-compose-chain.md`.
- **Response shapes marked `(inferred)` in `docs/research/endpoints/` are guesses, not facts.** DevTools' HAR buffer evicts response bodies, so several were reconstructed from `content.size`. Never mock an inferred shape in tests without a comment saying so — a green suite over a guessed fixture manufactures confidence. When a body is missing from a capture, read its byte size as evidence.
- **Pharmacy data:** lives on the new BFF microservices host `apims.kaiserpermanente.org`, NOT `healthy.kaiserpermanente.org/mychartcn/...`. Endpoints under `/kp/mycare/pharmacy-microservices/{rx-cost-inventory-bff, rx-order-management-bff, pharmacy-center-kpweb-bff}/v1/...`. Auth model: header-based (`X-IBM-client-Id`, `x-guid`, `x-region: MRN`, `X-KPSessionID: undefined`) PLUS the same session cookies. Cookies cross subdomains automatically because they're scoped to `.kaiserpermanente.org`. See `medications.py` for the working pattern. v1 only uses `rxDetails`.

## Development workflow

Dev sessions launch via terminal `claude` from `~/OpenKP/`, not the macOS Claude Code app. The app's per-session worktree default puts code under `.claude/worktrees/<branch>/`, which doesn't match where the Cowork live-test path imports from (`~/OpenKP/openkp/src/`). Worktree-side edits never reach the live MCP server without a manual copy.

All code lands in the main checkout, on `main` or a feature branch. Never under `.claude/worktrees/`. Live tests still happen in Cowork after Cmd+Q and relaunch (existing pattern, unchanged).

## Live-testing workflow

The MCP server runs as a subprocess under Claude Desktop, configured in `~/Library/Application Support/Claude/claude_desktop_config.json`. Hugo restarts Claude Desktop (Cmd+Q, relaunch) to pick up code changes. Unit tests cover most correctness questions and don't require a restart.

**Always verify the server process is newer than the edit before trusting a live result.** Python binds modules at import, so a running server keeps executing the code it loaded at startup. On 2026-07-29 four debugging cycles were spent reading identical failures from a process that predated the fix by 21 hours — `Cmd+Q` had not actually quit Claude.app:

```
ps -ax -o pid,lstart,command | grep venv/bin/openkp
pgrep -f "Claude.app/Contents/MacOS/Claude" || echo "quit ok"
```

If the process start time is older than the file mtime, the patch is not live and nothing observed means anything. Force it with `osascript -e 'quit app "Claude"'` and confirm the pid is gone before relaunching.

**Gate any temporary debug probe on `PYTEST_CURRENT_TEST`.** A probe that writes to a file from a scraper function also fires under `pytest`, and mocked fixtures then look exactly like live capture. This happened: a local test run wrote `{"composeId":"CID-123"}` into `~/.openkp/debug.log` and it was briefly read as a real Kaiser response.

When Hugo wants to smoke-test a new tool live, he'll say "restart done, try it" and we call the tool from chat. Claude Code doesn't have the openkp MCP configured by default, so live testing happens in Claude Desktop (Cowork) or by running the server manually via `openkp` script and calling tools over stdio.

**Write-tool live-testing — tail the audit log.** Write tools (Phase 3+) write to `~/.openkp/audit.log` (JSONL) before and after each Kaiser call. Whenever Hugo is about to trigger a write call from Cowork, set up a `Monitor` on `tail -F ~/.openkp/audit.log` *first*, then tell him to go. Events stream into the dev session as they happen — `intent` when the commit starts, `result`/`error` when it finishes. Way better than waiting for the LLM's response to be pasted back, and it works even when something fails before the LLM returns anything useful. The audit log is gitignored and lives outside the repo.

## Upstream reference — do NOT copy code

https://github.com/Fan-Pier-Labs/openrecord. Permissively licensed but we build fresh per ADR-001. Architectural patterns OK to borrow, implementation is independent.

## Hugo's style

Casual and direct. No em dashes, no semicolons. Contractions. Short paragraphs. Asks clarifying questions sparingly. Works in focused evenings, not full-time. Backward-reasons from outcomes. Wants assumptions surfaced.
