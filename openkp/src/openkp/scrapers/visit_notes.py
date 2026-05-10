"""Visit notes + after-visit summary scraper.

Two MCP tools surface from this module:

- `read_visit_notes(csn)` — clinical notes (provider chart notes, progress
  notes, op notes, etc.) plus the rendered After Visit Summary content,
  for one past visit. Uses Kaiser's `LoadReportContent` endpoint which
  returns Epic-rendered HTML; we strip to plain text for LLM consumers
  and also expose the raw HTML for callers that want it.
- `download_visit_avs_pdf(csn)` — save the canonical AVS PDF to disk,
  same pattern as `download_lab_result_pdf` and `download_message_attachment`.

Server-side flow for the notes tool (one round trip per note):

  1. CSRF token (past-details referer)
  2. GetVisitDetailsPast(csn) → visit metadata + AVS dcsId (if any)
  3. GetVisitNotes(CSN) → noteList[] + top-level lrpID
  4. CSRF token (note referer, scoped per page) — once, reused per note
  5. for each note: ValidateVisitNote(csn, hnoID, hnoDAT, lrpID)
                    LoadReportContent(reportID=lrpID, contextID=hnoID,
                                      contextDAT=hnoDAT, contextINI="HNO", csn)
  6. LoadReportContent(reportMnemonic="AMB_AVS", csn) — the rendered AVS

The AVS PDF download is a separate two-hop chain inside its own tool:

  GetVisitDetailsPast(csn) → avsInfo.avsSnapshots[0].dcsID
  GetDocumentDetails(dcsId) → downloadUrl
  GET <downloadUrl> → binary PDF

Docs: `docs/research/endpoints/visit_notes.md`
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from openkp.scrapers.csrf import fetch_csrf_token
from openkp.scrapers.request import KaiserRequest

logger = logging.getLogger(__name__)

# Endpoints
VISIT_DETAILS_PATH = "/mychartcn/api/visits/past-details/GetVisitDetailsPast"
GET_NOTES_PATH = "/mychartcn/api/visit-notes/GetVisitNotes"
VALIDATE_NOTE_PATH = "/mychartcn/api/visit-notes/ValidateVisitNote"
LOAD_REPORT_PATH = "/mychartcn/api/report-content/LoadReportContent"
DOCDETAILS_PATH = "/mychartcn/api/documents/viewer/GetDocumentDetails"

# Two distinct referers — Kaiser scopes CSRF tokens by page. Both are HTML
# container pages on healthy.kaiserpermanente.org.
_PAST_DETAILS_REFERER_TPL = (
    "https://healthy.kaiserpermanente.org/mychartcn/app/visits/past-details?csn={csn}"
)
_NOTE_REFERER_TPL = (
    "https://healthy.kaiserpermanente.org/mychartcn/app/visits/note?csn={csn}"
)

DEFAULT_DOWNLOAD_DIR = Path.home() / ".openkp" / "downloads"

# AVS report identifier. Kaiser's frontend uses the literal string
# "AMB_AVS" for ambulatory After-Visit Summaries. Different mnemonics
# probably exist for inpatient (we haven't observed any).
AVS_REPORT_MNEMONIC = "AMB_AVS"

# Block-level HTML tags that imply a paragraph break in stripped text.
# Same set as messages.py uses for message-body extraction.
_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "details", "dialog", "dd",
    "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "li", "main",
    "nav", "ol", "p", "pre", "section", "table", "tbody", "td", "tfoot", "th",
    "thead", "tr", "ul",
}
_MULTISPACE_RE = re.compile(r"[ \t]+")
_UNSAFE_FILENAME_RE = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


# --- models ---


class VisitNote(BaseModel):
    """One clinical note (or the synthetic AVS entry) for a past visit."""

    note_type: str | None = None        # "Progress Notes", "Operative Note", "After Visit Summary", ...
    iso: str | None = None              # ISO-8601. Clinical notes carry full timestamp ("2025-12-04T13:36:47-08:00"); AVS carries date-only ("2025-12-04") since Kaiser only returns a display date for the visit summary.
    is_addendum: bool = False
    is_sensitive: bool = False
    provider_name: str | None = None
    content_text: str | None = None     # HTML-stripped plain text — what an LLM should read
    content_html: str | None = None     # Raw HTML, for callers that want to render it


class VisitNotesResponse(BaseModel):
    """All notes + the AVS for one past visit."""

    csn: str
    visit_type: str | None = None
    encounter_date: str | None = None
    department: str | None = None
    primary_provider: str | None = None
    notes: list[VisitNote] = Field(default_factory=list)
    after_visit_summary: VisitNote | None = None
    avs_pdf_dcs_id: str | None = None       # Pass to download_visit_avs_pdf
    avs_pdf_available: bool = False


class AvsPdfDownload(BaseModel):
    """Outcome of `download_visit_avs_pdf`."""

    status: str                         # "downloaded" | "no_pdf_available" | "error"
    path: str | None = None
    display_name: str | None = None
    reason: str | None = None


# --- public ---


async def fetch_visit_notes(client: KaiserRequest, csn: str) -> VisitNotesResponse:
    """Fetch all clinical notes + rendered AVS for one past visit."""
    if not csn:
        raise ValueError("csn is required")

    pd_referer = _PAST_DETAILS_REFERER_TPL.format(csn=csn)
    note_referer = _NOTE_REFERER_TPL.format(csn=csn)

    csrf_pd = await fetch_csrf_token(client, referer=pd_referer)

    # Step 1: visit metadata + AVS snapshot dcsID (when one exists).
    detail_payload = await _post_json(
        client, VISIT_DETAILS_PATH,
        body={"csn": csn, "eorgID": ""},
        csrf=csrf_pd, referer=pd_referer,
    )
    summary = detail_payload.get("visitSummaryInfo") if isinstance(detail_payload, dict) else None
    summary = summary if isinstance(summary, dict) else {}
    avs_info = detail_payload.get("avsInfo") if isinstance(detail_payload, dict) else None
    avs_dcs_id = _extract_avs_dcs_id(avs_info)

    # Step 2: list of clinical notes (may be empty).
    notes_payload = await _post_json(
        client, GET_NOTES_PATH,
        body={"CSN": csn, "FromPvdPage": True},
        csrf=csrf_pd, referer=pd_referer,
    )
    note_list, lrp_id = _parse_note_list(notes_payload)

    # Step 3: per-note Validate + LoadReportContent. Notes use a different
    # referer (per-page CSRF). Fetch one CSRF token and reuse for all notes.
    notes: list[VisitNote] = []
    if note_list:
        csrf_n = await fetch_csrf_token(client, referer=note_referer)
        for i, raw_note in enumerate(note_list, start=1):
            if not isinstance(raw_note, dict):
                continue
            hno_id = _str_or_none(raw_note.get("hnoID"))
            hno_dat = _str_or_none(raw_note.get("hnoDAT"))
            if not (hno_id and hno_dat and lrp_id):
                continue

            try:
                await _post_json(
                    client, VALIDATE_NOTE_PATH,
                    body={
                        "csn": csn,
                        "hnoID": hno_id,
                        "hnoDAT": hno_dat,
                        "lrpID": lrp_id,
                        "fromPvdPage": True,
                    },
                    csrf=csrf_n, referer=note_referer,
                )
            except Exception as exc:
                logger.warning("ValidateVisitNote failed on note %s: %s", i, exc)
                continue

            try:
                content_payload = await _post_json(
                    client, LOAD_REPORT_PATH,
                    body={
                        "reportID": lrp_id,
                        "contextID": hno_id,
                        "contextDAT": hno_dat,
                        "contextINI": "HNO",
                        "csn": csn,
                        "isFullReportPage": False,
                        "uniqueClass": f"EID-{i:x}",
                        "nonce": secrets.token_hex(16),
                    },
                    csrf=csrf_n, referer=note_referer,
                )
            except Exception as exc:
                logger.warning("LoadReportContent (HNO) failed on note %s: %s", i, exc)
                content_payload = {}

            html = _str_or_none(content_payload.get("reportContent")) if isinstance(content_payload, dict) else None
            notes.append(VisitNote(
                note_type=_str_or_none(raw_note.get("displayName")),
                iso=_str_or_none(raw_note.get("iso")),
                is_addendum=bool(raw_note.get("isAddendum")),
                is_sensitive=bool(raw_note.get("isNoteSensitive")),
                provider_name=_provider_name(raw_note.get("provider")),
                content_text=_html_to_text(html),
                content_html=html,
            ))

    # Step 4: rendered AVS content. AMB_AVS doesn't need note IDs — just CSN.
    avs_note: VisitNote | None = None
    try:
        avs_payload = await _post_json(
            client, LOAD_REPORT_PATH,
            body={
                "reportMnemonic": AVS_REPORT_MNEMONIC,
                "reportID": "",
                "csn": csn,
                "isFullReportPage": False,
                "uniqueClass": "EID-avs",
                "nonce": secrets.token_hex(16),
            },
            csrf=csrf_pd, referer=pd_referer,
        )
    except Exception as exc:
        logger.warning("LoadReportContent (AMB_AVS) failed: %s", exc)
        avs_payload = {}

    avs_html = _str_or_none(avs_payload.get("reportContent")) if isinstance(avs_payload, dict) else None
    if avs_html:
        avs_note = VisitNote(
            note_type="After Visit Summary",
            iso=_display_date_to_iso(summary.get("encounterDate")),
            content_text=_html_to_text(avs_html),
            content_html=avs_html,
        )

    return VisitNotesResponse(
        csn=csn,
        visit_type=_str_or_none(summary.get("visitType")),
        encounter_date=_str_or_none(summary.get("encounterDate")),
        department=_str_or_none(summary.get("department")),
        primary_provider=_str_or_none(summary.get("provider")),
        notes=notes,
        after_visit_summary=avs_note,
        avs_pdf_dcs_id=avs_dcs_id,
        avs_pdf_available=avs_dcs_id is not None,
    )


async def download_visit_avs_pdf(
    client: KaiserRequest,
    csn: str,
    download_dir: Path | None = None,
) -> AvsPdfDownload:
    """Save the canonical AVS PDF to disk for one past visit.

    Two-hop chain inside one tool call:
      1. GetVisitDetailsPast(csn) → avsInfo.avsSnapshots[0].dcsID
      2. GetDocumentDetails(dcsId) → downloadUrl
      3. GET <downloadUrl> → binary PDF

    Returns `AvsPdfDownload` with status:
      - "downloaded"          — PDF saved, `path` populated
      - "no_pdf_available"    — visit has no AVS PDF (refills, walk-ins, etc)
      - "error"               — `reason` describes what failed
    """
    if not csn:
        return AvsPdfDownload(status="error", reason="csn is empty")

    pd_referer = _PAST_DETAILS_REFERER_TPL.format(csn=csn)
    csrf = await fetch_csrf_token(client, referer=pd_referer)

    try:
        detail_payload = await _post_json(
            client, VISIT_DETAILS_PATH,
            body={"csn": csn, "eorgID": ""},
            csrf=csrf, referer=pd_referer,
        )
    except Exception as exc:
        return AvsPdfDownload(status="error", reason=f"GetVisitDetailsPast failed: {exc}")

    avs_info = detail_payload.get("avsInfo") if isinstance(detail_payload, dict) else None
    dcs_id = _extract_avs_dcs_id(avs_info)
    if not dcs_id:
        return AvsPdfDownload(
            status="no_pdf_available",
            reason="visit has no AVS document (avsSnapshots empty or no dcsID)",
        )

    try:
        det_payload = await _post_json(
            client, DOCDETAILS_PATH,
            body={
                "dcsId": dcs_id,
                "fileExtension": "PDF",
                "organizationId": "",
                "useOldMobileLink": False,
            },
            csrf=csrf, referer=pd_referer,
        )
    except Exception as exc:
        return AvsPdfDownload(status="error", reason=f"GetDocumentDetails failed: {exc}")

    download_url = _str_or_none(det_payload.get("downloadUrl"))
    if not download_url:
        return AvsPdfDownload(status="error", reason="no downloadUrl in GetDocumentDetails response")
    display_name = _str_or_none(det_payload.get("displayName")) or f"After Visit Summary {csn[:8]}"

    # Kaiser returns the URL as a relative path starting with /Documents.
    # Prepend /mychartcn the same way labs.py does.
    path = download_url if download_url.startswith("/mychartcn") else f"/mychartcn{download_url}"
    pdf_response = await client.get(
        path,
        headers={"Accept": "application/pdf,*/*", "Referer": pd_referer},
    )
    if pdf_response.status_code >= 400:
        return AvsPdfDownload(
            status="error",
            reason=f"download GET returned {pdf_response.status_code}",
        )
    pdf_bytes = pdf_response.content
    if not pdf_bytes:
        return AvsPdfDownload(status="error", reason="empty PDF response")

    out_dir = download_dir or DEFAULT_DOWNLOAD_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_filename(display_name)
    if not safe.lower().endswith(".pdf"):
        safe += ".pdf"
    out_path = out_dir / safe
    out_path.write_bytes(pdf_bytes)

    return AvsPdfDownload(
        status="downloaded",
        path=str(out_path),
        display_name=display_name,
    )


# --- private ---


async def _post_json(
    client: KaiserRequest,
    path: str,
    *,
    body: dict,
    csrf: str,
    referer: str,
) -> dict:
    """Post JSON, raise_for_status, return the parsed JSON dict (or {} on empty body)."""
    response = await client.post(
        path,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://healthy.kaiserpermanente.org",
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
            "__RequestVerificationToken": csrf,
        },
        json=body,
    )
    response.raise_for_status()
    if not response.content:
        return {}
    try:
        parsed = response.json()
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_note_list(payload: Any) -> tuple[list, str | None]:
    """Pull `noteList` and top-level `lrpID` from a GetVisitNotes response.

    Returns (notes, lrp_id). Empty list / None on a malformed payload.
    """
    if not isinstance(payload, dict):
        return [], None
    raw = payload.get("noteList")
    notes = raw if isinstance(raw, list) else []
    lrp_id = _str_or_none(payload.get("lrpID"))
    return notes, lrp_id


def _extract_avs_dcs_id(avs_info: Any) -> str | None:
    """Find the dcsID inside the avsInfo block.

    Observed shape:
        avsInfo.avsSnapshots[0].dcsID   (string, "WP-..." prefix)

    Some past visits have no AVS at all; some have multiple snapshots
    (e.g. addended encounters). We pick the first available dcsID.
    """
    if not isinstance(avs_info, dict):
        return None
    snapshots = avs_info.get("avsSnapshots")
    if not isinstance(snapshots, list):
        return None
    for snap in snapshots:
        if not isinstance(snap, dict):
            continue
        dcs = _str_or_none(snap.get("dcsID")) or _str_or_none(snap.get("dcsId"))
        if dcs:
            return dcs
    return None


def _provider_name(raw: Any) -> str | None:
    """Pull a display name out of a note's `provider` sub-object.

    Observed in our recon: `provider` was an empty `{}` for the Progress
    Notes entry, so we tolerate missing fields. When populated, Kaiser uses
    keys like `name`, `displayName`, or `firstName`+`lastName`.
    """
    if not isinstance(raw, dict):
        return None
    direct = _str_or_none(raw.get("name") or raw.get("displayName"))
    if direct:
        return direct
    first = _str_or_none(raw.get("firstName"))
    last = _str_or_none(raw.get("lastName"))
    parts = [p for p in (first, last) if p]
    if parts:
        return " ".join(parts)
    return None


def _html_to_text(html: Any) -> str | None:
    """Strip Epic-rendered HTML to plain text suitable for an LLM caller.

    Mirrors the strategy in `messages.py:_html_to_text`:
      1. Convert `<br>` to newline.
      2. Insert blank lines before block-level tags so paragraphs separate
         visually in the output.
      3. `get_text(separator=" ")` to keep inline runs cohesive.
      4. Collapse runs of spaces and consecutive blank lines.

    Epic also embeds `data-copy-context` attributes carrying internal
    encounter / patient IDs in the rendered HTML. `get_text` drops attribute
    values automatically; the stripped text never contains them.
    """
    if not isinstance(html, str) or not html.strip():
        return None
    soup = BeautifulSoup(html, "lxml")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(_BLOCK_TAGS):
        block.insert_before("\n\n")
    text = soup.get_text(separator=" ", strip=False)

    lines = [_MULTISPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
    out: list[str] = []
    prev_blank = False
    for line in lines:
        if not line:
            if prev_blank:
                continue
            prev_blank = True
            out.append("")
        else:
            prev_blank = False
            out.append(line)
    return "\n".join(out).strip() or None


def _safe_filename(name: str) -> str:
    """Filesystem-safe filename, capped at 180 chars. Same rules as labs.py."""
    cleaned = _UNSAFE_FILENAME_RE.sub("_", name).strip()
    if len(cleaned) > 180:
        cleaned = cleaned[:180].rstrip()
    return cleaned or "after-visit-summary"


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _display_date_to_iso(value: Any) -> str | None:
    """Parse Kaiser's encounter-date display string into a date-only ISO.

    Kaiser returns `encounterDate` as a display string like "Dec 04, 2025" on
    the visit summary surface. The clinical-notes path returns proper ISO
    timestamps in `noteList[i].iso`, so the AVS branch needs to convert to
    keep `VisitNote.iso` honest. Returns None if the string doesn't match
    the expected `%b %d, %Y` format.
    """
    s = _str_or_none(value)
    if s is None:
        return None
    try:
        return datetime.strptime(s, "%b %d, %Y").date().isoformat()
    except ValueError:
        return None
