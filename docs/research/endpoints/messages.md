# Messages endpoint

Source HAR: `docs/research/captures/kp-messages-2.har`, 2026-04-23.

## Summary

| Feature | Endpoint | Status |
| --- | --- | --- |
| Folder list (badge counts) | `POST /mychartcn/api/conversations/GetFoldersList` | 🟡 Mapped, not yet surfaced as a tool |
| List threads (+ search + pagination) | `POST /mychartcn/api/conversations/GetConversationList` | ✅ Mapped, implemented |
| Read one thread (all messages) | `POST /mychartcn/api/conversations/GetConversationDetails` | ✅ Mapped, implemented |
| Compose / reply | unknown | 🔴 Phase 3 write, not captured |
| Download attachment | unknown | 🔴 Phase 3, not captured |

MCP tools registered: `list_messages`, `read_message`.

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
      "userOverrideNames": {"WP-24...": "DR. ANDREA PROVIDERONE"},
      "organizationId": "WP-24...",
      "messages": [
        {
          "wmgId": "WP-24...",
          "isUnread": true,
          "deliveryInstantISO": "2025-10-02T12:30:00Z",
          "body": "<p>HTML body...</p>",
          "author": {"displayName": "DR. ANDREA PROVIDERONE", "empKey": "WP-24..."},
          "attachments": []
        }
      ]
    }
  ],
  "users": {"WP-24...": {"name": "DR. ANDREA PROVIDERONE", "providerId": "..."}},
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

OpenKP surfaces only `name` and `file_extension` for now. Downloading is a
future concern — probably needs a POST with `dcsId`/`etxId` we haven't
captured yet.

## Known unknowns

- **Download endpoint for attachments.** Not in the captured HAR. Would need
  a capture of clicking an attachment.
- **Compose / reply endpoint.** Phase 3 write. Not captured.
- **Mark-as-read.** When the user opens a thread in the UI, Kaiser probably
  fires a side call to flip `isUnread`. Not captured in our HAR (or it may
  happen implicitly on `GetConversationDetails`). If important, a follow-up
  capture of opening an unread thread with the Network panel active would
  clarify.
- **`legacyMessageDetailsUrl`.** Every conversation carries one. Appears to
  be a URL into the legacy UI. We ignore it.

## PHI discipline

Message bodies are PHI. The scraper never logs them and returns stripped
text only through the MCP tool surface (encrypted stdio transport to Claude
Desktop). Test fixtures use fabricated content ("Follow-up note from the
provider," "Your visit is tomorrow at 10am"). No real subject lines, sender
names, or body text in version control.
