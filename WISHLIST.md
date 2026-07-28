# OpenKP — Wishlist

Ideas worth building when there's appetite, not blocking v1. Each entry should explain the use case and how it fits the bare-bones-substrate framing — OpenKP exposes structure, callers and contributors build on top.

Last updated: 2026-06-05.

## Current priority snapshot

These are the best next sprint candidates, based on the current Kaiser portal surface and the notes in `CLAUDE.md` plus `docs/research/endpoints/`.

1. `list_documents()` + `download_document(document_id)` — exposes the Document Center corpus that labs, messages, and visits do not fully cover.
2. `reply_to_message(thread_id, body, confirm=False)` — closes the obvious secure-messaging workflow gap.
3. `send_message` polish — small quality improvements before expanding the write surface.
4. `list_immunizations()` + `list_health_reminders()` — useful preventive-care substrate.
5. Billing / coverage reads — visible in the portal, but lower priority until the clinical-record and sharing surfaces are better covered.

---

## `list_upcoming_orders()` + `read_upcoming_order_instructions(order_id)` — pending tests and procedures

**Status:** Shipped 2026-06-05. Implemented from an in-app browser capture
plus a redacted OpenKP-side live probe of `GetUpcomingOrders`. The detail
response body is now mapped: `orderGroupList`, `orderList`, `providerList`,
and `upcomingOrdersSettings`, with full instructions embedded as
`orderList[*].instructions`. Live Claude MCP testing confirmed both the list
tool and the read-instructions tool.

**Use case:** Kaiser shows pending labs, imaging, and procedures before they become results. The home page currently surfaces "new instructions to review" cards for requested tests. OpenKP can read completed lab results, but it cannot yet answer "what has my doctor ordered that I still need to do?" or "what prep instructions did Kaiser give me?"

This fills the placed-but-not-resulted half of the care lifecycle and is especially useful for patients with multiple pending tests.

**Shape:** Two read tools:

- `list_upcoming_orders()` returns pending orders with `id`, `name`,
  ordering provider, ordered/due/expiration dates, scheduling fields,
  comments, and a short `instructions_preview` when present.
- `read_upcoming_order_instructions(order_id)` returns full patient-facing
  instructions with `instructions_text`, `instructions_html`, related
  location/timing fields, and source metadata.

Known route: `/mychartcn/app/upcoming-orders`.

Known detail call: `POST /mychartcn/api/upcoming-orders/GetUpcomingOrders` with `selectedOrderID` and `PageNonce`.

**What shipped:**
- CSRF-gated `POST /mychartcn/MixedItemFeed` order-id discovery from homepage
  JSON action URIs.
- `GetUpcomingOrders` detail/list parser with clear failure when required
  top-level maps are absent.
- MCP registrations for both tools.
- Focused tests in `openkp/tests/test_upcoming_orders.py`.
- Endpoint map update in `docs/research/endpoints/upcoming_orders.md`.

**Non-goal:** Don't decide whether an order is clinically important or overdue. Return Kaiser's structured data and let the caller reason with the user.

---

## `list_documents()` + `download_document(document_id)` — Document Center

**Use case:** Kaiser's Document Center is a separate corpus from messages, labs, AVS PDFs, and visit notes. It may contain letters, forms, releases, scanned reports, and other documents that patients otherwise do not know to look for. A patient should be able to ask "show me every document Kaiser has posted about me" without manually clicking through the portal.

**Shape:** A unified document list plus download:

- `list_documents()` returns document headers from both observed surfaces when possible: title, document type, source surface, date, author/source, related encounter/order identifiers, and a download handle.
- `download_document(document_id)` saves the source document to `~/.openkp/downloads/` using the same pattern as lab PDFs, AVS PDFs, and message attachments.

Known surfaces:

- Legacy MyChart: `POST /mychartcn/api/documents/viewer/LoadOtherDocuments`
- DDM BFF: `GET /kp/prod/mycare/ddm/getdocumentsbff/v1/documents?esb-envlbl=PROD`
- Existing download infrastructure: `GetDocumentDetails` plus `DownloadOrStream`

**What's missing:**
- Fresh HAR with response bodies preserved. Existing captures show real data but stripped bodies.
- Confirm whether the legacy and DDM BFF lists overlap or complement each other.
- Endpoint map update in `docs/research/endpoints/documents.md`.
- Parser, dedupe strategy, MCP registration, and tests.

**Non-goal:** Don't summarize or classify document content inside OpenKP. Return the document inventory and bytes; Claude can summarize in conversation.

---

## `list_access_log(...)` — portal and third-party access history

**Status:** Shipped and live-verified 2026-06-05 as
`list_access_log(kind="third_party", max_pages=5)`. Implemented from preserved
HAR bodies with focused tests. Live smoke tests returned 100 Fasten Connect
third-party entries across two pages, then stopped with `cursor_repeated`
because Kaiser returned the same next cursor twice.

**Use case:** A patient-directed data tool should help the patient see who and what has accessed their record. Kaiser's portal exposes sharing, linked apps, and access-log style surfaces. OpenKP should make it easy to ask: "Which third-party apps accessed my data, what did they read, and when?"

This is especially aligned with the CAIHL framing: patient-owned data is not just about reading the record, but about understanding the flows around the record.

**Shape:** One bounded read tool with a mode flag:

- `list_access_log(kind="third_party", max_pages=5)` for connected apps and outside access.
- `list_access_log(kind="portal", max_pages=5)` for the user's own portal access log.

Returns timestamp, actor/app name, third-party action / data class when exposed, and raw Kaiser enum values for fields whose labels are not yet mapped.

Known endpoints noted in `CLAUDE.md`:

- `GetPortalAccessLogEntries`
- `GetThirdPartyAccessLogEntries`
- Legacy `/mychartcn/api/access-logs/` family.

**What shipped:**
- `docs/research/endpoints/access_logs.md` with the request / response map.
- Bounded walker using the `startingLine` cursor.
- Explicit `stop_reason` / warning when Kaiser repeats a cursor.
- Tests for parser behavior, pagination, max-page stopping, and cursor-repeat stopping.

**What's still missing:**
- Enum labels for `entryType`, `ccdAction`, and `accessMethod` once more values are observed.
- A deeper-pagination contract for older third-party history beyond the repeated cursor.

**Non-goal:** Don't infer whether access was appropriate. Surface the facts and make the data flow legible.

---

## `reply_to_message(thread_id, body, confirm=False)` — reply to an existing care-team thread

**Use case:** `send_message` starts a new non-urgent message, but most real patient communication happens as replies inside existing threads. A patient should be able to ask, "reply to my cardiologist with this update" without composing a new thread or reselecting the recipient/topic.

**Shape:** Phase 3 write tool using the same preview/confirm/audit pattern as `send_message`.

- Preview returns the thread participants, subject, recent-message summary, body line count, and a confirmation requirement.
- Commit sends a reply to the existing thread.
- Audit log should not store the reply body.

**What's missing:**
- Fresh HAR capture from clicking Reply in an existing message thread.
- Endpoint map update in `docs/research/endpoints/messages.md`.
- Scraper implementation and tests alongside `send_message`.
- Live verification on a low-risk real thread.

**Non-goal:** Don't support attachments in v1. Keep the first version to plain-text replies.

---

## `send_message` polish — recipient and preview quality

**Use case:** Before expanding more write tools, the existing secure-message flow should expose enough context for safer previews. The tool already has the confirm pattern; the improvements are about making the preview more informative.

**Shape:** Small improvements in `messages.py`:

- Derive `"Primary Care"` from `recipientType == 1` when the PCP row has empty specialty/role fields.
- Surface `oocDateISO` and `oocContextString` so the preview can warn when a provider is out of office.
- Rename `body_preview` or always cap it consistently so the field name matches behavior.

**What's missing:**
- Implementation and tests.
- README/tool-doc note if returned fields change.

**Non-goal:** Don't change the confirm-before-act contract.

---

## `list_immunizations()` + `list_health_reminders()` — preventive-care substrate

**Use case:** The Kaiser menu exposes Immunizations and Health Reminders, but OpenKP cannot yet answer "what vaccines are in my record?" or "what preventive care does Kaiser think I am due for?" These tools would support gap-finding and appointment prep.

**Shape:** Two read tools:

- `list_immunizations()` returns vaccine name, administration date, dose/series fields when present, source, and status.
- `list_health_reminders()` returns reminder name, status, due date, last completed date, and related care gap text.

**What's missing:**
- HAR captures for both menu surfaces with response bodies preserved.
- New endpoint maps, scrapers, MCP registrations, and tests.

**Non-goal:** Don't independently calculate preventive-care guidelines. Report Kaiser's reminder state exactly.

---

## Billing and coverage reads

**Use case:** Billing & Coverage, Claims, and Benefits are visible in the menu, and some BFF endpoints are already mapped. These are useful for "what do I owe?" and "what coverage do I have?" questions, but they are less central than clinical-record, document, and sharing/access surfaces.

**Shape:** Lower-priority Phase 4+ reads:

- `get_billing_balance()`
- `list_claims()`
- `list_coverages()`
- `list_member_transitions()`

Known mapped BFFs live in `docs/research/endpoints/billing.md`.

**What's missing:**
- Fresh captures with bodies preserved.
- More investigation of claims/detail pages, not just the landing page.
- Careful handling of BFF-specific headers; billing does not use the pharmacy header contract.

**Non-goal:** No payment write tool until read-side billing is stable and the confirmation/audit surface is designed.

---

## Multi-user support (multi-profile on one Mac)

**Use case:** A Kaiser member (Hugo) wants to occasionally help a family member (e.g., sister-in-law) read her own KP data through Claude — without mixing accounts, leaking her PHI into his audit log, or running her tools against his session.

**Shape:** No code refactor needed. The substrate is already there.

- `OPENKP_DATA_DIR` env var already exists (defaults to `~/.openkp`). Each MCP server subprocess gets its own data dir → its own `session.json`, audit log, downloads folder.
- `KP_USERNAME` env var per process picks the keyring entry.
- Two `claude_desktop_config.json` server entries (e.g., `openkp-hugo`, `openkp-sil`) with different env blocks → two fully isolated profiles. The LLM sees them as distinct tool families (`mcp__openkp-hugo__list_messages` vs `mcp__openkp-sil__list_messages`), so cross-contamination is structurally impossible.

**What's missing:**
- README section showing the two-server config pattern with annotated env blocks.
- Optional: namespace the keyring service as `openkp:<profile>` to make export/audit cleaner. Debatable whether this is worth the migration.

**Non-goal:** Don't build a "switch profile" tool inside the MCP. Process boundaries are the right isolation primitive — anything finer-grained inside one process is more code, more risk, less elegant.

---

## No-stored-credentials login mode

**Use case:** When OpenKP is hosted on a machine the Kaiser member does not own (e.g., the helper scenario above), the member shouldn't have to entrust her password to the host. Today's flow stores the password in the host's keyring, which is fine for the single-user case but uncomfortable for the helper case.

**Shape:** A flag (e.g., `OPENKP_INTERACTIVE_LOGIN=1`) that changes `auth.py`'s first-run flow:

- Skip the autofill step.
- Playwright opens Kaiser's login page and waits for the user to type her password directly into Kaiser's form.
- OpenKP captures the resulting session cookies on redirect to `/mychartcn/Home` (existing behavior).

The password never touches OpenKP — not in keyring, not in env, not in memory. Kaiser sees it because Kaiser has to. The browser sees it because Kaiser's login is in the browser. OpenKP doesn't.

**Trade-off:** She has to re-type at session expiry (probably weekly, based on what we know of KP cookie lifetimes). For the helper case, that's a feature, not a bug.

**What's missing:** Implementation in `auth.py`, plus a README note explaining when to use this mode.

---

## `list_refill_orders(start_date, end_date)` — pharmacy order history

**Use case:** `track_refill_order` looks up one order if you already know its order number. There's no way today to ask "what refills have I placed in the last 12 months, and what did each cost?" That's the natural read companion — same domain as `request_refill` and `track_refill_order`, scoped to a list across time.

**Shape:** Sibling tool in `scrapers/refill.py`. Likely one GET against an `apims` BFF endpoint we haven't mapped yet (kp.org's Pharmacy → Order History page is the entry point). Returns a list of order summaries with `order_number`, `placed_at`, `order_status`, `copay_total`, and probably an Rx-count or per-Rx list. Reuses the same response-shape vocabulary as `track_refill_order` so callers can pivot from list → detail with one tool call.

**What's missing:**
- HAR capture: open DevTools, browse to kp.org pharmacy order history, save a focused HAR to `docs/research/captures/kp-order-history-1.har`. This is the gating step — without it, the request body / pagination shape is unknown.
- Endpoint map in `docs/research/endpoints/refill.md` (new "GET /orderHistory" or similar section).
- `fetch_refill_orders()` in `scrapers/refill.py`, MCP tool registration, tests modeled on `test_fetch_refill_order_*`.

**Adjacent endpoints already named in captures but unmapped:** `/orderStatus` (rx-order-management-bff, captured but body elided — may or may not be relevant), `/rxnotificationpreferences`, `/paytoprovider`, `/medGuide`, `/drugImage`, `/rxTransferDetails`. Worth checking during the same DevTools session whether any of these surface order-list data we'd otherwise miss.

**Non-goal:** Don't filter / aggregate / summarize on the OpenKP side. Return Kaiser's structured data and let the caller's Claude conversation do the "compare against last year" or "spot the copay outlier" work.

---

## `set_results_release_preferences(...)` — control test-result auto-release timing

**Use case:** Kaiser auto-releases lab and imaging results to MyChart on a schedule that doesn't always match the patient's preference. Some patients want results delayed until their doctor has had a chance to review and contextualize them — a critical-value lab seen at 11pm with no clinician available is more anxiety than information. MyChart already exposes a per-result-type release-timing toggle; the patient just has to find it. A Claude-driven flow ("delay my pathology results until my doctor reviews them") would surface a control most members don't know exists.

**Shape:** Phase 3 write tool with the confirm-before-act pattern. Read sibling `get_results_release_preferences()` returns the current settings; the write call submits the change.

- `POST /mychartcn/api/test-results/GetResultsReleasePreferences` body `{}` — read side, ~163 B response.
- `POST /mychartcn/api/test-results/SetResultsReleasePreferences` — write side, body shape unknown (HAR captured 2026-05-06 had bodies stripped).
- Audit log records intent + result with no PHI in the message.

**What's missing:**
- Fresh HAR with response bodies preserved to learn the preference shape (per-result-category? per-result-type? boolean delay vs. duration?).
- Endpoint map in `docs/research/endpoints/labs.md` (an "Adjacent endpoints worth noting" stub already exists, 2026-05-06).
- Scraper + tool registration + tests modeled on `request_refill`'s confirm pattern.

**Non-goal:** Don't editorialize about whether delayed release is "better." Just expose the control.

---

## `download_appointment_ics(csn)` — calendar file for one appointment

**Use case:** Kaiser already generates `.ics` files for appointments — they're behind the "Add to calendar" button on the visit-details page. Surfacing this as a tool means a member can ask "add my next two appointments to my calendar" and Claude can save the files into `~/.openkp/downloads/` (or write them straight into a calendar via a future MCP integration). Tiny but useful.

**Shape:** Single GET against an endpoint that returns text/calendar bytes. No CSRF, no nonce — same `/mychartcn/Visits/...` family.

- `GET /mychartcn/Visits/VisitDetails/GetCalendarFile?csn=<csn>&details=true`
- Saves to `~/.openkp/downloads/appointment-<csn-prefix>-<date>.ics`.
- The `csn` is already exposed by `list_appointments` and `list_past_visits`, so callers can chain naturally.

**What's missing:**
- Implementation in `scrapers/appointments.py` (or a new `calendar.py` if the file grows). Follows the existing PDF-download pattern from `download_lab_result_pdf`.
- Tests modeled on `test_download_lab_result_pdf`.
- One line of MCP tool registration.

**Non-goal:** Don't parse the .ics into a structured object — Kaiser's bytes are the source of truth, and downstream calendar apps already know how to consume the format.

---

## Adding to the wishlist

Keep entries tight. Use case + shape + what's missing + any non-goals. If an idea is just "would be nice if..." with no concrete shape, leave it out — the discipline is the point.

---

## Live-verification backlog

Moved out of `CLAUDE.md` on 2026-07-28. These are shipped tools whose behavior is
inferential or only partly exercised against real Kaiser data. Not blocking, but
each one is a known gap between what the code assumes and what we've actually seen.

- **Live-verify the `is_telemedicine` heuristic on `list_appointments` / `list_past_visits`.** Recon had zero virtual visits, so the heuristic (Telemedicine OR EVisit OR CanShowTelemedicine) is inferential. Cowork-Claude bypassed it by reading `visit_type` directly ("Telephone", "Video Visit"), but next time Hugo's calendar has a video or phone visit, peek at the dump to see whether the heuristic actually fires.
- **Capture a filter-applied appointments HAR** to learn how `LoadPast` accepts a provider/specialty filter ID. The filter UI HAR (session 15) only loaded the dropdown options; we never saw a filter actually applied. Unblocks `list_past_visits(provider="...")`-style queries.
- ~~**Live-verify the `send_message` commit path**~~ — **done 2026-07-29.** It took three separate fixes. The chain that was "theoretical-correct + unit-tested" was wrong in three places, because its response shapes were inferred from HAR byte sizes and the tests then mocked the inference. Full account in `docs/postmortems/2026-07-29-send-message-compose-chain.md`.

  **Read that before live-verifying anything else on this list.** The same pattern applies to every item here: a tool whose response shapes were never captured is not "probably fine," and a green test suite over an inferred fixture is evidence of nothing. `request_refill`'s commit path is the next one exposed to exactly this risk. Tail `~/.openkp/audit.log` from the dev session before you fire `confirm=True`, and confirm the running server process is newer than your last edit.
- Verify the DELIVERED transition for the chlorthalidone order from session 11 next time you're in OpenKP. The order number sits in `docs/research/captures/kp-refill-2-with-order-details.har` (gitignored) and the SHIPPED state is already snapshot in session-13.md. The remaining unknowns are the carrier-tracking-attached state and the DELIVERED transition.
- Live-verify `list_messages(deep_search=True)` from Cowork. The download tool was end-to-end verified in session 12, but the deep_search code path wasn't called explicitly — Cowork-Claude effectively reproduced the algorithm manually with `before_iso` walking.
