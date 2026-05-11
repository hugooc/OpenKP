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

## Current state (2026-05-04)

- **Phase 0 scaffold:** complete.
- **Phase 1 auth:** complete. Silent session reuse via `~/.openkp/session.json` + httpx probe to `/mychartcn/keepalive.asp`. Interactive first-run Chromium, silent after. See ADR-005 and `docs/recon/session-2.md`.
- **Phase 2 read tools:** complete.
  - `get_profile` ✅ shipped + live-verified. Demographics, contact info, insurance plans, PCP, **emergency contacts** (also covers DPOAHC healthcare agents). See `docs/recon/session-4.md` and `session-10.md`.
  - `list_messages` + `read_message` ✅ shipped + live-verified. Message center list, single-thread read, search. See `docs/recon/session-5.md`.
  - `list_lab_results` + `read_lab_result` + `download_lab_result_pdf` ✅ shipped + live-verified. Test results (labs, imaging, cardiac device reports) plus PDF download to `~/.openkp/downloads/`. The PDF tool surfaces four statuses: `downloaded`, `generation_in_progress` (Kaiser builds large PDFs on demand, retry in ~30s), `no_pdf_available` (no doc exists), `error`. See `docs/research/endpoints/labs.md` and `docs/recon/session-7.md`.
  - `list_medications` ✅ shipped + live-verified. Active and recent prescriptions with dose, prescriber, sig, refills, copay, mailable / auto-refill flags. **First scraper to hit the new pharmacy BFF microservices on `apims.kaiserpermanente.org`** — proves session cookies cross subdomains within `.kaiserpermanente.org`. See `docs/research/endpoints/medications.md` and `docs/recon/session-8.md`.
  - `list_problems` + `list_allergies` ✅ shipped + live-verified. Active diagnoses (name + date_noted, intentionally minimal — KP doesn't expose ICD/severity to patients) and allergy list (handles "no known allergies" as a first-class state via derived `status` field). Both back on the legacy `/mychartcn/Clinical/<topic>/LoadListData` family — meds was the BFF outlier, not the new normal. See `docs/research/endpoints/problems.md`, `allergies.md`, and `docs/recon/session-9.md`.
  - `emergency_contacts` (closes Phase 2) ✅ shipped + live-verified. Returns the full relationship roster — emergency contacts, DPOAHC healthcare agents, conservators — from a single Epic/MyChart endpoint. See `docs/research/endpoints/emergency_contacts.md`.
  - `list_appointments` + `list_past_visits` ✅ shipped + live-verified 2026-05-04. Upcoming/in-progress visits (single-call, no pagination) and past visits (paginated walker with `max_pages`, `page_size`, `until_iso` bounds). Both back on the legacy `/mychartcn/Visits/VisitsList/<Load*>` family. **Live-verified twice: "when's my next appointment" returned next visit cleanly; "how many appointments in 2025, split virtual vs in-person" walked past visits and answered correctly (9 clinical encounters: 6 in-person + 3 virtual).** Filter HAR yielded the `numVisitsToRetrieve` discovery (default page=10 in front end, but Kaiser honors up to 78 — OpenKP defaults to 50, 5x fewer round trips for multi-year history). Filter-by-provider would be a future extension via `LoadFilterOptions` (see `appointments.md` "Filter index"). Session journal in sidecar.
  - `read_visit_notes` + `download_visit_avs_pdf` ✅ shipped + live-verified 2026-05-04. Clinical notes (provider chart notes, progress notes, op notes) plus the rendered After Visit Summary, for one past visit. Four-step server-side chain (`GetVisitDetailsPast` → `GetVisitNotes` → per-note `ValidateVisitNote` + `LoadReportContent(contextINI=HNO)` → `LoadReportContent(reportMnemonic=AMB_AVS)`) collapsed into one tool. **Two-CSRF gotcha:** Kaiser scopes anti-forgery tokens by referer; ValidateVisitNote uses `/visits/note?csn=...` referer while everything else uses `/visits/past-details?csn=...`. AVS PDF download follows the labs-PDF pattern (GetDocumentDetails → DownloadOrStream). HTML-stripped to plain text on `content_text`, raw HTML preserved on `content_html`. See `docs/research/endpoints/visit_notes.md`. Session journal in sidecar.
- **Phase 3 write tools:** underway.
  - `request_refill(medication_id, confirm=False)` ✅ shipped 2026-04-25 (mail-only v1). Two-call confirm pattern, audit log + dry-run scaffolding. **Preview path live-verified, commit path pending next real refill cycle.** See `docs/recon/session-11.md`.
  - `track_refill_order(order_number)` ✅ shipped + live-verified 2026-04-27 (read sibling to request_refill). Single GET against `/orderDetails`. Surfaces order status (INPROGRESS / SHIPPED / DELIVERED), per-Rx detail, shipping address, payment last-4 / type / expiry, and a derived `tracking_ids` list. **Both INPROGRESS (HAR) and SHIPPED (live, 2026-04-27) verified against real Kaiser data.** Confirmed: `copay` on rxList entries populates post-adjudication (null on INPROGRESS, real $ once shipped), and `SHIPPED` is a real intermediate state where `digitalStatus="Complete"` even though `trackingId` is still empty (carrier handoff lags by hours/days). DELIVERED transition still unverified. See `docs/recon/session-13.md`.
  - `send_message(recipient_id, topic_value, subject, body, confirm=False)` + `list_message_recipients()` + `list_message_topics()` ✅ shipped 2026-05-03 (preview path live-verified, commit path unit-tested only). Two-call confirm pattern mirroring `request_refill`. Sits on `/mychartcn/api/medicaladvicerequests/*` (Kaiser's "Non-Urgent Medical Advice" / "Message your care team" surface). Five-step server-side chain collapsed into one tool: GetComposeId → SaveDraft (mints conversationId) → Send → RemoveComposeId. Audit log records intent/result/error with **subject and body NOT logged** (recipient name, topic, line count only). v1 limits: no attachments, no reply-to-existing-thread (always starts new conversation). Topic catalog discovered live: `97` Test Results / `98` Medication / `99` Visit Follow-Up / `100` Upcoming Appointment or Procedure / `101` Non-Urgent Medical Advice. See `docs/research/endpoints/messages.md` "Send new message" section and `docs/recon/session-14.md`.
- **Late-Phase-2 attachments + deep search:**
  - `download_message_attachment` ✅ shipped + live-verified 2026-04-25 (session 12). Two-step chain (`GetDocumentDetailsLegacy` → binary GET). Saves to `~/.openkp/downloads/`. Genetic panels and other clinically important documents arrive as message attachments — Kaiser doesn't surface them in test-results.
  - `list_messages(deep_search=True, max_pages=30)` ✅ shipped + live-verified 2026-04-25 (session 12). Walks pagination via `localSummary.oldestSearchedInstantISO` because Kaiser's `searchQuery` is page-scoped, not index-scoped (default search misses anything older than the most recent ~50 threads). Use this when looking for archival messages. See `docs/research/endpoints/messages.md` "Search" section and `docs/recon/session-12.md`.

**Tests:** 527 passing. Run with `.venv/bin/pytest -q` from `openkp/`.

## Next session: start here

**Top candidates, in rough priority order:**

1. **Git history rewrite + flip repo public** — the **only remaining v1 release blocker.** Working state is PHI-clean (session 17 scrub); old commits still hold the original DOB/GUID/MRN/ZIP/provider names/recon journals in their blobs. Steps documented in `docs/release-checklist.md`. Best done as one focused chunk: `git filter-repo` with the replacement table → force-push → file a GitHub support ticket asking them to GC unreferenced refs (1-3 business-day turnaround) → final pre-flip audit (fresh `git log -p | grep`, walk install steps from a clean directory) → flip repo public. **~3-4 hours of focused work, mostly waiting.**

2. **`reply_to_message(thread_id, body)`** — natural sibling to `send_message`. Needs a fresh HAR capture (the "Reply" button on an opened thread almost certainly hits a different endpoint than compose). Lower-risk than `send_message` because we're not picking a recipient — the thread already names one.

3. **`send_message` polish from session 14 review:**
   - **PCP role label fallback:** the PCP recipient row's `role` came back null because `specialty` and `pcpTypeDisplayName` were empty strings. Derive `"Primary Care"` from `recipientType == 1` so the UI/caller has something to display.
   - **OOC awareness:** the recipient catalog carries `oocDateISO` and `oocContextString` for providers who are out of office. Surface those as fields on `MessageRecipient` so the preview can flag "your provider is out of office until X" before the user commits.
   - **`body_preview` rename or cap:** today's field name suggests truncation but the implementation only truncates above 200 chars. Either rename to `body` (full echo always) or always cap with `...` suffix when longer.

**Loose ends (optional, not blocking):**
- **`read_visit_notes` `iso` field is inconsistent.** Clinical notes return real ISO-8601 (`"2025-12-04T13:36:47-08:00"` from `noteList[i].iso`); the synthetic `after_visit_summary` `iso` carries Kaiser's display string (`"Dec 04, 2025"` from `visitSummaryInfo.encounterDate`). Field name promises ISO but only delivers half the time. Either rename to `date_display` or parse the encounter date into real ISO before returning.
- **Live-verify the `is_telemedicine` heuristic on `list_appointments` / `list_past_visits`.** Recon had zero virtual visits, so the heuristic (Telemedicine OR EVisit OR CanShowTelemedicine) is inferential. Cowork-Claude bypassed it by reading `visit_type` directly ("Telephone", "Video Visit"), but next time Hugo's calendar has a video or phone visit, peek at the dump to see whether the heuristic actually fires.
- **Capture a filter-applied appointments HAR** to learn how `LoadPast` accepts a provider/specialty filter ID. The filter UI HAR (session 15) only loaded the dropdown options; we never saw a filter actually applied. Unblocks `list_past_visits(provider="...")`-style queries.
- **Live-verify the `send_message` commit path** next time you actually need to message a provider. Today only the preview path was hit live; the GetComposeId / SaveDraft / Send chain is theoretical-correct + unit-tested but not yet exercised against Kaiser. Tail `~/.openkp/audit.log` from the dev session before you fire `confirm=True` so events stream live.
- Verify the DELIVERED transition for the chlorthalidone order from session 11 next time you're in OpenKP. The order number sits in `docs/research/captures/kp-refill-2-with-order-details.har` (gitignored) and the SHIPPED state is already snapshot in session-13.md. The remaining unknowns are the carrier-tracking-attached state and the DELIVERED transition.
- Live-verify `list_messages(deep_search=True)` from Cowork. The download tool was end-to-end verified in session 12, but the deep_search code path wasn't called explicitly — Cowork-Claude effectively reproduced the algorithm manually with `before_iso` walking.
- ~~Spot-check whether MyChart "Documents" / "Visit Notes" / "After Visit Summary" sections hold reports OpenKP doesn't reach.~~ **Confirmed yes 2026-05-06** — Document Center is a separate surface (`LoadOtherDocuments`, plus a new `ddm/getdocumentsbff` BFF). See `docs/research/endpoints/documents.md`.

## New surfaces mapped 2026-05-06 (no bodies yet, no tools shipped)

A "click around with DevTools open" capture session surfaced three new data
domains that weren't on our radar. None implemented; all worth recording before
the threads go cold. Response bodies were stripped from the HARs by Chrome's
export (large-payload truncation), so each needs a fresh capture before
implementation.

- **Billing & Coverage** — five new BFFs on `apims.kaiserpermanente.org`
  (balance, coverage, guarantor, member-transition, notification prefs).
  **Auth contract differs from pharmacy** — `X-appName` / `X-componentName` /
  `X-region: HomeAndCAFH`, no `X-IBM-client-Id`, no `x-guid`. See
  `docs/research/endpoints/billing.md`.
- **Document Center** — `LoadOtherDocuments` (legacy) + `ddm/getdocumentsbff`
  (new BFF). Two parallel documents surfaces, likely overlapping. Plus the
  federal V/D/T `record-download` surface for C-CDA/PDF visit exports. See
  `docs/research/endpoints/documents.md`.
- **Upcoming orders** — pending labs/imaging/procedures the doctor placed but
  the patient hasn't completed yet, with patient prep instructions. The
  homepage "View instructions" / POTASSIUM card. New data class, pairs with
  `list_lab_results` at the opposite end of the lifecycle. Strong tool
  candidate. See `docs/research/endpoints/upcoming_orders.md`.

**BFF heterogeneity warning** added to `docs/research/endpoints/medications.md`:
the pharmacy header set is pharmacy-specific. Each new BFF needs its own
header capture.

## Read these first

- `DESIGN.md` — vision, principles, architecture, roadmap, tool inventory, safety patterns. Single source of truth.
- `docs/release-checklist.md` — pre-public-release todos. Items 1 (README) and 4 (LICENSE) done; item 2 (history rewrite) is the only remaining hard blocker.
- **Recon journals live in the gitignored sidecar** at `private/documentation/recon/` (consolidated 2026-05-10 from `~/Desktop/OpenKP Documentation/`; the whole `private/` tree is gitignored). The last few are the most relevant context: session-17 (PHI scrub + READMEs), session-16 (visit notes + AVS), session-15 (appointments + page_size), session-14 (send_message), session-13 (track_refill_order).
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
- **Pharmacy data:** lives on the new BFF microservices host `apims.kaiserpermanente.org`, NOT `healthy.kaiserpermanente.org/mychartcn/...`. Endpoints under `/kp/mycare/pharmacy-microservices/{rx-cost-inventory-bff, rx-order-management-bff, pharmacy-center-kpweb-bff}/v1/...`. Auth model: header-based (`X-IBM-client-Id`, `x-guid`, `x-region: MRN`, `X-KPSessionID: undefined`) PLUS the same session cookies. Cookies cross subdomains automatically because they're scoped to `.kaiserpermanente.org`. See `medications.py` for the working pattern. v1 only uses `rxDetails`.

## Development workflow

Dev sessions launch via terminal `claude` from `~/OpenKP/`, not the macOS Claude Code app. The app's per-session worktree default puts code under `.claude/worktrees/<branch>/`, which doesn't match where the Cowork live-test path imports from (`~/OpenKP/openkp/src/`). Worktree-side edits never reach the live MCP server without a manual copy.

All code lands in the main checkout, on `main` or a feature branch. Never under `.claude/worktrees/`. Live tests still happen in Cowork after Cmd+Q and relaunch (existing pattern, unchanged).

## Live-testing workflow

The MCP server runs as a subprocess under Claude Desktop, configured in `~/Library/Application Support/Claude/claude_desktop_config.json`. Hugo restarts Claude Desktop (Cmd+Q, relaunch) to pick up code changes. Unit tests cover most correctness questions and don't require a restart.

When Hugo wants to smoke-test a new tool live, he'll say "restart done, try it" and we call the tool from chat. Claude Code doesn't have the openkp MCP configured by default, so live testing happens in Claude Desktop (Cowork) or by running the server manually via `openkp` script and calling tools over stdio.

**Write-tool live-testing — tail the audit log.** Write tools (Phase 3+) write to `~/.openkp/audit.log` (JSONL) before and after each Kaiser call. Whenever Hugo is about to trigger a write call from Cowork, set up a `Monitor` on `tail -F ~/.openkp/audit.log` *first*, then tell him to go. Events stream into the dev session as they happen — `intent` when the commit starts, `result`/`error` when it finishes. Way better than waiting for the LLM's response to be pasted back, and it works even when something fails before the LLM returns anything useful. The audit log is gitignored and lives outside the repo.

## Upstream reference — do NOT copy code

https://github.com/Fan-Pier-Labs/openrecord. Permissively licensed but we build fresh per ADR-001. Architectural patterns OK to borrow, implementation is independent.

## Hugo's style

Casual and direct. No em dashes, no semicolons. Contractions. Short paragraphs. Asks clarifying questions sparingly. Works in focused evenings, not full-time. Backward-reasons from outcomes. Wants assumptions surfaced.
