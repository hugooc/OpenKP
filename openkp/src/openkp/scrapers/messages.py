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
import re
from typing import Any

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from openkp.scrapers.csrf import fetch_csrf_token
from openkp.scrapers.request import KaiserRequest

logger = logging.getLogger(__name__)

# Page that hosts the CSP nonce we need to unlock the JSON APIs.
PAGE_PATH = "/mychartcn/app/communication-center"
LIST_PATH = "/mychartcn/api/conversations/GetConversationList"
DETAILS_PATH = "/mychartcn/api/conversations/GetConversationDetails"
PAGE_REFERER = "https://healthy.kaiserpermanente.org/mychartcn/app/communication-center"

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
    """A file attached to a message. We don't download them (that's a
    separate Phase 3 concern), just surface enough metadata for display."""

    name: str | None = None
    file_extension: str | None = None


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


# --- fetchers ---


async def fetch_messages(
    client: KaiserRequest,
    folder: str = "inbox",
    search: str | None = None,
    before_iso: str | None = None,
    limit: int = MAX_PAGE_SIZE,
) -> list[MessageThread]:
    """List message threads in one folder.

    One round trip to GetConversationList, plus one upfront to fetch the
    page nonce. Kaiser returns up to 50 threads per page. `limit` caps at 50.
    For older pages, pass `before_iso` as the timestamp of the oldest thread
    from the previous result.

    Args:
      folder: One of `FOLDER_TAGS` keys. Defaults to "inbox".
      search: Optional search string. Kaiser searches subject, body, sender.
      before_iso: Cursor for pagination. Empty = newest page.
      limit: Max threads to return. Clamped to 50.

    Returns an empty list if the folder is unknown or the response is malformed.
    """
    tag = FOLDER_TAGS.get(folder.lower())
    if tag is None:
        logger.warning("Unknown folder %r; valid: %s", folder, sorted(FOLDER_TAGS))
        return []

    nonce = await _fetch_page_nonce(client)
    csrf = await fetch_csrf_token(client, referer=PAGE_REFERER)
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

    threads = _parse_conversation_list(response.json(), folder_tag=tag)
    clamped = max(1, min(limit, MAX_PAGE_SIZE))
    return threads[:clamped]


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

    users = payload.get("users") if isinstance(payload.get("users"), dict) else {}
    viewers = payload.get("viewers") if isinstance(payload.get("viewers"), dict) else {}

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

    users = payload.get("users") if isinstance(payload.get("users"), dict) else {}
    viewers = payload.get("viewers") if isinstance(payload.get("viewers"), dict) else {}
    overrides = payload.get("userOverrideNames") if isinstance(payload.get("userOverrideNames"), dict) else {}

    summary = _parse_thread_summary(payload, users=users, viewers=viewers, folder_tag=None)
    if summary is None:
        return None

    can_reply = False
    reply_flags = payload.get("replyFlags")
    if isinstance(reply_flags, dict):
        can_reply = bool(reply_flags.get("canReply"))

    messages_raw = payload.get("messages") if isinstance(payload.get("messages"), list) else []
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

    overrides = conv.get("userOverrideNames") if isinstance(conv.get("userOverrideNames"), dict) else {}

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
        author = latest.get("author") if isinstance(latest.get("author"), dict) else {}
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
        if not name and not ext:
            continue
        out.append(Attachment(name=name, file_extension=ext))
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


def _str_or_none(value: Any) -> str | None:
    """Coerce to stripped string, or None if empty / missing."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None
