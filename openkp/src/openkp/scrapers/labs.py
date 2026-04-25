"""Lab / test results scraper.

Three MCP tools surface from this module:

- `list_lab_results` — summaries of recent lab orders (LAB type only by default;
  Kaiser's Test Results endpoint also serves IMAGING and OTHER types).
- `read_lab_result` — full result with components, values, reference ranges,
  abnormal flags, narrative text, and provider comments.
- `download_lab_result_pdf` — save the kp.org-generated PDF report to disk,
  e.g. cardiac device interrogation reports that live only in PDF form.

Kaiser's `/mychartcn/api/test-results/*` family follows the same anti-forgery
pattern as messages:

- Every POST requires a `__RequestVerificationToken` header (shared CSRF
  helper in `scrapers/csrf.py`).
- `GetDetails` additionally requires a `PageNonce` in the JSON body, fetched
  from the test-results HTML page.
- PDF download is a three-hop chain: GetDocumentGenerationInfo → document ID,
  GetDocumentDetails → downloadUrl, GET downloadUrl → binary PDF.

Docs: `docs/research/endpoints/labs.md`
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from openkp.scrapers.csrf import fetch_csrf_token
from openkp.scrapers.request import KaiserRequest

logger = logging.getLogger(__name__)

# Endpoints
LIST_PATH = "/mychartcn/api/test-results/GetList"
DETAILS_PATH = "/mychartcn/api/test-results/GetDetails"
DOCGEN_PATH = "/mychartcn/api/test-results/GetDocumentGenerationInfo"
DOCDETAILS_PATH = "/mychartcn/api/documents/viewer/GetDocumentDetails"
PAGE_PATH = "/mychartcn/app/test-results"

# Referers. Details-level calls embed the order key.
PAGE_REFERER = "https://healthy.kaiserpermanente.org/mychartcn/app/test-results"
_DETAILS_REFERER_TMPL = (
    "https://healthy.kaiserpermanente.org/mychartcn/app/test-results/details"
    "?pageMode=1&eorderid={order_key}"
)

# Kaiser's result type enum. LAB covers numeric panels. IMAGING covers
# radiology / ECG / cardiac device checks (narrative + often a PDF).
# OTHER is a catch-all (pathology, transcriptions, etc).
RESULT_TYPE_LAB = "LAB"
RESULT_TYPE_IMAGING = "IMAGING"
RESULT_TYPE_OTHER = "OTHER"

# Kaiser caps page size at 200 in its UI. Our default matches the browser.
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200

# Abnormal flag values we treat as "not actually abnormal." Anything else
# (e.g. "High", "Low", "Critical") we surface as abnormal.
_NON_ABNORMAL_FLAGS = {"unknown", "normal", "", "none"}

# Default directory for PDF downloads. Matches the pattern used by
# SessionStore (everything user-scoped under ~/.openkp/).
DEFAULT_DOWNLOAD_DIR = Path.home() / ".openkp" / "downloads"

# Page nonce extraction — same shape as messages.py.
_NONCE_RE = re.compile(r"""nonce=['"]([a-f0-9]{16,})['"]""", re.IGNORECASE)

# HTML-to-text helpers. Bodies and comments in Kaiser's responses are HTML.
_BLOCK_TAGS = ("p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "blockquote", "pre")
_MULTISPACE_RE = re.compile(r" +")

# Characters unsafe for a filesystem path across the common cases.
_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


# --- models ---


class LabComponent(BaseModel):
    """One measurement inside a lab order (e.g. 'Creatinine', 'eGFR')."""

    name: str | None = None
    common_name: str | None = None
    value: str | None = None           # String form, always provided when there is a value
    numeric_value: float | None = None  # Kaiser's parsed numeric, when available
    units: str | None = None
    reference_range: str | None = None
    is_abnormal: bool = False
    comment: str | None = None         # From componentComments, HTML-stripped


class LabResult(BaseModel):
    """Summary of one test-results order (list view)."""

    order_key: str
    name: str | None = None
    result_date: str | None = None            # ISO timestamp
    result_date_display: str | None = None    # Kaiser's human-readable form
    ordering_provider: str | None = None
    authorizing_provider: str | None = None
    result_type: str | None = None            # "LAB", "IMAGING", "OTHER"
    is_abnormal: bool = False
    has_comment: bool = False
    organization_id: str | None = None


class LabResultDetail(LabResult):
    """Full result with all measurements, narrative, and provider comments."""

    components: list[LabComponent] = Field(default_factory=list)
    narrative: str | None = None              # studyResult.narrative (HTML-stripped)
    impression: str | None = None             # studyResult.impression (HTML-stripped)
    result_note: str | None = None            # resultNote.contentAsString
    provider_comments: list[str] = Field(default_factory=list)
    specimen: str | None = None               # orderMetadata.specimensDisplay
    status: str | None = None                 # orderMetadata.resultStatus
    collection_timestamp: str | None = None   # orderMetadata.collectionTimestampsDisplay
    has_pdf: bool = False                     # reportDetails.isDownloadablePDFReport


class LabPdfDownload(BaseModel):
    """Outcome of a PDF download attempt.

    Status values:
      - "downloaded"             — PDF is on disk, see `path`.
      - "generation_in_progress" — Kaiser is building the PDF on demand. Retry
                                   in ~30 seconds. Common on first request for
                                   cardiac device checks and imaging reports.
      - "no_pdf_available"       — No PDF exists or will exist for this order
                                   (e.g. simple LAB results like a single TSH).
      - "error"                  — Transport or IO failure; `reason` explains.
    """

    status: str
    path: str | None = None                    # Local filesystem path when downloaded
    filename: str | None = None
    size_bytes: int | None = None
    reason: str | None = None                  # When status != "downloaded"


# --- fetchers ---


async def fetch_lab_results(
    client: KaiserRequest,
    search: str = "",
    limit: int = _DEFAULT_LIMIT,
    include_all_types: bool = False,
) -> list[LabResult]:
    """List test-results orders. Filters to `resultType == LAB` by default.

    Args:
      search: Kaiser's `searchString`. Matches name and (empirically) other
        fields. Empty means no filter.
      limit: Max orders to return. Clamped to 200 (Kaiser's practical ceiling).
      include_all_types: If True, also include IMAGING and OTHER results.

    Returns an empty list on malformed responses.
    """
    csrf = await fetch_csrf_token(client, referer=PAGE_REFERER)
    payload = {
        "groupType": "ORDER",
        "searchString": search or "",
        "maxResults": max(1, min(limit, _MAX_LIMIT)),
        "isCurAdmFilterEnabled": False,
    }
    response = await client.post(LIST_PATH, headers=_api_headers(csrf), json=payload)
    response.raise_for_status()
    all_results = _parse_result_list(response.json())
    if include_all_types:
        return all_results
    return [r for r in all_results if r.result_type == RESULT_TYPE_LAB]


async def fetch_lab_result(
    client: KaiserRequest,
    order_key: str,
) -> LabResultDetail | None:
    """Read one order's full result. Returns `None` if `order_key` is empty
    or the response has no `results` entries.
    """
    if not order_key:
        return None
    referer = _DETAILS_REFERER_TMPL.format(order_key=order_key)
    csrf = await fetch_csrf_token(client, referer=referer)
    nonce = await _fetch_page_nonce(client)
    payload = {
        "orderKey": order_key,
        "organizationID": "",
        "PageNonce": nonce,
    }
    response = await client.post(
        DETAILS_PATH,
        headers=_api_headers(csrf, referer=referer),
        json=payload,
    )
    response.raise_for_status()
    return _parse_result_detail(response.json())


async def download_lab_result_pdf(
    client: KaiserRequest,
    order_key: str,
    download_dir: Path | None = None,
) -> LabPdfDownload:
    """Save a kp.org-generated PDF report for one order, return local path.

    Three-hop chain:
      1. GetDocumentGenerationInfo(orderKey) → documentID
      2. GetDocumentDetails(dcsId) → downloadUrl
      3. GET <downloadUrl> → binary PDF, saved to disk

    If the order doesn't have a PDF, returns `status='no_pdf_available'` rather
    than raising. Any HTTP failure on the chain returns `status='error'` with
    a short reason.
    """
    if not order_key:
        return LabPdfDownload(status="error", reason="order_key is empty")
    referer = _DETAILS_REFERER_TMPL.format(order_key=order_key)
    csrf = await fetch_csrf_token(client, referer=referer)

    # Step 1: ask Kaiser if the document exists / is ready
    gen_response = await client.post(
        DOCGEN_PATH,
        headers=_api_headers(csrf, referer=referer),
        json={"orderKey": order_key},
    )
    # Kaiser redirects (302) from this endpoint when no PDF document exists
    # for the order, which is typical for simple LAB results. Session-expiry
    # redirects to Ping are caught upstream by KaiserRequest, so any 3xx
    # reaching this layer means "no PDF for this order", not "you need to
    # log in again".
    if 300 <= gen_response.status_code < 400:
        logger.info(
            "GetDocumentGenerationInfo returned %s for order; treating as no_pdf_available",
            gen_response.status_code,
        )
        return LabPdfDownload(
            status="no_pdf_available",
            reason=f"GetDocumentGenerationInfo returned {gen_response.status_code} (no document for this order)",
        )
    gen_response.raise_for_status()
    gen = gen_response.json() if gen_response.content else {}
    document_id = _str_or_none(gen.get("documentID"))
    generation_status = _str_or_none(gen.get("generationStatus"))
    # Kaiser builds these PDFs on demand. The first request for a cardiac
    # device check or imaging report typically returns generationStatus =
    # "Generating" with no document_id yet. A second call ~30 seconds later
    # comes back as "Generated". We surface this as a distinct status so
    # callers know to retry rather than give up.
    if generation_status == "Generating":
        return LabPdfDownload(
            status="generation_in_progress",
            reason=f"generationStatus={generation_status!r} (Kaiser is generating the PDF, retry in ~30 seconds)",
        )
    if generation_status != "Generated" or not document_id:
        return LabPdfDownload(
            status="no_pdf_available",
            reason=f"generationStatus={generation_status!r}",
        )

    # Step 2: resolve the download URL
    det_response = await client.post(
        DOCDETAILS_PATH,
        headers=_api_headers(csrf, referer=referer),
        json={
            "dcsId": document_id,
            "fileExtension": "PDF",
            "organizationId": "",
            "useOldMobileLink": False,
        },
    )
    det_response.raise_for_status()
    det = det_response.json() if det_response.content else {}
    download_url = _str_or_none(det.get("downloadUrl"))
    if not download_url:
        return LabPdfDownload(status="error", reason="no downloadUrl in GetDocumentDetails")
    display_name = _str_or_none(det.get("displayName")) or document_id

    # Step 3: fetch the binary. downloadUrl comes back as a relative path; we
    # prepend /mychartcn if Kaiser didn't already include it.
    path = download_url if download_url.startswith("/mychartcn") else f"/mychartcn{download_url}"
    pdf_response = await client.get(
        path,
        headers={"Accept": "application/pdf,*/*", "Referer": referer},
    )
    pdf_response.raise_for_status()
    pdf_bytes = pdf_response.content
    if not pdf_bytes:
        return LabPdfDownload(status="error", reason="empty PDF response")

    out_dir = download_dir or DEFAULT_DOWNLOAD_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_filename(display_name)
    if not safe.lower().endswith(".pdf"):
        safe += ".pdf"
    out_path = out_dir / safe
    out_path.write_bytes(pdf_bytes)

    return LabPdfDownload(
        status="downloaded",
        path=str(out_path),
        filename=safe,
        size_bytes=len(pdf_bytes),
    )


# --- private helpers ---


async def _fetch_page_nonce(client: KaiserRequest) -> str:
    """GET the test-results HTML, extract the CSP nonce from a <style> tag.

    Same shape as messages.py's nonce fetch. Raises `ValueError` if the page
    doesn't contain a recognizable `nonce=...` attribute.
    """
    response = await client.get(
        PAGE_PATH,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    response.raise_for_status()
    match = _NONCE_RE.search(response.text)
    if not match:
        raise ValueError("Page nonce not found in test-results HTML")
    return match.group(1)


def _api_headers(csrf_token: str, referer: str = PAGE_REFERER) -> dict[str, str]:
    """Shared headers for the `/api/test-results/*` and related POSTs."""
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://healthy.kaiserpermanente.org",
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
        "__RequestVerificationToken": csrf_token,
    }


# --- list parser ---


def _parse_result_list(payload: Any) -> list[LabResult]:
    """Walk GetList's `newResults` dict + `newResultGroups` list.

    Kaiser's list response is normalized:
      - `newResultGroups` is a list of visit/encounter groups with dates and
        organization context.
      - `newResults` is a dict keyed by result ID (with a `^` suffix in the
        key) holding the per-order metadata.

    We fold them together into a flat list of `LabResult` summaries,
    preserving group-level date and organization data where available.
    """
    if not isinstance(payload, dict):
        return []
    new_results = payload.get("newResults")
    if not isinstance(new_results, dict):
        return []
    groups_raw = payload.get("newResultGroups")
    groups = groups_raw if isinstance(groups_raw, list) else []

    # Build an index: result_key (without trailing ^) → group metadata.
    group_index: dict[str, dict[str, Any]] = {}
    for g in groups:
        if not isinstance(g, dict):
            continue
        org_id = _str_or_none(g.get("organizationID"))
        sort_date = _str_or_none(g.get("sortDate"))
        display_date = _str_or_none(g.get("formattedDate"))
        for rid in g.get("resultList") or []:
            rid_str = _str_or_none(rid)
            if rid_str is None:
                continue
            group_index[rid_str] = {
                "organization_id": org_id,
                "sort_date": sort_date,
                "display_date": display_date,
            }

    out: list[LabResult] = []
    for key, entry in new_results.items():
        if not isinstance(entry, dict):
            continue
        # Kaiser appends '^' to result keys in the `newResults` dict; strip for
        # cross-referencing and as our canonical order_key.
        order_key = key.rstrip("^")
        # Prefer the key inside the entry if present.
        inner_key = _str_or_none(entry.get("key"))
        if inner_key:
            order_key = inner_key
        group = group_index.get(order_key, {})
        metadata = entry.get("orderMetadata") or {}

        # Some orders don't have prioritizedInstantISO; fall back to the group
        # sort date (encounter date).
        iso = _str_or_none(metadata.get("prioritizedInstantISO")) or group.get("sort_date")
        display = _str_or_none(metadata.get("prioritizedInstantDisplay")) or group.get("display_date")

        out.append(
            LabResult(
                order_key=order_key,
                name=_str_or_none(entry.get("name")),
                result_date=iso,
                result_date_display=display,
                ordering_provider=_str_or_none(metadata.get("orderProviderName")),
                authorizing_provider=_str_or_none(metadata.get("authorizingProviderName")),
                result_type=_str_or_none(metadata.get("resultType")),
                is_abnormal=bool(entry.get("isAbnormal")),
                has_comment=bool(entry.get("hasComment")),
                organization_id=group.get("organization_id"),
            )
        )

    # Sort newest-first by ISO date (None sorts last).
    out.sort(key=lambda r: r.result_date or "", reverse=True)
    return out


# --- detail parser ---


def _parse_result_detail(payload: Any) -> LabResultDetail | None:
    """Walk GetDetails response, produce a `LabResultDetail`.

    Response shape: `{"orderName": ..., "key": ..., "results": [{...}]}`.
    `results` is usually a 1-element list (one order per detail call).
    """
    if not isinstance(payload, dict):
        return None
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return None
    first = results[0]
    if not isinstance(first, dict):
        return None
    order_key = _str_or_none(first.get("key")) or _str_or_none(payload.get("key"))
    if order_key is None:
        return None
    metadata = first.get("orderMetadata") or {}

    study_result = first.get("studyResult") or {}
    result_note = first.get("resultNote") or {}
    report_details = first.get("reportDetails") or {}

    narrative = _html_to_text(study_result.get("narrative"))
    impression = _html_to_text(study_result.get("impression"))
    note_html = result_note.get("contentAsHtml") or result_note.get("contentAsString")
    note = _html_to_text(note_html) if note_html else None

    comments_raw = first.get("providerComments") or []
    provider_comments: list[str] = []
    for c in comments_raw:
        if not isinstance(c, dict):
            continue
        html = c.get("contentAsHtml") or c.get("contentAsString")
        text = _html_to_text(html)
        if text:
            provider_comments.append(text)

    components = _parse_components(first.get("resultComponents"))

    # Kaiser's details endpoint sets `hasComment` only for provider-level
    # comments, not the per-component assay notes. The list endpoint computes
    # a broader flag that includes component comments. We normalize: surface
    # `has_comment=True` whenever ANY of the real text surfaces are populated,
    # so the flag means the same thing across list and detail views.
    has_comment = (
        bool(first.get("hasComment"))
        or any(c.comment for c in components)
        or bool(provider_comments)
        or bool(narrative)
        or bool(impression)
        or bool(note)
    )

    return LabResultDetail(
        order_key=order_key,
        name=_str_or_none(first.get("name")) or _str_or_none(payload.get("orderName")),
        result_date=_str_or_none(metadata.get("prioritizedInstantISO")),
        result_date_display=_str_or_none(metadata.get("prioritizedInstantDisplay")),
        ordering_provider=_str_or_none(metadata.get("orderProviderName")),
        authorizing_provider=_str_or_none(metadata.get("authorizingProviderName")),
        result_type=_str_or_none(metadata.get("resultType")),
        is_abnormal=bool(first.get("isAbnormal")),
        has_comment=has_comment,
        organization_id=None,  # Not in details response; list has it
        components=components,
        narrative=narrative,
        impression=impression,
        result_note=note,
        provider_comments=provider_comments,
        specimen=_str_or_none(metadata.get("specimensDisplay")),
        status=_str_or_none(metadata.get("resultStatus")),
        collection_timestamp=_str_or_none(metadata.get("collectionTimestampsDisplay")),
        has_pdf=bool(report_details.get("isDownloadablePDFReport")),
    )


def _parse_components(raw: Any) -> list[LabComponent]:
    """Walk resultComponents, produce `LabComponent` rows."""
    if not isinstance(raw, list):
        return []
    out: list[LabComponent] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        info = item.get("componentInfo") or {}
        result_info = item.get("componentResultInfo") or {}
        comments = item.get("componentComments") or {}

        flag = _str_or_none(result_info.get("abnormalFlagCategoryValue"))
        is_abnormal = bool(flag) and flag.lower() not in _NON_ABNORMAL_FLAGS

        numeric = result_info.get("numericValue")
        numeric_value = _float_or_none(numeric)

        comment_text = None
        comment_html = comments.get("contentAsHtml") or comments.get("contentAsString")
        if comment_html:
            comment_text = _html_to_text(comment_html)

        out.append(
            LabComponent(
                name=_str_or_none(info.get("name")),
                common_name=_str_or_none(info.get("commonName")),
                value=_str_or_none(result_info.get("value")),
                numeric_value=numeric_value,
                units=_str_or_none(info.get("units")),
                reference_range=_parse_reference_range(result_info.get("referenceRange")),
                is_abnormal=is_abnormal,
                comment=comment_text,
            )
        )
    return out


def _parse_reference_range(raw: Any) -> str | None:
    """Extract a human-readable reference range string.

    Kaiser returns this as a nested dict, for example:

        {"displayLow": "", "displayHigh": "",
         "lowerBoundExclusive": false, "upperBoundExclusive": false,
         "formattedReferenceRange": "<140"}

    Prefer the pre-formatted `formattedReferenceRange`. Fall back to
    `<low> - <high>` if that's empty but the bounds are present. Accept a
    plain string too as a defensive fallback (we haven't observed it, but
    the type is not worth crashing over).
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return _str_or_none(raw)
    if not isinstance(raw, dict):
        return None
    formatted = _str_or_none(raw.get("formattedReferenceRange"))
    if formatted:
        return formatted
    low = _str_or_none(raw.get("displayLow"))
    high = _str_or_none(raw.get("displayHigh"))
    if low and high:
        return f"{low} - {high}"
    return low or high or None


# --- html + filename utilities ---


def _html_to_text(html: Any) -> str | None:
    """Strip HTML to plain text, preserving paragraph and line breaks.

    Same shape as the helper in messages.py. Kept local here to keep the
    scrapers independent.
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
        if line:
            out.append(line)
            prev_blank = False
        elif not prev_blank:
            out.append("")
            prev_blank = True
    return "\n".join(out).strip() or None


def _safe_filename(name: str) -> str:
    """Produce a filesystem-safe name for the PDF download.

    Replaces path separators, control chars, and Windows-reserved chars with
    underscores. Trims whitespace. Caps length at 180 to stay well under
    common filesystem limits when combined with the download directory path.
    """
    cleaned = _UNSAFE_FILENAME_RE.sub("_", name).strip()
    if len(cleaned) > 180:
        cleaned = cleaned[:180].rstrip()
    return cleaned or "result"


# --- primitive coercers ---


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
