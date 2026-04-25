# OpenKP — Design Document

**Version:** 0.1 (living document)
**Last updated:** April 24, 2026
**Author:** Test Patient

---

## 1. Vision

OpenKP is a patient-directed bridge between Claude and Kaiser Permanente's patient portal. It lets a Kaiser member ask Claude, in plain English, to read and act on their own medical record. Refill a prescription. Send a secure message to a doctor. Summarize the last three lab panels. Book an annual physical. Everything a member can do by clicking around kp.org, OpenKP exposes as a tool Claude can call on the member's behalf.

The project is explicitly framed through the **Critical AI Health Literacy (CAIHL)** lens. Institutional AI in healthcare has been built primarily to serve institutions: ambient scribes that feed billing systems, prior-authorization bots that deny claims faster, decision-support tools that formalize existing clinical bias. Patient-directed AI flips the orientation. The patient owns the credentials, owns the data, and owns the agent. The record is already theirs by law. OpenKP simply makes it usable in the way every other personal domain has been usable for a decade.

Kaiser's sanctioned FHIR patient API is read-only and capped to USCDI. That limit is a policy choice, not a technical necessity. OpenKP treats it as an artifact of institutional preference rather than a constraint, and works around it by logging in as the patient, using the patient's own credentials, in the same way the patient would if they picked up their phone and opened the Kaiser app.

### What OpenKP is

A local MCP server that runs on the user's Mac, exposes patient-portal actions to Claude via the Model Context Protocol, and keeps all credentials and PHI on the user's machine.

### What OpenKP is not

- **Not a hosted service.** There is no OpenKP server. There is no shared database. The project never holds another person's credentials or health data.
- **Not a replacement for Kaiser.** Every action OpenKP takes is one the user can already take manually.
- **Not a clinical tool.** OpenKP does not diagnose, recommend treatment, or offer medical judgment. It wires a conversational interface to administrative and informational portal actions.
- **Not a product.** It's a patient-advocacy research tool and reference implementation. No fees, no usage caps, no business model.

### Who OpenKP is for (v1)

The v1 audience is **technically-curious KP members and patient-advocacy peers**. People who already use Claude Code, or are willing to install it, and can follow a Claude-Code-guided setup. The install path is "clone the repo and let Claude Code walk you through it." That's the bargain we're offering in v1, and it's a real one. It is not a one-click installer for non-technical users. That work is real and significant, and it is parked at Phase 4.5 to be picked up only if v1 generates the kind of demand that justifies it.

This audience choice is deliberate. It lets us ship something genuinely useful to the people most likely to benefit, without taking on a second job as a software distributor. It also matches the CAIHL ethos: empowering patients who are already practicing critical engagement with health AI, and giving them a tool they can fork, audit, and adapt. Over time, if it proves valuable, we widen the audience.

---

## 2. Principles

These are the non-negotiables. Every design decision traces back to one of them.

1. **Local-first by default.** PHI never leaves the user's machine except on requests to Kaiser. No telemetry. No crash reporting. No "anonymous usage metrics."

2. **The user owns the keys.** Credentials live in the OS keychain or a gitignored `.env` file. OpenKP can read them but cannot exfiltrate them. If OpenKP disappears tomorrow, the user loses nothing.

3. **Writes require confirmation.** No tool that changes state runs without an explicit preview and confirmation. This is enforced at the tool level, not left to prompt discipline.

4. **Everything is auditable.** Every write action is logged locally with timestamp, inputs, and Kaiser's response. The user can always answer "what did my AI do in my record?"

5. **Open source, MIT licensed.** Anyone can fork, inspect, modify, or redistribute. Unlike Open Record's source-available license, OpenKP imposes no commercial or redistribution restrictions.

6. **Stand on shoulders, don't copy code.** Open Record is our conceptual predecessor. We learn from its architecture, its tool surface, and its docs. We do not copy its code.

7. **Graceful failure.** When Kaiser changes an endpoint (and they will), OpenKP fails loudly and clearly, not silently. The user should always know when a tool is broken vs when it worked.

---

## 3. Architecture

The system has four layers. Each layer has a single job and a clean interface to the next.

```
┌─────────────────────────────────────────────────────┐
│ Claude Desktop                                      │
│  (user's natural-language interface)                │
└─────────────────┬───────────────────────────────────┘
                  │ MCP over stdio
                  ▼
┌─────────────────────────────────────────────────────┐
│ mcp_server.py (FastMCP)                             │
│  Tool registry: list_labs, send_message, refill,... │
│  Confirmation & audit decorators                    │
└─────────────────┬───────────────────────────────────┘
                  │ Python function calls
                  ▼
┌─────────────────────────────────────────────────────┐
│ scrapers/request.py                                 │
│  Authenticated httpx client                         │
│  Expiry detection + retry                           │
└──────┬──────────────────────────────────────────────┘
       │                               │
       ▼                               ▼
┌──────────────────┐          ┌────────────────────┐
│ scrapers/        │          │ scrapers/auth.py   │
│ session.py       │◄─────────┤ Playwright + Ping  │
│  Cookie store    │          │ OAuth login        │
│  Re-auth trigger │          └────────────────────┘
└──────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────┐
│ healthy.kaiserpermanente.org                        │
│  Epic MyChart endpoints behind Ping SSO             │
└─────────────────────────────────────────────────────┘
```

### Layer responsibilities

**Layer 1: `auth.py`.** Owns the login flow. Drives a Playwright-managed Chromium with a persistent profile. First run is interactive so the user handles MFA. Subsequent runs reuse the profile silently. Produces a `KaiserSession` object with valid cookies.

**Layer 2: `session.py`.** Owns session lifecycle. Holds the active `KaiserSession`, detects expiry, triggers re-auth. Single source of truth for "are we logged in right now?"

**Layer 3: `request.py`.** Owns authenticated HTTP. Every endpoint module calls `KaiserRequest.get(path)` or `.post(path, json=...)`. Handles cookie injection, expiry detection (redirect to Ping = session dead), and one automatic retry after re-auth.

**Layer 4: `mcp_server.py` and endpoint modules.** Owns the tool surface. One tool per patient-portal action. Each tool is a thin Python function that calls `KaiserRequest`, parses the response into a clean shape, and returns it to Claude. Write tools are wrapped in confirmation and audit decorators.

### Why this shape

The layering means we can substitute any layer without touching the others. If Kaiser adds a new MFA method, only `auth.py` changes. If we decide to swap Playwright for a browser extension later, only `auth.py` changes. If we move from Python to Rust for packaging reasons, the interfaces stay the same.

It also mirrors how the real Kaiser web UI works. Separating auth from request from tool is just recognizing the actual boundaries in the system.

---

## 4. Technology choices

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Fastest iteration for a solo/small-team project. Excellent libraries for HTTP, HTML parsing, and browser automation. Well-understood by the CAIHL-adjacent developer community. |
| MCP framework | `mcp` package (FastMCP) | Official Anthropic SDK. Decorator-based tool registration. Handles stdio and HTTP transports. |
| HTTP client | `httpx` | Async-first, type-safe, modern replacement for `requests`. Cookie jar support matches what we need. |
| Browser automation | Playwright | Free, open source (MIT), backed by Microsoft. Better reliability than Selenium. Handles Ping's JS-heavy login flow. |
| HTML parsing | `beautifulsoup4` + `lxml` | Still the best combination for scraping real-world HTML. |
| Credential storage | `keyring` | System keychain integration on macOS, Windows, Linux. Cross-platform with a single API. |
| Data modeling | `pydantic` | Strong typing for medical record shapes. Cleaner JSON serialization than dataclasses when we need to be strict. |
| Packaging (future) | `.dxt` (Claude Desktop Extension) | Official Anthropic format for one-click MCP distribution to non-technical users. |

All dependencies are free and open source. The only cost of operation is the RAM Chromium uses (~300-500 MB) while a session is being refreshed.

---

## 5. Roadmap

Each phase has an explicit exit criterion. We do not move to the next phase until the current one is demonstrably working.

### Phase 0 — Scaffold (complete)

**Deliverable:** working directory structure, FastMCP server with `ping` and `whoami` tools, empty auth stubs, tests pass.

**Exit criterion:** `openkp` launches, `pytest` is green, Claude Desktop can call `ping` and get back `pong`.

**Status:** done (this session).

### Phase 1 — Auth

**Deliverable:** `auth.py` and `session.py` fully implemented. User can log into Kaiser once, interactively, and subsequent sessions are silent until the trust cookie expires.

**Work:**
1. Capture a live kp.org login in Chrome DevTools, save the HAR — ✅
2. Map the Ping OAuth flow end-to-end — ✅ `docs/research/endpoints/auth.md`
3. Implement `login_interactive()` with Playwright persistent context — ✅
4. Implement silent reuse — ✅ cookies persist to `~/.openkp/session.json`, probed via httpx on startup. Headless Playwright probing was tried and removed — it bounces to Ping because headless fingerprint differs from the headed session. A 30 s keepalive loop (`scrapers/session.py`) keeps the session warm while the MCP server is up.
5. Handle MFA pathways (SMS, email, push, WebAuthn) — ✅ by policy: OpenKP never handles MFA factors in code. The first interactive login happens in a visible Chromium window, the user completes whatever challenge Ping presents, we poll the URL for landing on `/mychartcn/Home`, then capture cookies. This covers every MFA type Ping supports now and in the future, at the cost of requiring one interactive session to seed device trust. See README lines 29-31 and ADR-005.
6. Integration tests — ✅ `tests/test_keepalive.py`, `tests/test_session_persistence.py`, `tests/test_request.py` cover the cookie roundtrip, keepalive cadence, probe alive/dead behavior, and `KaiserRequest`'s retry-on-expiry. Full HAR-replay of the Playwright login flow is not needed for the exit criterion and is deferred.

**Exit criterion:** `whoami` tool returns the username, and a new `session_check` tool confirms we can reach an authenticated endpoint (e.g., kp.org profile) without redirecting to Ping. **Met 2026-04-22.**

**Estimated effort:** 2-4 weeks of focused evenings. Actual: ~1 day end-to-end (helped by the HAR being clear and `/mychartcn/keepalive.asp` being a dead-simple session probe once we stopped trying `/mycare/v1.0/user`).

### Phase 2 — Read-only MVP

**Deliverable:** Four working read tools.

**Tools:**
1. `get_profile()` — demographics, PCP, emergency contacts
2. `list_medications(active_only: bool = True)` — current meds, dosage, prescribing MD, last fill date
3. `list_lab_results(since: date | None = None)` — lab panels with result values, reference ranges, abnormal flags
4. `list_messages(folder: Literal["inbox", "sent"] = "inbox", limit: int = 20)` — message headers
5. `read_message(message_id: str)` — full message body and thread

**Work per tool:** (a) observe the Kaiser web request via DevTools, (b) implement the `KaiserRequest` call, (c) write the parser from response → pydantic model, (d) register the MCP tool, (e) test.

**Exit criterion:** Claude can answer "summarize my last lipid panel and tell me if anything looks off" and get a useful response grounded in real data from your account.

**Estimated effort:** 3-4 weeks.

### Phase 3 — Write tools

**Deliverable:** Four working write tools, each wrapped in confirmation and audit.

**Tools:**
1. `request_refill(medication_id: str)` — queue a refill with the associated pharmacy
2. `send_message(recipient_id: str, subject: str, body: str)` — compose and send a secure message to a care team member
3. `reply_to_message(message_id: str, body: str)` — reply to an existing thread
4. `list_available_slots(provider_id: str, date_from: date, date_to: date)` + `book_appointment(slot_id: str)` — two-step appointment booking

**Safety requirements:**
- Every write tool implements the confirm-before-act pattern (see Section 8)
- Every write writes to `~/.openkp/audit.log` before and after the request
- Dry-run mode via `OPENKP_DRY_RUN=1` returns success without hitting Kaiser

**Exit criterion:** Claude can take "refill my lisinopril and then tell Dr. Chen I'll be traveling next week" and carry it out end-to-end with visible confirmations.

**Estimated effort:** 3-4 weeks.

### Phase 4 — Public release on GitHub

**Audience:** technically-curious KP members and patient-advocacy peers (see Section 1). People who can install Claude Code, or already have it, and follow a Claude-Code-guided setup.

**Deliverable:** A polished public GitHub repo with a README that's readable by a curious human AND structured enough for Claude Code to walk a user through installation end-to-end.

**Work:**
1. README rewrite for the v1 audience: prerequisites, exact `claude_desktop_config.json` paths, first-run Chromium login walkthrough, where credentials live, how to verify it's working, common failure modes.
2. CONTRIBUTING.md and a GitHub Issues template for "Kaiser changed X" reports.
3. CAIHL-framed positioning copy in the README. Lead with patient-directed AI on patient-owned data, not "AI can read my chart."
4. License surface, security disclosure path, code of conduct.
5. Announcement to the CAIHL-adjacent community.

**Out of scope for Phase 4:** bundled installer, `.dxt` packaging, signing/notarization, GUI credential entry, cross-platform builds beyond what currently works on the author's Mac. These move to Phase 4.5 if real demand emerges.

**Exit criterion:** A KP member who knows what a terminal is, with Claude Code installed, can clone the repo and complete a successful end-to-end query of their own record (e.g., "summarize my last lipid panel") without needing to ask the author for help.

**Estimated effort:** 1-2 weeks.

### Phase 4.5 (only if Phase 4 generates demand) — Frictionless installer for non-technical users

**Trigger:** Real, repeated requests from non-technical KP members who want to use OpenKP and cannot via the Phase 4 path. Not built speculatively.

**Deliverable:** A `.dxt` file (or equivalent), bundled Python runtime, bundled Playwright Chromium, signed and notarized installer, install video, cross-platform builds if Windows demand exists.

**Work:** what was originally Phase 4. Pyinstaller bundling, Playwright Chromium bundling, credential-entry UI that writes directly to OS keychain, macOS signing and notarization, `.dxt` packaging, three-minute install video.

**Exit criterion:** A KP member with no programming experience can install OpenKP and complete a successful query within 10 minutes.

**Estimated effort:** 4-6 weeks once cross-platform and signing are taken seriously.

### Phase 5 (optional) — Browser extension architecture

**Deliverable:** A Chrome extension that replaces the Playwright auth layer.

**Rationale:** If the user is already logged into kp.org in Chrome, we can piggyback on their session rather than running our own. This is architecturally cleaner and more robust against Kaiser's auth changes.

**Exit criterion:** Feature parity with the Playwright architecture, with lower maintenance burden.

**Estimated effort:** 4-6 weeks. Only pursued if Phase 4 reveals pain points the extension would solve.

### Phase 6 (research) — Imaging, documents, letters

**Deliverable:** Tools for medical imaging, PDF document retrieval, visit summaries.

**Notes:** Open Record has extensive imaging support (CLO format decoders, eUnity X-ray parsing). This work is genuinely complex and should only be attempted once the MVP has proven its value.

---

## 6. Use Cases

This section catalogs what OpenKP is for: not "retrieve this field from the record," but "help me understand and act on my health." Institutional AI is already built to do the first on behalf of clinicians and payers. OpenKP exists to do the second on behalf of the patient.

The CAIHL frame sets the direction. A patient-directed tool succeeds when it moves the patient from passive recipient of care to informed, prepared, advocating participant in it. Every question below is chosen because it does one of those things.

### The tool surface follows the questions

The tool inventory in Section 7 is downstream of this section. If a question worth asking doesn't map to some combination of tools, the inventory is incomplete. If a tool exists but answers no empowering question, it probably doesn't belong.

Questions come in two shapes:

1. **Direct.** One tool answers.
2. **Synthesized.** Several tools combine. Claude reads across them and composes.

A design implication: **every read tool returns structured, complete data.** Tools don't pre-summarize. Claude is the summarizer. Tools provide material rich enough to answer any reasonable question derived from their domain.

### Six categories of empowerment

Each category is a move the patient is trying to make. The questions are intentionally broad so the same tool surface serves a patient with a chronic cardiac diagnosis, a patient working through a new cancer workup, and a patient in steady-state preventive care alike. The specifics come from the patient's record, not from the tool.

#### 1. Understanding what's there

The record becomes legible. The patient moves from "I have a chart somewhere" to "I understand what's in my chart."

| Question | Tool(s) Claude combines |
|---|---|
| Summarize my health history in plain language. | profile, problems, procedures, visits, documents |
| What conditions appear in my record that I've never discussed with my doctor? | `list_problems`, `list_visits` |
| List every medication I've been prescribed and why. | `list_medications`, `list_visits` |
| What specialists have I seen, and what was the stated reason for each referral? | `list_visits`, `list_documents` |
| Walk me through my [condition] story from diagnosis to now. | `list_problems`, `list_visits`, `list_documents` |

#### 2. Spotting patterns

The AI sees across time. The patient moves from "I vaguely remember those numbers" to "here's a trajectory."

| Question | Tool(s) |
|---|---|
| Which of my labs are trending in a concerning direction, even if they're still "in range"? | `list_lab_results` |
| Compare my vitals across visits. What's changed? | `list_visits` (with vitals), `list_lab_results` |
| Are there symptoms I've reported multiple times to different providers that were never connected? | `list_visits`, `list_documents` |

#### 3. Catching gaps and errors

The patient moves from assuming the record is accurate to actively auditing it.

| Question | Tool(s) |
|---|---|
| What preventive screenings am I due for, given my age, sex, and history? | `list_preventive_care`, `list_immunizations` |
| Are there contradictions between what different providers have written about me? | `list_documents`, `list_visits` |
| Does my medication list match what I'm actually taking? Flag discrepancies. | `list_medications` |
| Are there allergies mentioned in visit notes but never added to my allergy list? | `list_allergies`, `list_documents` |
| What's in my problem list that should probably be resolved or removed? | `list_problems` |

#### 4. Reading between the lines

The CAIHL core. Clinical notes often contain language that follows the patient from visit to visit and shapes their care in ways they never see. Surfacing it is genuinely new territory, and it is the single clearest demonstration of why patient-directed AI differs from institutional AI.

| Question | Tool(s) |
|---|---|
| How do clinicians describe me in their notes? Are there words or framings (noncompliant, anxious, difficult) that might be shaping my care? | `list_documents`, `read_document` |
| What does my referral pattern suggest about how my care team sees my trajectory, even if nobody has said it out loud? | `list_visits`, `list_documents` |
| If you were advocating for me, what's the single most important thing in my record that deserves more attention than it's getting? | all read tools |

#### 5. Preparing for what's next

The patient walks into the next appointment informed rather than reactive.

| Question | Tool(s) |
|---|---|
| I have an appointment with [specialist] next week. What should I bring up, and what questions would a well-prepared patient ask? | `list_visits`, `list_problems`, `list_lab_results`, `list_documents` |
| Based on my history, what tests or workups would a thorough clinician consider that I haven't had? | `list_procedures`, `list_lab_results`, `list_problems` |
| Build me a one-page summary I could hand to a new specialist or second-opinion consult. | all read tools |

#### 6. Acting on my own behalf

Phase 3. The patient exercises agency through the same tool they used to understand the record. Agency here is not a separate mode, it's the natural continuation of comprehension.

| Action | Tool(s) |
|---|---|
| Refill my medication. | `request_refill` |
| Send a message to my care team. | `send_message`, `reply_to_message` |
| Book an appointment. | `list_available_slots`, `book_appointment` |
| Cancel or reschedule an appointment. | `cancel_appointment`, `book_appointment` |
| Update an emergency contact. | `update_emergency_contact` |

### Acceptance criteria per phase

**Phase 2 (read MVP) ships when** Claude can credibly answer, against a real Kaiser account, at least one question from each of categories 1 through 5. A phase-2 bar of "Claude can tell me what meds I'm on" is too weak. The bar is: Claude can produce a one-page summary for a new specialist, surface lab trends the patient missed, flag stale entries on the problem list, and comment meaningfully on how the patient is framed in clinical notes.

**Phase 3 (writes) ships when** Claude can carry out, with confirmation, the actions in category 6.

---

## 7. Tool inventory

The full tool surface we're targeting, grouped by phase. The use cases in Section 6 generate this inventory. If a use case isn't answerable by some combination of tools below, the inventory is incomplete.

### Phase 2 reads

| Tool | Description | Key fields to return |
|---|---|---|
| `get_profile` | Name, DOB, PCP, MRN | `name`, `dob`, `pcp`, `pcp_specialty`, `mrn` |
| `get_insurance` | Active coverage, plan, renewal | `plan_name`, `member_id`, `effective_date`, `renewal_date` |
| `list_medications` | Current and recent medications | `name`, `dose`, `start_date`, `prescriber`, `pharmacy`, `last_fill`, `refills_remaining` |
| `list_lab_results` | Lab panels with values (supports filters: `test_name`, `date_from`, `date_to`) | `test_name`, `value`, `units`, `ref_range`, `abnormal_flag`, `date` |
| `list_allergies` | Known allergies + reactions | `substance`, `reaction`, `severity`, `onset_date` |
| `list_problems` | Active problem list | `name`, `icd10`, `onset_date`, `status`, `managing_provider` |
| `list_procedures` | Surgical and procedural history | `name`, `date`, `performing_provider`, `location`, `cpt` |
| `list_visits` | Past and upcoming appointments (supports filters: `specialty`, `provider`, `date_from`, `date_to`) | `date`, `provider`, `specialty`, `reason`, `summary_document_id` |
| `list_messages` | Message headers | `id`, `from`, `subject`, `date`, `unread`, `thread_id` |
| `read_message` | Full message body + thread | `body`, `attachments`, `full_thread` |
| `list_immunizations` | Vaccination history | `vaccine`, `date`, `series_position`, `administered_by` |
| `list_preventive_care` | Screenings due, overdue, recent | `screening`, `last_done`, `next_due`, `status` |
| `list_emergency_contacts` | Emergency contacts on file | `name`, `relationship`, `phone`, `priority` |
| `list_documents` | Document headers (visit summaries, op notes, imaging reports) | `id`, `type`, `date`, `title`, `encounter_id` |
| `read_document` | Full document content | `body_text`, `images`, `metadata` |

### Phase 3 writes

Every tool in this section follows the confirm-before-act pattern (see Section 8).

| Tool | Description |
|---|---|
| `request_refill` | Queue a medication refill with its associated pharmacy |
| `send_message` | Compose a new secure message to a care team member |
| `reply_to_message` | Reply to an existing thread |
| `list_available_slots` | Open appointment slots for a provider/date range |
| `book_appointment` | Book a slot |
| `cancel_appointment` | Cancel an upcoming appointment |
| `update_emergency_contact` | Edit or add an emergency contact |

### Phase 6 research (maybe)

- Imaging retrieval (DICOM, CLO-format decoding like Open Record does for X-rays)
- `list_bills`, `view_bill`, `pay_bill` (billing and payments)
- Family history, social history
- Referral status and history
- Letters from care team

These extend the catalog but aren't required for the thesis.

---

## 8. Safety patterns

Write tools get three protective layers.

### Confirm-before-act

Every write tool, when called, does two things before hitting Kaiser:

1. **Returns a preview.** The tool first constructs the exact HTTP request it's about to make and returns a human-readable preview to Claude. Claude shows this to the user.
2. **Waits for a confirmation token.** Claude cannot complete the write without passing back a confirmation string that the preview included. This is a belt-and-suspenders guard against a hallucinated tool call.

Example shape in code:

```python
@mcp.tool()
def send_message(recipient_id: str, subject: str, body: str, confirm_token: str = "") -> dict:
    preview = build_preview(recipient_id, subject, body)
    if confirm_token != preview["token"]:
        return {"status": "preview", "preview": preview, "confirm_with": preview["token"]}
    # Second call with matching token proceeds with the real send
    return actually_send(recipient_id, subject, body)
```

### Audit log

Every write action writes two entries to `~/.openkp/audit.log`:

- **Pre-write:** timestamp, tool name, parameters (with PHI redacted or hashed as appropriate), confirm token.
- **Post-write:** timestamp, Kaiser's response code, any confirmation ID Kaiser returns.

The log is append-only. The user can review it at any time to see exactly what OpenKP has done on their behalf.

### Dry-run mode

`OPENKP_DRY_RUN=1` makes every write tool return success without actually sending the request to Kaiser. The audit log still records the intent with a `dry_run: true` flag. This lets the user test prompts and workflows safely.

---

## 9. Privacy and security model

### What lives where

| Data | Location | Protection |
|---|---|---|
| Kaiser username | `.env` file or OS keychain | `.gitignore`, file permissions |
| Kaiser password | OS keychain (preferred) or `.env` | Keychain encryption |
| Session cookies | Playwright profile under `~/.openkp/` | File permissions (user-only) |
| Audit log | `~/.openkp/audit.log` | File permissions (user-only) |
| PHI from responses | In memory only, returned to Claude | Never written to disk by OpenKP |

### What we never do

- Never log credentials
- Never write PHI to disk (except the audit log, and only as parameters the user knowingly passed)
- Never send any data to any server other than Kaiser
- Never phone home with telemetry, crash reports, or usage metrics

### Threat model

**In scope:**
- A misconfigured `.env` accidentally committed to a public repo → mitigated by `.gitignore` and keychain-first credential storage
- A user's Mac is compromised → out of our control, same threat as any other app with kp.org access
- Kaiser changes endpoints → mitigated by loud failures and versioned endpoint maps

**Out of scope:**
- Nation-state actors
- A compromised Anthropic API (Claude Desktop handles this, not us)
- Kaiser suing individual users (legal risk, not security risk)

---

## 10. Distribution strategy

See Section 5 roadmap phases 4, 4.5, and 5 for the full distribution plan. Summary:

1. **Now (Phases 1-3):** Build the read and write tools to a credible MVP. Local install for the author and a small group of contributors.
2. **Phase 4 (next public step):** GitHub release. README structured for both human readers and Claude Code as the install agent. Audience is technically-curious KP members. Mac-first is acceptable in v1 as long as it's called out clearly.
3. **Phase 4.5 (only if demand justifies it):** `.dxt` installer, bundled runtime, GUI credential entry, signing and notarization, install video. Cross-platform if Windows demand emerges.
4. **Phase 5 (maybe):** Chrome extension architecture if Playwright maintenance becomes painful.
5. **Never:** hosted service. The legal and ethical costs outweigh any convenience gain.

---

## 11. Relationship to Open Record

Open Record, by Ryan Hughes at Fan Pier Labs, is the prior art and conceptual source for this project. OpenKP does not use any of Open Record's code. We are deliberately implementing from scratch because:

1. Open Record's license is source-available and restricts commercial use and redistribution. OpenKP is MIT-licensed.
2. Open Record targets vanilla Epic MyChart. Kaiser is a different auth architecture (Ping OAuth in front of Epic). The 896-line MyChart login is not reusable.
3. Open Record is a full web application with Postgres, user accounts, and a Gemini AI proxy. OpenKP is a single-user local tool. The web app scaffolding is overhead we don't need.

**What we learn from Open Record:**
- The tool surface (35+ MCP tools) is a strong scope-setter
- The separation of auth, session, and request is the right architecture
- The `fake-mychart` pattern (mock portal for dev) is worth copying conceptually
- The `openclaw-plugin` variant (fully local, no server) is the correct privacy posture
- Their `CLAUDE.md` and `docs/scraping.md` are thoughtful references

**What we do differently:**
- Python instead of TypeScript/Bun (solo-builder velocity)
- Single-user local instead of multi-tenant web app
- MIT license instead of source-available
- Safety patterns (confirm-before-act, audit log, dry-run) as first-class, not afterthoughts
- CAIHL framing as the organizing purpose, not an incidental benefit

---

## 12. Risks and open questions

### Known risks

1. **Kaiser changes endpoints without warning.** Portals drift. Expect to fix breakage every few months. Mitigation: clear failure modes, versioned endpoint maps, short release cycles.

2. **Ping OAuth MFA is more hostile than expected.** If Kaiser implements device attestation (not just device trust), pure Playwright may be insufficient. Fallback: browser extension architecture (Phase 5).

3. **Kaiser's ToS prohibits automated access.** Personal use on your own account is a legal gray zone. Public distribution at scale increases exposure. Mitigation: keep OpenKP framed as a personal research tool, stay below any threshold of volume that would draw Kaiser's attention, never offer as a hosted service.

4. **Multi-region variance.** Kaiser operates in 8 regions (NorCal, SoCal, Hawaii, NW, CO, GA, Mid-Atlantic, WA). Portal behavior may differ. Mitigation: develop against one region first (the author's), design for pluggable region adapters later.

5. **Maintenance burden.** A single unpaid developer cannot keep pace with an actively maintained portal forever. Mitigation: write clear contribution docs, attract co-maintainers through the CAIHL community, accept that OpenKP may have a finite useful life and that's OK.

### Open questions

1. **Should OpenKP offer a "read-only mode" flag** that disables all write tools, for users who only want summarization and never action?
2. **How do we handle time-sensitive actions like refills?** A refill request is confirmed, but what if it's the wrong med? Is a 60-second "undo" window feasible?
3. **Can the audit log detect anomalies?** If Claude calls `send_message` five times in a minute, should OpenKP warn or block?
4. **What's the right way to share region adapters?** A single repo with plugins, or separate repos per region?
5. **How do we contribute back to the CAIHL literature?** Paper, blog post, workshop submission, all of the above?

---

## 13. Success metrics

### Phase 2 (read MVP)

- A Kaiser member can ask Claude "what are my current medications" and get an accurate answer drawn from their actual account.
- The author uses OpenKP at least weekly for real health-record questions, not just as a demo.

### Phase 3 (writes)

- The author can complete a real refill request through Claude without ever opening kp.org.
- The audit log contains at least 50 real actions with zero unintended side effects.

### Phase 4 (GitHub release)

- 10+ technically-curious KP members outside the author's immediate network clone the public repo, use Claude Code to walk through setup, and complete a successful end-to-end query of their own record.
- At least one CAIHL-adjacent publication or talk references OpenKP as a patient-directed AI case study.
- No first-time-setup support escalations to the author. The README + Claude Code path is sufficient.

### Phase 4.5 (frictionless installer, only if pursued)

- 10+ non-technical KP members install via `.dxt` and complete a successful action without ever opening a terminal.

### Long-term (2+ years)

- The existence of OpenKP measurably contributes to a shift in industry posture, even marginally. Ideally a Kaiser product person sees it and argues internally for expanded patient API scope.
- CAIHL teaching materials cite OpenKP as the canonical worked example of patient-directed AI.

---

## 14. Credits and references

**Prior art:**
- Ryan Hughes / Fan Pier Labs, [Open Record](https://github.com/Fan-Pier-Labs/openrecord)
- Brendan Keeler, ["The Scrapers At MyChart's Gate"](https://healthapiguy.substack.com/p/the-scrapers-at-mycharts-gate)

**Theoretical foundation:**
- The Critical AI Health Literacy (CAIHL) framework, drawing on Freirean critical pedagogy applied to patient-AI relationships
- Test Patient's prior advocacy work on patient data rights

**Technical references:**
- Model Context Protocol specification
- PingFederate OAuth 2.0 / OIDC specs
- Epic MyChart public documentation (what little exists)

---

## Appendix A: Directory structure

```
~/OpenKP/
├── DESIGN.md                   ← this document
├── docs/
│   ├── adr/                    ← architecture decision records
│   │   ├── 001-build-fresh-vs-fork.md
│   │   ├── 002-python-fastmcp-playwright.md
│   │   ├── 003-local-first-no-hosted-service.md
│   │   └── 004-writes-require-confirm-token.md
│   ├── recon/
│   │   └── session-1.md        ← initial feasibility analysis
│   └── research/               ← scratch pad (endpoints, captures, reading)
├── scripts/
│   └── cleanup.sh              ← idempotent workspace cleanup
└── openkp/                     ← our project
    ├── pyproject.toml
    ├── README.md
    ├── .env.example
    ├── .gitignore
    ├── src/openkp/
    │   ├── __init__.py
    │   ├── config.py
    │   ├── mcp_server.py
    │   └── scrapers/
    │       ├── __init__.py
    │       ├── auth.py
    │       ├── session.py
    │       └── request.py
    ├── fake_kp/                ← mock portal for dev (Phase 2)
    └── tests/
        └── test_config.py
```

## Appendix B: Key technical decisions and why

| Decision | Alternative considered | Why we chose this |
|---|---|---|
| Python over TypeScript | TypeScript (matches Open Record) | Faster solo iteration; Playwright Python is mature; simpler packaging for `.dxt` |
| Playwright over pure HTTP | Raw httpx + Ping OAuth library | Ping's JS-driven login and device fingerprinting make pure HTTP fragile; Playwright pays the reliability tax upfront |
| MIT license | Source-available | We want this to be reusable by anyone, including researchers and other patient-advocacy projects |
| Single-user local | Multi-tenant hosted | Privacy ethics and legal liability both favor local; also sidesteps HIPAA BAA complexity |
| FastMCP over raw MCP SDK | Raw MCP SDK | Decorator syntax is cleaner; official Anthropic recommendation |
| Keychain for credentials | Encrypted `.env` with master password | OS keychain is the system's existing answer to this problem; no need to reinvent |
| Confirm-before-act via token | Claude-side confirmation prompt | Enforcing at the tool level means a hallucinated tool call can't actually execute a write |

## Appendix C: Links to follow-up work

- `docs/recon/session-1.md` — what we learned about Open Record and Kaiser's auth
- `docs/adr/` — architecture decision records (001–004)
- `openkp/README.md` — how to install and run
- Open Record source is no longer in the workspace (removed by `scripts/cleanup.sh`). Refer to the upstream repo at https://github.com/Fan-Pier-Labs/openrecord for its `CLAUDE.md` and `docs/scraping.md`
