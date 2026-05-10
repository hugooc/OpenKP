"""Messaging scraper: list threads + read a single thread.

Kaiser's Message Center is Epic MyChart under the hood. The two endpoints we
use both live at `/mychartcn/api/conversations/*` and require a CSP nonce
extracted from the communication-center HTML page as a `PageNonce` field in
the JSON body.

Flow:

  1. GET /mychartcn/app/communication-center         -> HTML page with <style nonce="...">
  2. POST /mychartcn/api/conversations/GetConversationList {tag, searchQuery, ...}
     OR
     POST /mychartcn/api/conversations/GetConversationDetails {id, ...}

Response bodies are JSON. Message bodies themselves are HTML — we strip to
plain text via bs4 so Claude gets clean input.

PHI discipline: never log message bodies, subjects, or sender names. Never
put real content in test fixtures.

Docs: `docs/research/endpoints/messages.md`
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from openkp.safety import audit_log_event, is_dry_run
from openkp.scrapers.csrf import fetch_csrf_token
from openkp.scrapers.request import KaiserRequest

logger = logging.getLogger(__name__)

# Page that hosts the CSP nonce we need to unlock the JSON APIs.
PAGE_PATH = "/mychartcn/app/communication-center"
LIST_PATH = "/mychartcn/api/conversations/GetConversationList"
DETAILS_PATH = "/mychartcn/api/conversations/GetConversationDetails"
PAGE_REFERER = "https://healthy.kaiserpermanente.org/mychartcn/app/communication-center"

# Attachment-download chain. `Legacy` variant is what the message-center UI
# uses — distinct from the `GetDocumentDetails` that lab-result PDFs hit.
# Message attachments are static files (no on-demand generation step).
DOCDETAILS_LEGACY_PATH = "/mychartcn/api/documents/viewer/GetDocumentDetailsLegacy"

# Send-message ("Non-Urgent Medical Advice") endpoint paths. All require the
# CSRF anti-forgery header but, unlike GetConversationList / Details, do NOT
# need a PageNonce in the JSON body. See messages.md "Send new message".
COMPOSE_ID_PATH = "/mychartcn/api/conversations/GetComposeId"
COMPOSE_REMOVE_PATH = "/mychartcn/api/conversations/RemoveComposeId"
SUBTOPICS_PATH = "/mychartcn/api/medicaladvicerequests/GetSubtopics"
RECIPIENTS_PATH = "/mychartcn/api/medicaladvicerequests/GetMedicalAdviceRequestRecipients"
VIEWERS_PATH = "/mychartcn/api/medicaladvicerequests/GetViewers"
DRAFT_SAVE_PATH = "/mychartcn/api/medicaladvicerequests/SaveMedicalAdviceRequestDraft"
SEND_PATH = "/mychartcn/api/medicaladvicerequests/SendMedicalAdviceRequest"

# Where attachment binaries land. Same directory as lab PDFs — fewer surprises
# for callers that handle both, and the displayName disambiguates.
DEFAULT_DOWNLOAD_DIR = Path.home() / ".openkp" / "downloads"

# Characters unsafe for a filesystem path across the common cases.
_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# Folder name → Kaiser integer tag. Observed empirically in
# `docs/research/captures/kp-messages-2.har` GetFoldersList response, matched
# against the sidebar labels in the UI.
FOLDER_TAGS: dict[str, int] = {
    "inbox": 1,          # Kaiser calls this "Conversations"
    "archive": 2,
    "bookmarked": 3,
    "automated": 6,      # "Automated messages"
    "appointments": 7,
}

# Kaiser returns at most 50 conversations per GetConversationList call.
MAX_PAGE_SIZE = 50

# Matches `nonce='...'` or `nonce="..."` attributes in the page HTML. The
# values are 32-char hex strings. We scope to 16+ chars for safety.
_NONCE_RE = re.compile(r"""nonce=['"]([a-f0-9]{16,})['"]""", re.IGNORECASE)


# --- models ---


class Attachment(BaseModel):
    """A file attached to a message.

    `dcs_id` is Kaiser's opaque document handle. Pass it to
    `download_message_attachment` to fetch the binary.
    """

    name: str | None = None
    file_extension: str | None = None
    dcs_id: str | None = None
    attachment_type: int | None = None
    organization_id: str | None = None


class Author(BaseModel):
    """Sender of a single message."""

    name: str | None = None


class Message(BaseModel):
    """One message within a thread."""

    id: str
    sent_at: str | None = None         # ISO timestamp
    is_unread: bool = False
    author: Author | None = None
    body_text: str | None = None       # HTML-stripped plain text
    attachments: list[Attachment] = Field(default_factory=list)


class MessageThread(BaseModel):
    """Summary of one conversation thread — used in list views."""

    id: str                            # Kaiser's `hthId`
    subject: str | None = None
    preview: str | None = None         # Short server-provided preview
    last_sender: str | None = None     # Display name resolved from user maps
    last_message_at: str | None = None
    is_unread: bool = False
    has_attachments: bool = False
    has_tasks: bool = False
    has_urgent: bool = False
    total_messages: int = 1
    folder_tag: int | None = None      # Which folder this was listed from
    organization_id: str | None = None


class MessageThreadDetail(MessageThread):
    """A full thread: all metadata plus every message's body."""

    can_reply: bool = False
    messages: list[Message] = Field(default_factory=list)


class MessageAttachmentDownload(BaseModel):
    """Outcome of a `download_message_attachment` call.

    Status values:
      - "downloaded" — file is on disk, see `path`.
      - "error"      — transport, IO, or missing-downloadUrl failure;
                       `reason` explains.

    Unlike lab PDFs, message attachments are static files that Kaiser stores
    once and serves directly — there is no `generation_in_progress` state.
    """

    status: str
    path: str | None = None
    filename: str | None = None
    size_bytes: int | None = None
    reason: str | None = None


# --- send-message models ---


class MessageRecipient(BaseModel):
    """One row from `GetMedicalAdviceRequestRecipients`.

    `recipient_id` is OpenKP's stable handle — it's whichever of `userId`,
    `providerId`, or a synthesized fallback uniquely identifies the recipient
    in Kaiser's compose-payload contract. Pass it back to `send_message`.

    The full opaque tuple Kaiser actually wants on Send (`userId`, `poolId`,
    `providerId`, `departmentId`) is preserved as `_raw` so the send path can
    echo it verbatim.
    """

    recipient_id: str
    display_name: str | None = None
    role: str | None = None  # PCP role label or specialty if Kaiser exposes one
    raw: dict[str, Any] = Field(default_factory=dict)


class MessageTopic(BaseModel):
    """One row from `GetSubtopics`. `value` is the integer-as-string Kaiser
    expects on Send (e.g. `"100"` for "Upcoming Appointment or Procedure")."""

    value: str
    title: str | None = None


class MessagePreview(BaseModel):
    """What `send_message(confirm=False)` returns. Read-only reconnaissance.

    `can_confirm` is True only when the recipient + topic both resolved
    cleanly and the message has a non-empty subject and body. `warnings`
    explains every blocker (missing recipient, empty subject, etc.) so the
    caller can fix issues before retrying with `confirm=True`.
    """

    recipient_id: str
    recipient_display_name: str | None = None
    topic_value: str
    topic_title: str | None = None
    subject: str
    body_preview: str  # First ~200 chars, for visual confirmation only
    body_line_count: int
    can_confirm: bool = False
    warnings: list[str] = Field(default_factory=list)


class MessageConfirmation(BaseModel):
    """What `send_message(confirm=True)` returns when the message is sent."""

    conversation_id: str | None = None
    recipient_display_name: str | None = None
    topic_title: str | None = None
    subject: str | None = None
    sent_at: str | None = None  # ISO timestamp captured client-side
    succeeded: bool = False
    dry_run: bool = False


# --- fetchers ---


async def fetch_messages(
    client: KaiserRequest,
    folder: str = "inbox",
    search: str | None = None,
    before_iso: str | None = None,
    limit: int = MAX_PAGE_SIZE,
    deep_search: bool = False,
    max_pages: int = 30,
) -> list[MessageThread]:
    """List message threads in one folder.

    Single-page mode (default): one round trip to GetConversationList. Kaiser
    returns up to 50 threads. For older pages, pass `before_iso` as the cursor.

    Deep-search mode (`deep_search=True`): walk pagination using the
    `oldestSearchedInstantISO` cursor Kaiser returns in `localSummary`. Stops
    when Kaiser reports no more conversations, when the cursor stops
    advancing, or when `max_pages` is hit. Use this when searching for older
    threads — Kaiser's `searchQuery` only matches within the loaded page, so
    a single-page search misses anything older than the most recent ~50
    threads. Results are deduped by thread id.

    Args:
      folder: One of `FOLDER_TAGS` keys. Defaults to "inbox".
      search: Optional search string. Kaiser searches subject, body, sender.
      before_iso: Cursor for pagination. Empty = newest page. In deep-search
        mode, this is the starting cursor (default = newest).
      limit: Max threads to return. Clamped to 50. Ignored in deep-search
        mode (use `max_pages` to bound that walk instead).
      deep_search: If True, walk pagination across the full message history.
      max_pages: Hard cap on pages walked in deep-search mode. Default 30
        (≈ 1500 threads worth of history). Ignored in single-page mode.

    Returns an empty list if the folder is unknown or the response is malformed.
    """
    tag = FOLDER_TAGS.get(folder.lower())
    if tag is None:
        logger.warning("Unknown folder %r; valid: %s", folder, sorted(FOLDER_TAGS))
        return []

    # Same nonce + CSRF reused across every page in a deep walk — Kaiser's UI
    # does the same. Each call would otherwise spend two extra round trips.
    nonce = await _fetch_page_nonce(client)
    csrf = await fetch_csrf_token(client, referer=PAGE_REFERER)

    if not deep_search:
        threads, _ = await _fetch_message_page(
            client, tag=tag, search=search, before_iso=before_iso, csrf=csrf, nonce=nonce,
        )
        clamped = max(1, min(limit, MAX_PAGE_SIZE))
        return threads[:clamped]

    seen_ids: set[str] = set()
    merged: list[MessageThread] = []
    cursor = before_iso or ""
    pages = max(1, max_pages)
    for _ in range(pages):
        threads, summary = await _fetch_message_page(
            client, tag=tag, search=search, before_iso=cursor, csrf=csrf, nonce=nonce,
        )
        for t in threads:
            if t.id and t.id not in seen_ids:
                seen_ids.add(t.id)
                merged.append(t)
        if not summary.get("hasMoreConversations"):
            break
        next_cursor = _str_or_none(summary.get("oldestSearchedInstantISO")) or ""
        # Guard against a malformed response that would loop forever.
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return merged


async def _fetch_message_page(
    client: KaiserRequest,
    *,
    tag: int,
    search: str | None,
    before_iso: str | None,
    csrf: str,
    nonce: str,
) -> tuple[list[MessageThread], dict[str, Any]]:
    """One GetConversationList POST. Returns (threads, localSummary).

    `localSummary` carries the deep-search contract:
      - `hasMoreConversations` (bool) — should we keep paginating?
      - `oldestSearchedInstantISO` — cursor for the next page (advances even
        when the current page returns zero matches).
    """
    payload = {
        "tag": tag,
        "localLoadParams": {
            "loadStartInstantISO": before_iso or "",
            "loadEndInstantISO": "",
            "pagingInfo": 1,
        },
        "externalLoadParams": {},
        "searchQuery": search or "",
        "PageNonce": nonce,
    }
    response = await client.post(LIST_PATH, headers=_api_headers(csrf), json=payload)
    response.raise_for_status()
    body = response.json() if response.content else {}
    threads = _parse_conversation_list(body, folder_tag=tag)
    summary_raw = body.get("localSummary") if isinstance(body, dict) else None
    summary = summary_raw if isinstance(summary_raw, dict) else {}
    return threads, summary


async def fetch_message(client: KaiserRequest, thread_id: str) -> MessageThreadDetail | None:
    """Fetch a full thread by its id (the `id` field from `MessageThread`).

    Returns `None` if the thread can't be found or the response is malformed.
    Raises on HTTP errors.
    """
    if not thread_id:
        return None

    nonce = await _fetch_page_nonce(client)
    csrf = await fetch_csrf_token(client, referer=PAGE_REFERER)
    payload = {
        "id": thread_id,
        "messageId": "",
        "organizationId": "",
        "PageNonce": nonce,
    }
    response = await client.post(DETAILS_PATH, headers=_api_headers(csrf), json=payload)
    response.raise_for_status()
    return _parse_conversation_details(response.json())


async def download_message_attachment(
    client: KaiserRequest,
    dcs_id: str,
    file_extension: str = "PDF",
    display_name: str | None = None,
    organization_id: str = "",
    download_dir: Path | None = None,
) -> MessageAttachmentDownload:
    """Save a message attachment binary to disk.

    Two-hop chain:
      1. POST GetDocumentDetailsLegacy(dcsId) → downloadUrl
      2. GET <downloadUrl> → binary bytes, saved to disk

    Args:
      dcs_id: The `dcs_id` field from a `read_message` attachment.
      file_extension: Kaiser's extension marker (e.g. "PDF", "JPG"). Pass
        through from the attachment metadata.
      display_name: Optional override for the saved filename. If omitted, we
        use Kaiser's `displayName` from GetDocumentDetailsLegacy.
      organization_id: Cross-region attachment marker. Default empty matches
        the same-region case.
      download_dir: Override the default `~/.openkp/downloads/` directory.

    Returns a `MessageAttachmentDownload` with `status='downloaded'` on
    success, or `status='error'` with a short `reason` if anything fails
    short of a raised HTTP error.
    """
    if not dcs_id:
        return MessageAttachmentDownload(status="error", reason="dcs_id is empty")

    csrf = await fetch_csrf_token(client, referer=PAGE_REFERER)

    det_response = await client.post(
        DOCDETAILS_LEGACY_PATH,
        headers=_api_headers(csrf),
        json={
            "dcsId": dcs_id,
            "fileExtension": file_extension,
            "organizationId": organization_id,
            "useOldMobileLink": False,
        },
    )
    det_response.raise_for_status()
    det = det_response.json() if det_response.content else {}
    download_url = _str_or_none(det.get("downloadUrl"))
    if not download_url:
        return MessageAttachmentDownload(
            status="error",
            reason="no downloadUrl in GetDocumentDetailsLegacy response",
        )
    name_for_file = display_name or _str_or_none(det.get("displayName")) or dcs_id

    # Kaiser returns downloadUrl as a relative path that omits the /mychartcn
    # prefix. Match the labs scraper's behavior: prepend it if missing.
    path = download_url if download_url.startswith("/mychartcn") else f"/mychartcn{download_url}"
    bin_response = await client.get(
        path,
        headers={"Accept": "application/pdf,*/*", "Referer": PAGE_REFERER},
    )
    bin_response.raise_for_status()
    body = bin_response.content
    if not body:
        return MessageAttachmentDownload(status="error", reason="empty response body")

    out_dir = download_dir or DEFAULT_DOWNLOAD_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_filename(name_for_file)
    suffix = f".{file_extension.lower().lstrip('.')}" if file_extension else ""
    if suffix and not safe.lower().endswith(suffix):
        safe += suffix
    out_path = out_dir / safe
    out_path.write_bytes(body)

    return MessageAttachmentDownload(
        status="downloaded",
        path=str(out_path),
        filename=safe,
        size_bytes=len(body),
    )


# --- private helpers ---


async def _fetch_page_nonce(client: KaiserRequest) -> str:
    """Fetch the communication-center HTML and extract the CSP nonce.

    Raises `ValueError` if the page doesn't contain a `nonce=...` attribute
    we recognize. In practice this would mean the page layout changed.
    """
    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "X-Requested-With": "XMLHttpRequest",
    }
    response = await client.get(PAGE_PATH, headers=headers)
    response.raise_for_status()
    match = _NONCE_RE.search(response.text)
    if not match:
        raise ValueError("Page nonce not found in communication-center HTML")
    return match.group(1)


def _api_headers(csrf_token: str) -> dict[str, str]:
    """Shared headers for the /api/conversations/* POST calls.

    Kaiser's ASP.NET anti-forgery middleware 500s the request (caught by the
    /mychartcn/Home/FiveHundred error page redirect) without the token.
    """
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://healthy.kaiserpermanente.org",
        "Referer": PAGE_REFERER,
        "X-Requested-With": "XMLHttpRequest",
        "__RequestVerificationToken": csrf_token,
    }


def _parse_conversation_list(payload: Any, *, folder_tag: int | None) -> list[MessageThread]:
    """Walk a GetConversationList response, produce `MessageThread` summaries."""
    if not isinstance(payload, dict):
        return []
    convs = payload.get("conversations")
    if not isinstance(convs, list):
        return []

    users_raw = payload.get("users")
    users: dict[str, Any] = users_raw if isinstance(users_raw, dict) else {}
    viewers_raw = payload.get("viewers")
    viewers: dict[str, Any] = viewers_raw if isinstance(viewers_raw, dict) else {}

    out: list[MessageThread] = []
    for conv in convs:
        if not isinstance(conv, dict):
            continue
        thread = _parse_thread_summary(
            conv,
            users=users,
            viewers=viewers,
            folder_tag=folder_tag,
        )
        if thread is not None:
            out.append(thread)
    return out


def _parse_conversation_details(payload: Any) -> MessageThreadDetail | None:
    """Walk a GetConversationDetails response, produce a full thread."""
    if not isinstance(payload, dict):
        return None
    thread_id = _str_or_none(payload.get("hthId"))
    if thread_id is None:
        return None

    users_raw = payload.get("users")
    users: dict[str, Any] = users_raw if isinstance(users_raw, dict) else {}
    viewers_raw = payload.get("viewers")
    viewers: dict[str, Any] = viewers_raw if isinstance(viewers_raw, dict) else {}
    overrides_raw = payload.get("userOverrideNames")
    overrides: dict[str, Any] = overrides_raw if isinstance(overrides_raw, dict) else {}

    summary = _parse_thread_summary(payload, users=users, viewers=viewers, folder_tag=None)
    if summary is None:
        return None

    can_reply = False
    reply_flags = payload.get("replyFlags")
    if isinstance(reply_flags, dict):
        can_reply = bool(reply_flags.get("canReply"))

    raw_messages = payload.get("messages")
    messages_raw: list[Any] = raw_messages if isinstance(raw_messages, list) else []
    messages = [
        m for m in (_parse_message(raw, users=users, viewers=viewers, overrides=overrides) for raw in messages_raw)
        if m is not None
    ]

    total = payload.get("totalMessages")
    total_int = total if isinstance(total, int) else len(messages) or 1

    return MessageThreadDetail(
        **summary.model_dump(),
        can_reply=can_reply,
        messages=messages,
    ).model_copy(update={"total_messages": total_int})


def _parse_thread_summary(
    conv: dict[str, Any],
    *,
    users: dict[str, Any],
    viewers: dict[str, Any],
    folder_tag: int | None,
) -> MessageThread | None:
    """Pull a `MessageThread` summary out of one conversation dict."""
    thread_id = _str_or_none(conv.get("hthId"))
    if thread_id is None:
        return None

    overrides_raw = conv.get("userOverrideNames")
    overrides: dict[str, Any] = overrides_raw if isinstance(overrides_raw, dict) else {}

    tags = conv.get("tags")
    tag_set = set(tags.keys()) if isinstance(tags, dict) else set()

    # Last message timestamp + last sender come from the most recent message
    # Kaiser inlines into the conversation summary.
    last_sender = None
    last_message_at = None
    messages_raw = conv.get("messages")
    if isinstance(messages_raw, list) and messages_raw:
        latest = messages_raw[0] if isinstance(messages_raw[0], dict) else {}
        last_message_at = _str_or_none(latest.get("deliveryInstantISO"))
        author_raw = latest.get("author")
        author: dict[str, Any] = author_raw if isinstance(author_raw, dict) else {}
        # Authors in the inline message carry a direct displayName, which is
        # the most reliable source. Fall back to user-key resolution.
        last_sender = _str_or_none(author.get("displayName"))
        if last_sender is None:
            user_keys = conv.get("userKeys")
            if isinstance(user_keys, list) and user_keys:
                last_sender = _resolve_display_name(
                    _str_or_none(user_keys[0]),
                    users=users,
                    viewers=viewers,
                    overrides=overrides,
                )

    total_messages = 1
    if isinstance(messages_raw, list):
        total_messages = max(len(messages_raw), 1)

    return MessageThread(
        id=thread_id,
        subject=_str_or_none(conv.get("subject")),
        preview=_str_or_none(conv.get("previewText")),
        last_sender=last_sender,
        last_message_at=last_message_at,
        is_unread="Unread" in tag_set,
        has_attachments=bool(conv.get("hasAttachments")),
        has_tasks=bool(conv.get("hasTasks")),
        has_urgent=bool(conv.get("hasUrgentMsgs")),
        total_messages=total_messages,
        folder_tag=folder_tag,
        organization_id=_str_or_none(conv.get("organizationId")),
    )


def _parse_message(
    raw: Any,
    *,
    users: dict[str, Any],
    viewers: dict[str, Any],
    overrides: dict[str, Any],
) -> Message | None:
    """One message dict → `Message` model (with body HTML stripped)."""
    if not isinstance(raw, dict):
        return None
    msg_id = _str_or_none(raw.get("wmgId"))
    if msg_id is None:
        return None

    author_name = None
    author_raw = raw.get("author")
    if isinstance(author_raw, dict):
        author_name = _str_or_none(author_raw.get("displayName"))
        if author_name is None:
            author_name = _resolve_display_name(
                _str_or_none(author_raw.get("empKey")),
                users=users,
                viewers=viewers,
                overrides=overrides,
            )

    return Message(
        id=msg_id,
        sent_at=_str_or_none(raw.get("deliveryInstantISO")),
        is_unread=bool(raw.get("isUnread")),
        author=Author(name=author_name) if author_name else None,
        body_text=_html_to_text(raw.get("body")),
        attachments=_parse_attachments(raw.get("attachments")),
    )


def _parse_attachments(raw: Any) -> list[Attachment]:
    if not isinstance(raw, list):
        return []
    out: list[Attachment] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = _str_or_none(item.get("name"))
        ext = _str_or_none(item.get("fileExtension"))
        dcs_id = _str_or_none(item.get("dcsId"))
        if not name and not ext and not dcs_id:
            continue
        att_type = item.get("type")
        out.append(
            Attachment(
                name=name,
                file_extension=ext,
                dcs_id=dcs_id,
                attachment_type=att_type if isinstance(att_type, int) else None,
                organization_id=_str_or_none(item.get("organizationId")),
            )
        )
    return out


def _resolve_display_name(
    key: str | None,
    *,
    users: dict[str, Any],
    viewers: dict[str, Any],
    overrides: dict[str, Any],
) -> str | None:
    """Map an obfuscated Kaiser user key to a display name.

    Priority order:
      1. `userOverrideNames[key]` — already a plain string.
      2. `users[key].name` — provider/staff side.
      3. `viewers[key].name` — patient/viewer side.
    """
    if not key:
        return None
    if isinstance(overrides, dict):
        override = overrides.get(key)
        if isinstance(override, str):
            name = _str_or_none(override)
            if name:
                return name
    for pool in (users, viewers):
        if not isinstance(pool, dict):
            continue
        entry = pool.get(key)
        if isinstance(entry, dict):
            name = _str_or_none(entry.get("name")) or _str_or_none(entry.get("displayName"))
            if name:
                return name
    return None


_BLOCK_TAGS = ("p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "blockquote", "pre")
_MULTISPACE_RE = re.compile(r" +")


def _html_to_text(html: Any) -> str | None:
    """Strip HTML to plain text, preserving paragraph and line breaks.

    Strategy:
      1. Insert a blank line before each block-level tag so paragraphs stay
         visually separated in the output.
      2. Convert `<br>` to a newline.
      3. Use `get_text(separator=" ")` so inline text nodes stay cohesive
         within a paragraph.
      4. Collapse runs of spaces and blank lines for readability.
    """
    if not isinstance(html, str) or not html.strip():
        return None
    soup = BeautifulSoup(html, "lxml")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(_BLOCK_TAGS):
        block.insert_before("\n\n")
    text = soup.get_text(separator=" ", strip=False)

    # Per-line: collapse runs of spaces, strip.
    lines = [_MULTISPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
    # Collapse runs of blank lines down to a single blank.
    out: list[str] = []
    prev_blank = False
    for line in lines:
        if line:
            out.append(line)
            prev_blank = False
        elif not prev_blank:
            out.append("")
            prev_blank = True
    return "\n".join(out).strip() or None


def _safe_filename(name: str) -> str:
    """Produce a filesystem-safe name for a downloaded attachment.

    Replaces path separators, control chars, and Windows-reserved chars with
    underscores. Trims whitespace. Caps length at 180 to stay well under
    common filesystem limits when combined with the download directory path.
    """
    cleaned = _UNSAFE_FILENAME_RE.sub("_", name).strip()
    if len(cleaned) > 180:
        cleaned = cleaned[:180].rstrip()
    return cleaned or "attachment"


def _str_or_none(value: Any) -> str | None:
    """Coerce to stripped string, or None if empty / missing."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


# --- send-message: public ---


async def list_message_recipients(client: KaiserRequest) -> list[MessageRecipient]:
    """List the providers and pools the patient may message.

    Calls `GetMedicalAdviceRequestRecipients`. Mirrors the recipient picker
    in the MyChart UI ("Whose office do you want to contact?"). Returns the
    full list — order is whatever Kaiser sends, which empirically puts the
    PCP first followed by specialists with prior visits, alphabetical.

    Use the `recipient_id` from a returned row when calling `send_message`.
    Tolerant of unknown response shapes — see messages.md "Open response-shape
    questions".
    """
    csrf = await fetch_csrf_token(client, referer=PAGE_REFERER)
    response = await client.post(
        RECIPIENTS_PATH,
        headers=_api_headers(csrf),
        json={"organizationId": ""},
    )
    response.raise_for_status()
    payload = response.json() if response.content else {}
    recipients = _parse_recipients(payload)
    if not recipients:
        _debug_dump_payload("recipients", payload)
    return recipients


async def list_message_topics(client: KaiserRequest) -> list[MessageTopic]:
    """List the valid `topic_value` codes for `send_message`.

    Calls `GetSubtopics`. The MyChart UI labels these as "Reason for Message".

    Verified catalog (live, 2026-05-03):
      - value="97",  title="Test Results"
      - value="98",  title="Medication"
      - value="99",  title="Visit Follow-Up"
      - value="100", title="Upcoming Appointment or Procedure"
      - value="101", title="Non-Urgent Medical Advice"
    """
    csrf = await fetch_csrf_token(client, referer=PAGE_REFERER)
    response = await client.post(
        SUBTOPICS_PATH,
        headers=_api_headers(csrf),
        json={"organizationId": ""},
    )
    response.raise_for_status()
    payload = response.json() if response.content else {}
    topics = _parse_topics(payload)
    if not topics:
        _debug_dump_payload("topics", payload)
    return topics


async def send_message(
    client: KaiserRequest,
    *,
    recipient_id: str,
    topic_value: str,
    subject: str,
    body: str,
    confirm: bool = False,
    data_dir: Path | None = None,
) -> MessagePreview | MessageConfirmation:
    """Send a non-urgent message to a Kaiser provider. Two-call confirm pattern.

    Args:
      client: Authenticated KaiserRequest.
      recipient_id: The `recipient_id` from a `list_message_recipients` row.
      topic_value: One of the `value` strings from `list_message_topics`,
        e.g. "100" for "Upcoming Appointment or Procedure" or "101" for
        "Non-Urgent Medical Advice".
      subject: Message subject. Required, non-empty after strip.
      body: Message body. Required, non-empty after strip. Newlines are
        preserved; Kaiser stores per-line as an array of strings.
      confirm: When False (default), build and return a `MessagePreview`
        without sending. When True, run the full GetComposeId → SaveDraft →
        Send chain and return a `MessageConfirmation`.
      data_dir: OpenKP data directory for the audit log. Required when
        `confirm=True`. Ignored on preview.

    Confirm pattern:
      Call once with `confirm=False` to preview. Read the preview. If
      `can_confirm` is True and you want to proceed, call again with
      `confirm=True`.

    Safety:
      - Every commit-path call writes to `<data_dir>/audit.log` before and
        after each Kaiser request.
      - `OPENKP_DRY_RUN=1` short-circuits the actual SendMedicalAdviceRequest
        POST and returns a synthetic confirmation with `dry_run=True`. The
        prep calls (GetComposeId, SaveDraft) still run because they're not
        observable side effects to the recipient.
      - Body and subject text are kept out of the audit log.

    v1 limits:
      - No attachments (the `documentIds` array is always empty).
      - No reply-to-existing-thread support (always starts a new conversation).
    """
    if not isinstance(recipient_id, str) or not recipient_id.strip():
        raise ValueError("recipient_id must be a non-empty string")
    if not isinstance(topic_value, str) or not topic_value.strip():
        raise ValueError("topic_value must be a non-empty string")
    if not isinstance(subject, str):
        raise ValueError("subject must be a string")
    if not isinstance(body, str):
        raise ValueError("body must be a string")

    recipient_id = recipient_id.strip()
    topic_value = topic_value.strip()
    subject_clean = subject.strip()
    body_lines = body.splitlines() if body else [""]

    csrf = await fetch_csrf_token(client, referer=PAGE_REFERER)

    recipients = await list_message_recipients(client)
    recipient = _find_recipient(recipients, recipient_id)

    topics = await list_message_topics(client)
    topic = _find_topic(topics, topic_value)

    preview = _build_message_preview(
        recipient=recipient,
        recipient_id_input=recipient_id,
        topic=topic,
        topic_value_input=topic_value,
        subject=subject_clean,
        body_lines=body_lines,
    )

    if not confirm:
        return preview

    if not preview.can_confirm:
        raise ValueError(
            "Cannot send message: preview reports it is not committable. "
            f"Reasons: {', '.join(preview.warnings) or 'unknown'}."
        )
    if data_dir is None:
        raise ValueError("data_dir is required when confirm=True")
    if recipient is None or topic is None:
        # Unreachable when can_confirm=True. Defensive.
        raise RuntimeError("Internal: confirmable preview but missing recipient or topic")

    return await _commit_send_message(
        client=client,
        csrf=csrf,
        recipient=recipient,
        topic=topic,
        subject=subject_clean,
        body_lines=body_lines,
        data_dir=data_dir,
    )


# --- send-message: parsers ---


def _parse_recipients(payload: Any) -> list[MessageRecipient]:
    """Walk the GetMedicalAdviceRequestRecipients response.

    Response envelope is unknown — tries the most likely top-level keys, then
    falls back to a recursive scan for any list whose first element matches
    the `recipient` shape (has both displayName and userId/providerId).
    Logs the envelope shape on first call so we can pin it down.
    """
    rows = _coerce_recipient_list(payload)
    if not rows:
        logger.warning(
            "GetMedicalAdviceRequestRecipients: no recipient list found "
            "(top-level keys=%s). Response shape may have changed; see messages.md.",
            list(payload.keys())[:10] if isinstance(payload, dict) else type(payload).__name__,
        )
        return []

    out: list[MessageRecipient] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        recipient = _parse_recipient_row(row)
        if recipient is not None:
            out.append(recipient)
    return out


def _coerce_recipient_list(payload: Any) -> list[Any]:
    """Find the list of recipient dicts inside an unknown envelope."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    # Likely top-level keys (any one of these may be the wrapper).
    for key in ("recipients", "results", "items", "data", "list", "providers"):
        candidate = payload.get(key)
        if isinstance(candidate, list):
            return candidate
    # Fallback: scan for a list whose first dict has displayName + (userId or providerId).
    for value in payload.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            first = value[0]
            if "displayName" in first and ("userId" in first or "providerId" in first):
                return value
    return []


def _parse_recipient_row(raw: dict[str, Any]) -> MessageRecipient | None:
    """Build a MessageRecipient from one row, picking a stable id."""
    user_id = _str_or_none(raw.get("userId"))
    provider_id = _str_or_none(raw.get("providerId"))
    pool_id = _str_or_none(raw.get("poolId"))
    display = _str_or_none(raw.get("displayName"))

    # Prefer userId — it's the field the verified send-flow recipient used.
    # Fall back to providerId, then poolId. Without one of these the row is
    # not actionable.
    recipient_id = user_id or provider_id or pool_id
    if not recipient_id:
        return None

    role = _str_or_none(raw.get("role")) or _str_or_none(raw.get("specialty"))

    # Keep the full row for the send path to echo verbatim. Even if we don't
    # know every field today, Kaiser may add/change them, and the contract
    # is that the full recipient object goes back unchanged.
    return MessageRecipient(
        recipient_id=recipient_id,
        display_name=display,
        role=role,
        raw=raw,
    )


def _parse_topics(payload: Any) -> list[MessageTopic]:
    """Walk a GetSubtopics response. Tolerates several envelope shapes.

    Verified shape (live, 2026-05-03):

      ```
      {"topicList": [{"value":"97", "displayName":"Test Results"}, ...],
       "organizationId": ""}
      ```

    Accepts `displayName` as the title field with `title` / `name` / `label`
    as fallbacks for forward compatibility.
    """
    rows = _coerce_topic_list(payload)
    if not rows:
        logger.warning(
            "GetSubtopics: no topic list found (top-level keys=%s). "
            "Response shape may have changed; see messages.md.",
            list(payload.keys())[:10] if isinstance(payload, dict) else type(payload).__name__,
        )
        return []

    out: list[MessageTopic] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = _str_or_none(row.get("value"))
        if value is None:
            continue
        title = (
            _str_or_none(row.get("displayName"))
            or _str_or_none(row.get("title"))
            or _str_or_none(row.get("name"))
            or _str_or_none(row.get("label"))
        )
        out.append(MessageTopic(value=value, title=title))
    return out


# Title-shaped fields we'll accept on a topic row. `displayName` is the live
# Kaiser shape; the others are forward-compat for adjacent endpoints.
_TOPIC_TITLE_KEYS = ("title", "displayName", "name", "label")


def _coerce_topic_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    # Try common wrapper keys, case-insensitively. `topicList` is the verified
    # live shape; the others cover plausible variants we haven't seen.
    wrapper_keys = ("topicList", "topics", "subtopics", "subTopics",
                    "results", "items", "data", "list")
    lowered = {k.lower(): k for k in payload.keys() if isinstance(k, str)}
    for key in wrapper_keys:
        actual = payload.get(key)
        if isinstance(actual, list):
            return actual
        # Case-insensitive retry.
        actual2 = payload.get(lowered.get(key.lower(), ""))
        if isinstance(actual2, list):
            return actual2
    # Recursive fallback: any list whose first dict has `value` plus a
    # title-shaped field. Accepts displayName / title / name / label.
    for value in payload.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            first = value[0]
            if "value" in first and any(k in first for k in _TOPIC_TITLE_KEYS):
                return value
    return []


def _find_recipient(
    recipients: list[MessageRecipient], wanted_id: str
) -> MessageRecipient | None:
    """Match a user-supplied recipient_id against the catalog.

    Matches against the resolved `recipient_id` first (what we returned from
    `list_message_recipients`), then falls back to scanning the raw userId /
    providerId / poolId fields so callers passing a Kaiser-side ID directly
    still resolve.
    """
    for r in recipients:
        if r.recipient_id == wanted_id:
            return r
    for r in recipients:
        raw = r.raw
        if any(_str_or_none(raw.get(k)) == wanted_id for k in ("userId", "providerId", "poolId")):
            return r
    return None


def _find_topic(topics: list[MessageTopic], wanted_value: str) -> MessageTopic | None:
    for t in topics:
        if t.value == wanted_value:
            return t
    return None


def _build_message_preview(
    *,
    recipient: MessageRecipient | None,
    recipient_id_input: str,
    topic: MessageTopic | None,
    topic_value_input: str,
    subject: str,
    body_lines: list[str],
) -> MessagePreview:
    warnings: list[str] = []

    if recipient is None:
        warnings.append(
            f"recipient_id {recipient_id_input!r} not found — call list_message_recipients to discover valid IDs"
        )
    if topic is None:
        warnings.append(
            f"topic_value {topic_value_input!r} not found — call list_message_topics to discover valid values"
        )
    if not subject:
        warnings.append("subject is empty — Kaiser requires a non-empty subject")
    body_joined = "".join(body_lines).strip()
    if not body_joined:
        warnings.append("body is empty — Kaiser requires a non-empty message")

    return MessagePreview(
        recipient_id=recipient.recipient_id if recipient else recipient_id_input,
        recipient_display_name=recipient.display_name if recipient else None,
        topic_value=topic.value if topic else topic_value_input,
        topic_title=topic.title if topic else None,
        subject=subject,
        body_preview=("\n".join(body_lines))[:200],
        body_line_count=len(body_lines),
        can_confirm=not warnings,
        warnings=warnings,
    )


# --- send-message: commit ---


async def _commit_send_message(
    *,
    client: KaiserRequest,
    csrf: str,
    recipient: MessageRecipient,
    topic: MessageTopic,
    subject: str,
    body_lines: list[str],
    data_dir: Path,
) -> MessageConfirmation:
    """Run GetComposeId → SaveDraft → Send (or short-circuit under dry-run).

    Sequence:
      1. POST GetComposeId           → composeId
      2. POST SaveMedicalAdviceRequestDraft (conversationId="")  → conversationId
      3. POST SendMedicalAdviceRequest (final payload)            → 200 = sent
      4. POST RemoveComposeId        — best-effort cleanup (errors ignored)

    Each step audits before and after via `audit_log_event`. Subject and body
    are NOT logged — only metadata (recipient display name, topic title,
    body line count). Under dry-run, steps 1 and 2 still run (idempotent
    prep) but step 3 is skipped and we synthesize a success.
    """
    audit_fields = {
        "recipient_display_name": recipient.display_name,
        "topic_value": topic.value,
        "topic_title": topic.title,
        "subject_length": len(subject),
        "body_line_count": len(body_lines),
        "body_total_chars": sum(len(line) for line in body_lines),
    }
    audit_log_event(data_dir, tool="send_message", phase="intent", fields=audit_fields)

    try:
        compose_id = await _post_get_compose_id(client, csrf)
        conversation_id = await _post_save_draft(
            client=client,
            csrf=csrf,
            compose_id=compose_id,
            conversation_id="",
            recipient=recipient,
            topic=topic,
            subject=subject,
            body_lines=body_lines,
        )

        if is_dry_run():
            sent_at = datetime.now(timezone.utc).isoformat()
            confirmation = MessageConfirmation(
                conversation_id=conversation_id,
                recipient_display_name=recipient.display_name,
                topic_title=topic.title,
                subject=subject,
                sent_at=sent_at,
                succeeded=True,
                dry_run=True,
            )
            audit_log_event(
                data_dir,
                tool="send_message",
                phase="result",
                fields={**audit_fields, "conversation_id": conversation_id, "succeeded": True, "dry_run": True},
            )
            # Best-effort cleanup even in dry-run, so KP doesn't accumulate orphan composeIds.
            await _post_remove_compose_id_safely(client, csrf, compose_id)
            return confirmation

        await _post_send_message(
            client=client,
            csrf=csrf,
            compose_id=compose_id,
            conversation_id=conversation_id,
            recipient=recipient,
            topic=topic,
            subject=subject,
            body_lines=body_lines,
        )
    except Exception as exc:
        audit_log_event(
            data_dir,
            tool="send_message",
            phase="error",
            fields={**audit_fields, "error_type": type(exc).__name__, "error_msg": str(exc)[:200]},
        )
        # Best-effort cleanup if we already minted a composeId.
        try:
            await _post_remove_compose_id_safely(client, csrf, locals().get("compose_id"))
        except Exception:
            pass
        raise

    sent_at = datetime.now(timezone.utc).isoformat()
    confirmation = MessageConfirmation(
        conversation_id=conversation_id,
        recipient_display_name=recipient.display_name,
        topic_title=topic.title,
        subject=subject,
        sent_at=sent_at,
        succeeded=True,
        dry_run=False,
    )
    audit_log_event(
        data_dir,
        tool="send_message",
        phase="result",
        fields={**audit_fields, "conversation_id": conversation_id, "succeeded": True},
    )
    await _post_remove_compose_id_safely(client, csrf, compose_id)
    return confirmation


async def _post_get_compose_id(client: KaiserRequest, csrf: str) -> str:
    """POST GetComposeId. Returns the composeId token."""
    response = await client.post(COMPOSE_ID_PATH, headers=_api_headers(csrf), json={})
    response.raise_for_status()
    payload = response.json() if response.content else {}
    compose_id = _extract_first_string_value(payload, ("composeId", "ComposeId", "id"))
    if not compose_id:
        raise RuntimeError(
            "GetComposeId response missing composeId (response keys: "
            f"{list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__})"
        )
    return compose_id


async def _post_save_draft(
    *,
    client: KaiserRequest,
    csrf: str,
    compose_id: str,
    conversation_id: str,
    recipient: MessageRecipient,
    topic: MessageTopic,
    subject: str,
    body_lines: list[str],
) -> str:
    """POST SaveMedicalAdviceRequestDraft. Returns the conversationId.

    The first SaveDraft (with conversationId="") is what mints the
    conversationId server-side. Send then echoes it back.
    """
    body = _build_compose_payload(
        compose_id=compose_id,
        conversation_id=conversation_id,
        recipient=recipient,
        topic=topic,
        subject=subject,
        body_lines=body_lines,
    )
    response = await client.post(DRAFT_SAVE_PATH, headers=_api_headers(csrf), json=body)
    response.raise_for_status()
    payload = response.json() if response.content else {}
    conv_id = _extract_first_string_value(payload, ("conversationId", "ConversationId", "id"))
    if not conv_id:
        raise RuntimeError(
            "SaveMedicalAdviceRequestDraft response missing conversationId "
            f"(response keys: {list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__})"
        )
    return conv_id


async def _post_send_message(
    *,
    client: KaiserRequest,
    csrf: str,
    compose_id: str,
    conversation_id: str,
    recipient: MessageRecipient,
    topic: MessageTopic,
    subject: str,
    body_lines: list[str],
) -> None:
    """POST SendMedicalAdviceRequest. We rely on HTTP 200 = success.

    Response body shape (~86 bytes) is unknown — see messages.md "Open
    response-shape questions". Not load-bearing for correctness.
    """
    body = _build_compose_payload(
        compose_id=compose_id,
        conversation_id=conversation_id,
        recipient=recipient,
        topic=topic,
        subject=subject,
        body_lines=body_lines,
    )
    response = await client.post(SEND_PATH, headers=_api_headers(csrf), json=body)
    response.raise_for_status()


async def _post_remove_compose_id_safely(
    client: KaiserRequest,
    csrf: str,
    compose_id: str | None,
) -> None:
    """Cleanup helper. Logged-not-raised — failure here doesn't undo the send."""
    if not compose_id:
        return
    try:
        response = await client.post(
            COMPOSE_REMOVE_PATH,
            headers=_api_headers(csrf),
            json={"composeId": compose_id},
        )
        response.raise_for_status()
    except Exception as exc:
        logger.warning("RemoveComposeId failed (%s); composeId may leak server-side", type(exc).__name__)


def _build_compose_payload(
    *,
    compose_id: str,
    conversation_id: str,
    recipient: MessageRecipient,
    topic: MessageTopic,
    subject: str,
    body_lines: list[str],
) -> dict[str, Any]:
    """Build the SaveDraft / Send body shape exactly as the MyChart UI does.

    Recipient and topic objects must echo Kaiser's full key set verbatim — we
    use whatever fields appeared in `raw` plus enforce defaults for the four
    keys we know are always required (`displayName`, `userId`, `poolId`,
    `providerId`, `departmentId`). The viewers array is the patient's self
    viewer derived from the recipient row when available; v1 doesn't surface
    proxy access.
    """
    recipient_obj = _build_recipient_payload(recipient)
    return {
        "recipient": recipient_obj,
        "topic": {"title": topic.title or "", "value": topic.value},
        "conversationId": conversation_id,
        "organizationId": "",
        "viewers": _build_viewers_payload(recipient),
        "messageBody": list(body_lines),
        "messageSubject": subject,
        "documentIds": [],
        "includeOtherViewers": False,
        "composeId": compose_id,
    }


def _build_recipient_payload(recipient: MessageRecipient) -> dict[str, Any]:
    """Echo the recipient row in the shape Kaiser expects on Send.

    Pulls from `raw` first so any Kaiser-side fields we don't model (added
    later, region-specific, etc.) survive the round trip. Backfills the five
    canonical keys with empty strings if missing.
    """
    raw = dict(recipient.raw) if isinstance(recipient.raw, dict) else {}
    raw.setdefault("displayName", recipient.display_name or "")
    for key in ("userId", "poolId", "providerId", "departmentId"):
        raw.setdefault(key, "")
    return raw


def _build_viewers_payload(recipient: MessageRecipient) -> list[dict[str, str]]:
    """Build the `viewers` array — currently always one self-viewer.

    The wprId comes from the recipient row's `wprId` if present (some Kaiser
    payloads embed it), otherwise from `viewerId` or empty as a last resort.
    A live `GetViewers` call would be the canonical source; for v1 we trust
    whatever Kaiser wrote into the recipient row, falling back to empty —
    Kaiser's server-side validation will reject a clearly-bad viewers array.
    """
    raw = recipient.raw if isinstance(recipient.raw, dict) else {}
    wpr_id = (
        _str_or_none(raw.get("wprId"))
        or _str_or_none(raw.get("viewerId"))
        or _str_or_none(raw.get("selfWprId"))
        or ""
    )
    return [{"wprId": wpr_id}]


DEBUG_DUMPS_ENV = "OPENKP_DEBUG_DUMPS"


def _debug_dumps_enabled() -> bool:
    """True when OPENKP_DEBUG_DUMPS is set to a truthy value.

    Truthy: "1", "true", "yes", "on" (case-insensitive). Anything else,
    including unset, is False.
    """
    return os.getenv(DEBUG_DUMPS_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _debug_dump_payload(kind: str, payload: Any) -> None:
    """Diagnostic: dump an unrecognized response to ~/.openkp/, opt-in only.

    When `_parse_topics` or `_parse_recipients` returns empty, the response
    shape didn't match anything we expected. The recipients payload names the
    user's care team — PHI-adjacent — so dumping is gated behind
    OPENKP_DEBUG_DUMPS. Without that env var set, this just logs a one-line
    warning so the operator knows the parser missed something.

    Best-effort: swallows IO errors silently — diagnostics must never break
    the tool.

    Output path (when enabled): ~/.openkp/debug-<kind>-<utc-timestamp>.json
    """
    if not _debug_dumps_enabled():
        logger.warning(
            "Parser returned empty for %s; set %s=1 to capture the raw payload",
            kind, DEBUG_DUMPS_ENV,
        )
        return
    try:
        import json as _json
        target = Path.home() / ".openkp" / f"debug-{kind}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as fh:
            _json.dump(payload, fh, indent=2, default=str)
        logger.warning("Wrote unrecognized %s response to %s", kind, target)
    except Exception as exc:
        logger.warning("Failed to dump %s payload (%s)", kind, type(exc).__name__)


def _extract_first_string_value(payload: Any, keys: tuple[str, ...]) -> str | None:
    """Pull the first non-empty string value matching any of `keys`.

    Looks at the top level first, then recurses one level into nested dicts.
    Used to be tolerant of `{composeId: "..."}` vs `{data: {composeId: "..."}}`
    envelope variations we haven't yet confirmed.
    """
    if not isinstance(payload, dict):
        return None
    for k in keys:
        v = _str_or_none(payload.get(k))
        if v is not None:
            return v
    for nested in payload.values():
        if isinstance(nested, dict):
            for k in keys:
                v = _str_or_none(nested.get(k))
                if v is not None:
                    return v
    return None
