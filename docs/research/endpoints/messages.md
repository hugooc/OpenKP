# Messages endpoint

Source HAR: `docs/research/captures/kp-messages-2.har`, 2026-04-23.

## Summary

| Feature | Endpoint | Status |
| --- | --- | --- |
| Folder list (badge counts) | `POST /mychartcn/api/conversations/GetFoldersList` | 🟡 Mapped, not yet surfaced as a tool |
| List threads (+ search + pagination) | `POST /mychartcn/api/conversations/GetConversationList` | ✅ Mapped, implemented |
| Read one thread (all messages) | `POST /mychartcn/api/conversations/GetConversationDetails` | ✅ Mapped, implemented |
| Compose new message | `GetComposeId` → `SaveMedicalAdviceRequestDraft` → `SendMedicalAdviceRequest` | ✅ Mapped, implemented (mail-only, see §send) |
| Discard draft | `RemoveComposeId` + `DeleteMedicalAdviceRequestDraft` | ✅ Mapped, implemented |
| List recipients | `GetMedicalAdviceRequestRecipients` | ✅ Mapped, implemented |
| List topics | `GetSubtopics` | ✅ Mapped, implemented |
| Reply to thread | unknown | 🔴 Not yet captured |
| Download attachment | `GetDocumentDetailsLegacy` → `GET /Documents/ViewDocument/Download` | ✅ Mapped, implemented |

MCP tools registered: `list_messages`, `read_message`, `download_message_attachment`,
`list_message_recipients`, `list_message_topics`, `send_message`.

## Auth / anti-forgery — two separate tokens required

Learned the hard way during session 5 live-test: these endpoints need
**both** a CSP page nonce AND a classic anti-forgery CSRF token. Sending
only one returns a 302 redirect to `/mychartcn/Home/FiveHundred`
(ASP.NET's unhandled-exception page), which is how Kaiser's middleware
tells us the request was rejected.

### Token 1: CSP page nonce (goes in the JSON body as `PageNonce`)

Hosted on the communication-center HTML page:

```
GET https://healthy.kaiserpermanente.org/mychartcn/app/communication-center
```

The response HTML contains one or more `<style nonce="..."/>` tags. The
value is a 32-character hex string (CSP's standard per-request nonce).
OpenKP pulls it with a regex: `nonce=['"]([a-f0-9]{16,})['"]`. Any style
or script tag works — they share the same nonce per page load.

### Token 2: CSRF anti-forgery token (goes in the `__RequestVerificationToken` header)

Same pattern we use for CareTeam/Load. Fetched from a dedicated endpoint:

```
GET https://healthy.kaiserpermanente.org/mychartcn/Home/CSRFToken?noCache=<random>
```

Response is an HTML fragment:

```html
<input name="__RequestVerificationToken" type="hidden" value="..." />
```

Implemented once, shared across scrapers in `openkp/scrapers/csrf.py`.

### Full flow for one conversations call

1. GET `/mychartcn/app/communication-center` → extract CSP nonce.
2. GET `/mychartcn/Home/CSRFToken` → extract anti-forgery token.
3. POST `/mychartcn/api/conversations/<endpoint>` with:
   - `__RequestVerificationToken: <token from step 2>` header
   - JSON body containing `PageNonce: "<nonce from step 1>"`

Three round trips per tool call. Cheap, and the alternative (caching
tokens across calls) adds state management we'd rather not maintain until
performance requires it.

## Folder tag map (empirical)

`GetFoldersList` returns the user's folders as `{tag: int, totalCount: int,
badgeCount: int}`. The tags are Epic's internal folder IDs; we matched them
against the UI sidebar labels:

| tag | OpenKP folder name | Kaiser UI label |
| --- | --- | --- |
| 1 | `inbox` | Conversations |
| 2 | `archive` | Archive |
| 3 | `bookmarked` | Bookmarked |
| 6 | `automated` | Automated messages |
| 7 | `appointments` | Appointments |

No "Sent" folder is exposed — Kaiser's UI has a "Send a message" button but
doesn't expose sent items as a folder here. If a future recon finds one,
add its tag to `FOLDER_TAGS`.

## `POST /mychartcn/api/conversations/GetConversationList`

**URL:** `https://healthy.kaiserpermanente.org/mychartcn/api/conversations/GetConversationList`

**Headers:**

```
Accept: application/json
Content-Type: application/json
Origin: https://healthy.kaiserpermanente.org
Referer: https://healthy.kaiserpermanente.org/mychartcn/app/communication-center
X-Requested-With: XMLHttpRequest
__RequestVerificationToken: <token from /mychartcn/Home/CSRFToken>
```

**Request body (JSON):**

```json
{
  "tag": 1,
  "localLoadParams": {
    "loadStartInstantISO": "",
    "loadEndInstantISO": "",
    "pagingInfo": 1
  },
  "externalLoadParams": {},
  "searchQuery": "",
  "PageNonce": "<32-char hex from HTML>"
}
```

- `tag`: folder tag (see table above).
- `searchQuery`: empty string = unfiltered. Kaiser matches against subject,
  body, and sender.
- `localLoadParams.loadStartInstantISO`: pagination cursor. Empty = newest
  page. Pass the `deliveryInstantISO` of the oldest message from the previous
  page to step backward.
- `localLoadParams.loadEndInstantISO`: always empty in observed traffic.
  Likely reserved for future date-range filtering.
- `pagingInfo`: always `1` in observed traffic. Hardcoded.
- `externalLoadParams`: always `{}` in observed traffic. Hardcoded.

**Response shape (trimmed to the fields we actually use):**

```json
{
  "legacyXUnreadCount": 1,
  "conversations": [
    {
      "hthId": "WP-24...",
      "subject": "...",
      "previewText": "...",
      "tags": {"Unread": true, "System": true},
      "hasAttachments": false,
      "hasTasks": false,
      "hasUrgentMsgs": false,
      "userKeys": ["WP-24..."],
      "viewerKeys": ["WP-24..."],
      "userOverrideNames": {"WP-24...": "DR. EXAMPLE PROVIDER"},
      "organizationId": "WP-24...",
      "messages": [
        {
          "wmgId": "WP-24...",
          "isUnread": true,
          "deliveryInstantISO": "2025-10-02T12:30:00Z",
          "body": "<p>HTML body...</p>",
          "author": {"displayName": "DR. EXAMPLE PROVIDER", "empKey": "WP-24..."},
          "attachments": []
        }
      ]
    }
  ],
  "users": {"WP-24...": {"name": "DR. EXAMPLE PROVIDER", "providerId": "..."}},
  "viewers": {"WP-24...": {"name": "PATIENT NAME", "isSelf": true}}
}
```

- `conversations[].hthId` is the thread handle. Pass it to `read_message` /
  `GetConversationDetails`.
- `conversations[].messages[0]` is the latest message inlined, which includes
  its body. That means the list view already has enough to show a full
  preview — no second round trip needed for a summary card.
- `users` and `viewers` are dicts keyed by obfuscated user keys. The values
  have a `name` field (display name). `userOverrideNames` holds direct
  string mappings and wins over `users`/`viewers`.

### `localSummary` is the pagination contract

Every `GetConversationList` response carries a `localSummary` object that
tells the caller how to walk pagination:

```json
{
  "localSummary": {
    "hasMoreConversations": true,
    "newestLoadedInstantISO": "2026-03-09T23:04:35Z",
    "oldestLoadedInstantISO": "2024-05-09T18:34:09Z",
    "oldestSearchedInstantISO": "2024-05-09T18:34:09Z",
    "numberLoaded": 50,
    "pagingInfo": 0
  }
}
```

- `hasMoreConversations`: keep paginating?
- `oldestSearchedInstantISO`: cursor for the **next** page. Pass this back as
  `loadStartInstantISO` to step into older history. **It advances even when
  the current page returns zero results** — that's the trick that lets
  search reach archival threads (see deep-search section below).
- `oldestLoadedInstantISO`: oldest result actually returned in this page,
  empty when `numberLoaded == 0`.
- `numberLoaded`: thread count in this page (after server-side `searchQuery`
  filtering, if any).

### `searchQuery` is page-scoped, not index-scoped

The biggest gotcha in this endpoint: `searchQuery` filters within the page
that `loadStartInstantISO` selects, not across the whole inbox. With an
empty cursor, you load the most recent ~50 threads and the search runs
only against those. Older matches are invisible.

The KP UI handles this with a "Search more conversations" link that walks
pagination via `oldestSearchedInstantISO` until Kaiser sets
`hasMoreConversations: false`. There is **no separate global-search
endpoint** — it's the same `GetConversationList` endpoint, called
repeatedly with progressively older cursors.

We mirror this with `fetch_messages(deep_search=True, max_pages=N)`. Source
HAR: `docs/research/captures/kp-messages-deepsearch-1.har`, 2026-04-25,
captured by searching "genetic" against an account where the matching
threads are ~3 years old.

Walk algorithm (matches what the UI does):

```
cursor = before_iso or ""
for _ in range(max_pages):
    threads, summary = fetch_one_page(cursor, searchQuery)
    accumulate threads (dedupe by hthId)
    if not summary.hasMoreConversations: break
    next_cursor = summary.oldestSearchedInstantISO
    if not next_cursor or next_cursor == cursor: break  # avoid infinite loop
    cursor = next_cursor
```

Reuse the same `PageNonce` and CSRF token across every page in one walk —
the UI does, and refetching them per page would triple the round-trip cost.

## `POST /mychartcn/api/conversations/GetConversationDetails`

**URL:** `https://healthy.kaiserpermanente.org/mychartcn/api/conversations/GetConversationDetails`

**Headers:** same as GetConversationList.

**Request body (JSON):**

```json
{
  "id": "<hthId from list>",
  "messageId": "",
  "organizationId": "",
  "PageNonce": "<nonce>"
}
```

- `id`: the thread's `hthId`.
- `messageId`: empty in observed traffic. Possibly used to deep-link to one
  message within a thread. We don't use it.
- `organizationId`: empty in observed traffic. Kaiser appears to resolve it
  from the thread id.

**Response shape (trimmed):**

Same top-level fields as a single entry in `conversations[]` from the list
endpoint, plus:

```json
{
  "totalMessages": 2,
  "replyFlags": {"canReply": true, "cannotReplyReason": ""},
  "hasPreviouslyViewed": true,
  "lastViewedByStaffInstantISO": "...",
  "messageIdUsedToLoad": "",
  "messages": [
    {
      "wmgId": "...",
      "isUnread": false,
      "deliveryInstantISO": "2025-10-03T09:15:00Z",
      "body": "<p>...</p>",
      "author": {"displayName": "...", "empKey": "..."},
      "attachments": [],
      "tasks": [],
      "suggestedActions": []
    }
  ]
}
```

Messages arrive newest-first. Every message has its own `wmgId`.

## Message bodies are HTML

Observed: all message `body` values start with `<` and contain standard tags
(`<p>`, `<br>`, `<a>`, inline styles). The scraper strips HTML to plain text
via `bs4`:

1. `<br>` → `\n`.
2. Block-level tags get a blank line inserted before them so paragraphs stay
   visually separated.
3. `get_text(separator=" ")` keeps inline text cohesive.
4. Runs of spaces and blank lines are collapsed for readability.

The stripped plain text is what surfaces to Claude as `body_text`. The raw
HTML is discarded. If we ever need HTML for rendering, we'd add a separate
`body_html` field — not doing it speculatively.

## Attachment shape

Observed structure:

```json
{
  "type": 1,
  "dcsId": "...",
  "etxId": "...",
  "name": "Screenshot",
  "fileExtension": "jpg",
  "legacyUrlForCommunityJump": "...",
  "organizationId": "..."
}
```

OpenKP surfaces `name`, `file_extension`, `dcs_id`, `attachment_type`, and
`organization_id`. `dcs_id` is the handle for `download_message_attachment`.

## Attachment download chain

Source HAR: `docs/research/captures/kp-email-attachment-download.har`,
2026-04-25. Captured by clicking the attachment chip on a 2023 genetics
counseling thread.

Two real requests beyond the existing `read_message` flow (the rest of the
HAR is Quantum Metric analytics noise):

### Step 1: `POST /mychartcn/api/documents/viewer/GetDocumentDetailsLegacy`

**Request body:**

```json
{
  "dcsId": "<from message attachment>",
  "fileExtension": "PDF",
  "organizationId": "",
  "useOldMobileLink": false
}
```

Headers: same `__RequestVerificationToken` + `Content-Type: application/json`
+ `Origin` + `Referer` shape as `GetConversationDetails`. **No `PageNonce`
required** — only the conversation endpoints need it.

**Response shape:**

```json
{
  "dcsId": "...",
  "token": "...",                          // Internal handle, ignore
  "orgId": "",
  "displayName": "...",                    // Use as filename
  "userFriendlyDisplayName": "",
  "legacyEncryption": true,                // Just signals which crypto Kaiser uses
  "isMobile": false,
  "fileDescription": "...",
  "allowPreview": false,
  "downloadUrl": "/Documents/ViewDocument/Download?dcsid=...&displayName=...&dcsExt=PDF",
  "previewUrl": "",
  "mimeType": "application/pdf"
}
```

`downloadUrl` arrives without the `/mychartcn` prefix. Prepend it before the
GET (same gotcha as labs PDF download).

### Step 2: `GET /mychartcn/Documents/ViewDocument/Download?dcsid=...&displayName=...&dcsExt=PDF`

Returns the binary file. Server sends `Content-Disposition: attachment;
filename="..."` and the correct `Content-Type` (e.g. `application/pdf`).
We send `Accept: application/pdf,*/*` even though the browser used a
generic `text/html` accept — Kaiser doesn't care.

### Why `Legacy`?

The endpoint name `GetDocumentDetailsLegacy` is distinct from the
`GetDocumentDetails` lab-result PDFs use. The response includes
`legacyEncryption: true`, which suggests these documents predate Kaiser's
newer document-store migration. Behaviorally identical for our purposes —
the chain is simpler than the lab PDF flow because there's no on-demand
generation step (message attachments are static files Kaiser stored once).

### What's missing vs lab PDFs

- **No `GetDocumentGenerationInfo` step.** Message attachments are static.
- **No `generation_in_progress` outcome.** Either the file is there or
  there's an error.
- **No `no_pdf_available` outcome.** The caller already has a `dcs_id` from
  `read_message`, which means the attachment exists.

## Send new message — `SendMedicalAdviceRequest` and friends

Source HAR: `kp-send-message-to-provider.har` (real send, 2026-05-03) +
`kp-compose-message2.har` (compose-and-discard with topic switch + attachment
upload, 2026-05-03).

Kaiser's UI calls this flow "Non-Urgent Medical Advice" / "Message your care
team" — internally it's a **medical advice request**, distinct from the
generic conversations API used for read paths. All endpoints sit under
`/mychartcn/api/medicaladvicerequests/*` (and a few helpers under
`/mychartcn/api/conversations/*`). Same anti-forgery contract as the read
endpoints (`__RequestVerificationToken` header), but **no `PageNonce`** in
the body.

> **Response-body capture gap.** Chrome DevTools' HAR exporter has a small
> circular buffer for response bodies. The 690 KB `GetConversationList`
> response that fires when the inbox loads evicts every other body. So the
> request-body shapes below are verified against live Kaiser, but the
> **response shapes are inferred** (from response sizes, the IDs that
> subsequent requests echo back, and the labels visible in the UI). The
> first live test will validate them — see "Open response-shape questions"
> at the end of this section.

### Flow at a glance

```
                      ┌─────────────────────────────────────┐
                      │ POST GetComposeId                   │ → composeId
                      └─────────────────────────────────────┘
                                     │
                      ┌─────────────────────────────────────┐
                      │ POST GetSubtopics  (catalog)        │ ↘
                      │ POST GetMedicalAdviceRequestRecipi… │  prep
                      │ POST GetViewers                     │ ↗
                      └─────────────────────────────────────┘
                                     │
                      ┌─────────────────────────────────────┐
                      │ POST SaveMedicalAdviceRequestDraft  │ → conversationId  (1st save)
                      └─────────────────────────────────────┘
                                     │
                      ┌─────────────────────────────────────┐
                      │ POST SendMedicalAdviceRequest       │ → 200 = sent
                      └─────────────────────────────────────┘
                                     │
                      ┌─────────────────────────────────────┐
                      │ POST RemoveComposeId  (cleanup)     │
                      └─────────────────────────────────────┘
```

Discard path (close compose without sending):

```
RemoveComposeId  →  DeleteMedicalAdviceRequestDraft({conversationId})
```

OpenKP collapses the prep + draft + send into one `send_message` call. We
skip the `GetMessageMenuSettings` / `LogMessageMenuItemSelected` /
`GetDisclaimer` / `GetConversationsToContinue` calls — they're UI-only
chrome.

### `POST /mychartcn/api/conversations/GetComposeId`

Generates a per-compose token that ties the subsequent SaveDraft / Send /
RemoveComposeId calls together. Without it the SaveDraft endpoint rejects.

**Request body:** `{}` (literally empty object).

**Response (inferred, ~126 bytes):** `{"composeId": "WP-…"}` — the token is
~100 chars of `WP-`-prefixed encoded bytes (Kaiser's standard ID format).

### `POST /mychartcn/api/medicaladvicerequests/GetSubtopics`

Returns the topic catalog — the dropdown labelled "Reason for Message" in
the UI.

**Request body:**

```json
{ "organizationId": "" }
```

**Response shape (verified live, 2026-05-03):**

```json
{
  "topicList": [
    {"value": "97",  "displayName": "Test Results"},
    {"value": "98",  "displayName": "Medication"},
    {"value": "99",  "displayName": "Visit Follow-Up"},
    {"value": "100", "displayName": "Upcoming Appointment or Procedure"},
    {"value": "101", "displayName": "Non-Urgent Medical Advice"}
  ],
  "organizationId": ""
}
```

| `value` | `displayName` |
| --- | --- |
| `97`  | Test Results |
| `98`  | Medication |
| `99`  | Visit Follow-Up |
| `100` | Upcoming Appointment or Procedure |
| `101` | Non-Urgent Medical Advice |

Two notable parser gotchas the first live call exposed:

- Wrapper key is **`topicList`** (camelCase, capital L). Not `topics` /
  `subtopics` / `data`.
- Title field is **`displayName`**, not `title`. Our parser now accepts
  `displayName` first, then `title` / `name` / `label` for forward compat.

### `POST /mychartcn/api/medicaladvicerequests/GetMedicalAdviceRequestRecipients`

Returns the providers and pools the patient is allowed to message.

**Request body:**

```json
{ "organizationId": "" }
```

**Response shape (one recipient, inferred from the verbatim shape echoed
back in subsequent SaveDraft / Send bodies):**

```json
{
  "displayName": "DR. EXAMPLE PROVIDER",
  "userId":      "WP-…",
  "poolId":      "",
  "providerId":  "WP-…",
  "departmentId":""
}
```

The 6.6 KB response size is consistent with ~10–15 recipients (PCP,
specialists with prior visits, advice-line pools). Order in the UI screenshot:
PCP first, then alphabetical.

### `POST /mychartcn/api/medicaladvicerequests/GetViewers`

Returns the patient's own self-viewer ID, used as the sole entry in the
SaveDraft / Send `viewers` array. (The "viewers" mechanism supports proxy
access — e.g. a parent messaging on behalf of a minor child — but for v1
we always send self-viewer.)

**Request body:**

```json
{ "organizationId": "" }
```

**Response (inferred):** `{"viewers": [{"wprId": "WP-…", "displayName": "…", "isSelf": true, …}], …}`

Total 323 bytes, so very thin.

### `POST /mychartcn/api/medicaladvicerequests/SaveMedicalAdviceRequestDraft`

Saves the in-progress message. The first call returns Kaiser's
`conversationId`, which subsequent calls (and the final Send) echo back.
The MyChart UI fires this on every keystroke — OpenKP fires it once.

**Request body:**

```json
{
  "recipient": {
    "displayName": "DR. EXAMPLE PROVIDER",
    "userId":      "WP-…",
    "poolId":      "",
    "providerId":  "WP-…",
    "departmentId":""
  },
  "topic":          { "title": "Non-Urgent Medical Advice", "value": "101" },
  "conversationId": "",
  "organizationId": "",
  "viewers":        [ { "wprId": "WP-…" } ],
  "messageBody":    [ "" ],
  "messageSubject": "",
  "documentIds":    [],
  "includeOtherViewers": false,
  "composeId":      "WP-…"
}
```

- `messageBody` is an **array of strings**, one per line (not a single
  string). Empty lines preserved as `""`. The KP UI splits by newline and
  posts the array verbatim.
- `conversationId` empty on the first call; populated thereafter.
- `documentIds` is the file IDs returned by `UploadFile` (out of scope for
  v1 send).

**Response (inferred, ~123 bytes):** `{"conversationId": "WP-…"}` (~100 chars
token + JSON wrapping).

### `POST /mychartcn/api/medicaladvicerequests/SendMedicalAdviceRequest`

The actual send. Same payload shape as SaveDraft, but `conversationId`
must be the one SaveDraft just returned. After this returns 200, the
message is in the recipient's queue.

**Request body:** identical to SaveDraft, with `conversationId` populated
and `messageSubject` + `messageBody` finalized.

**Response (inferred, ~86 bytes):** unknown — likely `{"success": true}` or
`{"sent": true}` or the conversationId echoed back. We only check HTTP 200,
so the body shape is non-load-bearing.

### `POST /mychartcn/api/conversations/RemoveComposeId`

Releases the compose token. Always called after Send (and after a
discard).

**Request body:**

```json
{ "composeId": "WP-…" }
```

**Response (~126 bytes):** ack, shape unknown.

### `POST /mychartcn/api/medicaladvicerequests/DeleteMedicalAdviceRequestDraft`

Discard path only. Fires after the user picks "Discard draft" in the
"Message in progress" confirmation dialog. The saved draft is permanently
removed.

**Request body:**

```json
{ "conversationId": "WP-…", "organizationId": "" }
```

OpenKP doesn't currently expose this — `send_message` either commits or
short-circuits on preview, neither leaves a draft behind. If we ever add a
draft-only mode, this is the rollback hook.

### `POST /mychartcn/api/medicaladvicerequests/UploadFile`

Captured but **not implemented in v1**. The UI uses this to attach files
(passport.jpg in our compose-2 capture). Returns a document ID:

```
WP-24l0-2FcBmgy5XC6sSNTcoY1tQ-3D-3D-24ZhsRP4d16cTleIiYIA2ACtCh4Jnnrsvseq0e2DfK3g0-3D
```

…that the next SaveDraft includes in `documentIds[]`. Future work.

### Open response-shape questions

The first live calls to the new tools will resolve these. Permissive
parsers in `messages.py` log the raw response when an unexpected shape
appears, so we capture a real example without breaking the user's call.

1. ~~**`GetSubtopics` envelope.** Is it `[{title,value},…]` (raw array) or
   `{topics:[…]}` or `{subtopics:[…]}`?~~ **Resolved 2026-05-03**:
   `{"topicList": [{"value":..., "displayName":...}], "organizationId":""}`.
2. **`GetMedicalAdviceRequestRecipients` envelope.** Same question — top-level
   array vs. wrapper object. (Live call returned 12 recipients cleanly,
   so the recursive fallback caught it; exact wrapper key not yet confirmed.)
3. **`GetViewers` field name for self-viewer wprId.** Is it `wprId` or
   `viewerId`? Both have appeared in Epic captures elsewhere.
4. **`SendMedicalAdviceRequest` success body.** 86 bytes — likely a tiny ack
   or status flag. Unimportant for correctness (we use HTTP 200 as truth)
   but worth recording for diagnostics.

## Other known unknowns

- **Reply to an existing thread.** The send flow above starts a *new*
  conversation. The "Reply" button on an opened thread almost certainly
  hits a different endpoint that takes the parent thread id. Needs a fresh
  HAR.
- **Mark-as-read.** When the user opens a thread in the UI, Kaiser probably
  fires a side call to flip `isUnread`. Not captured.
- **`legacyMessageDetailsUrl`.** Every conversation carries one. Appears to
  be a URL into the legacy UI. We ignore it.
- **Send-flow attachments.** `UploadFile` body shape (multipart, not JSON)
  is not implemented. v1 `send_message` rejects calls that include
  attachments.

## PHI discipline

Message bodies are PHI. The scraper never logs them and returns stripped
text only through the MCP tool surface (encrypted stdio transport to Claude
Desktop). Test fixtures use fabricated content ("Follow-up note from the
provider," "Your visit is tomorrow at 10am"). No real subject lines, sender
names, or body text in version control.
