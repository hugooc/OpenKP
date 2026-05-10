"""Tests for scrapers/visit_notes.py: parser + HTTP integration.

All fixtures use fabricated names, dates, and IDs. No PHI.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openkp.scrapers.visit_notes import (
    VALIDATE_NOTE_PATH,
    VISIT_DETAILS_PATH,
    AvsPdfDownload,
    VisitNotesResponse,
    _extract_avs_dcs_id,
    _html_to_text,
    _parse_note_list,
    _provider_name,
    _safe_filename,
    _str_or_none,
    download_visit_avs_pdf,
    fetch_visit_notes,
)


# --- fake data (non-PHI) ---


_FAKE_CSRF_PD = "fake-csrf-past-details-aaa"
_FAKE_CSRF_NOTE = "fake-csrf-note-bbb"
_FAKE_CSN = "fake-csn-12345"
_FAKE_LRP_ID = "fake-lrp-id-67890"
_FAKE_HNO_ID = "fake-hno-id-99999"
_FAKE_HNO_DAT = "fake-hno-dat-77777"
_FAKE_DCS_ID = "fake-dcs-id-aaaaaa"


def _csrf_html(token: str) -> str:
    return f'<input name="__RequestVerificationToken" type="hidden" value="{token}" />'


def _detail_payload(*, with_avs: bool = True) -> dict:
    avs_snapshots = [{"dcsID": _FAKE_DCS_ID}] if with_avs else []
    return {
        "encounterType": "ambulatory",
        "csn": _FAKE_CSN,
        "dat": "fake-dat-aaa",
        "externalStatus": "NotExternalVisit",
        "notesInfo": {"isAtLeastOneNoteShareable": True},
        "avsInfo": {
            "canShowDischargeInstr": False,
            "avsLiveReport": {"reportMnemonic": "", "reportID": "", "reportContext": ""},
            "avsSnapshots": avs_snapshots,
            "hasShareableAvs": with_avs,
            "isAdmissionActive": False,
        },
        "visitSummaryInfo": {
            "summaryType": "PastAppointment",
            "department": "Sample Department",
            "provider": "DR. SAMPLE PROVIDER",
            "encounterDate": "Jan 01, 2025",
            "visitType": "Office Visit",
        },
    }


def _notes_payload(notes: list | None = None) -> dict:
    if notes is None:
        notes = [{
            "hnoID": _FAKE_HNO_ID,
            "hnoDAT": _FAKE_HNO_DAT,
            "displayName": "Progress Notes",
            "iso": "2025-01-01T10:00:00-08:00",
            "isAddendum": False,
            "provider": {},
            "isNoteSensitive": False,
        }]
    return {
        "lrpID": _FAKE_LRP_ID,
        "depPhoneNumber": "555-555-5555",
        "isAtLeastOneNoteSensitive": False,
        "noteList": notes,
    }


def _validate_payload() -> dict:
    return {
        "isAddendum": False,
        "success": True,
        "noteISO": "2025-01-01T10:00:00-08:00",
        "displayName": "Progress Notes",
        "isNoteSensitive": False,
        "isEncounterSensitive": False,
    }


def _hno_content_payload() -> dict:
    return {
        "reportContent": (
            "<div class='rpt'><div class='sectionHeader'><h1>Progress Notes</h1></div>"
            "<p>Patient seen for follow-up.</p>"
            "<p>Plan: continue current regimen.</p></div>"
        ),
        "reportCss": "<style>...</style>",
        "baseFontSize": 0,
        "stylesheets": [],
    }


def _avs_content_payload() -> dict:
    return {
        "reportContent": (
            "<div class='rpt'><h1>After Visit Summary</h1>"
            "<p>Visit Date: Jan 01, 2025</p>"
            "<p>Provider: DR. SAMPLE PROVIDER</p>"
            "<p>Instructions: take meds as prescribed.</p></div>"
        ),
        "reportCss": "<style>...</style>",
        "baseFontSize": 0,
        "stylesheets": [],
    }


def _docdetails_payload() -> dict:
    return {
        "dcsId": _FAKE_DCS_ID,
        "token": "fake-token-blob",
        "displayName": "After Visit Summary Jan 01, 2025",
        "downloadUrl": (
            f"/Documents/ViewDocument/DownloadOrStream?dcsid={_FAKE_DCS_ID}"
            "&displayName=AVS+Jan+01%2c+2025&dcsExt=PDF"
        ),
        "previewUrl": "",
        "mimeType": "application/pdf",
        "fileDescription": "After Visit Summary",
    }


# --- helpers ---


def test_str_or_none():
    assert _str_or_none("  hi  ") == "hi"
    assert _str_or_none("") is None
    assert _str_or_none(None) is None
    assert _str_or_none(0) == "0"


def test_safe_filename():
    assert _safe_filename("Normal Name.pdf") == "Normal Name.pdf"
    # Path separators + control chars get replaced
    assert "/" not in _safe_filename("a/b\\c.pdf")
    # Empty falls back to a sane default
    assert _safe_filename("") == "after-visit-summary"
    # Long names are capped
    assert len(_safe_filename("x" * 300)) <= 180


# --- _html_to_text ---


def test_html_to_text_strips_tags_keeps_paragraph_breaks():
    html = "<div><p>First.</p><p>Second.</p></div>"
    text = _html_to_text(html)
    assert "First." in text
    assert "Second." in text
    assert "<" not in text
    # Paragraphs separated by a blank line
    assert "First.\n" in text


def test_html_to_text_converts_br_to_newline():
    html = "Line one<br>Line two<br>Line three"
    text = _html_to_text(html)
    assert text.count("\n") >= 2


def test_html_to_text_collapses_runs_of_blank_lines():
    html = "<p>A</p><p></p><p></p><p></p><p>B</p>"
    text = _html_to_text(html)
    # No more than one blank line between A and B
    assert "\n\n\n" not in text


def test_html_to_text_returns_none_for_empty_or_garbage():
    assert _html_to_text(None) is None
    assert _html_to_text("") is None
    assert _html_to_text("   ") is None
    assert _html_to_text(42) is None


def test_html_to_text_drops_data_attributes():
    """Epic embeds internal IDs in data-copy-context. They must not leak."""
    html = '<div data-copy-context="||||  12345|67890||">Visible content</div>'
    text = _html_to_text(html)
    assert "12345" not in text
    assert "67890" not in text
    assert "Visible content" in text


# --- _parse_note_list ---


def test_parse_note_list_happy_path():
    notes, lrp = _parse_note_list(_notes_payload())
    assert lrp == _FAKE_LRP_ID
    assert len(notes) == 1
    assert notes[0]["hnoID"] == _FAKE_HNO_ID


def test_parse_note_list_empty_list():
    payload = {"lrpID": _FAKE_LRP_ID, "noteList": []}
    notes, lrp = _parse_note_list(payload)
    assert notes == []
    assert lrp == _FAKE_LRP_ID


def test_parse_note_list_malformed():
    assert _parse_note_list({}) == ([], None)
    assert _parse_note_list({"noteList": "not a list"}) == ([], None)
    assert _parse_note_list(None) == ([], None)
    assert _parse_note_list("garbage") == ([], None)


# --- _extract_avs_dcs_id ---


def test_extract_avs_dcs_id_happy_path():
    avs_info = {"avsSnapshots": [{"dcsID": _FAKE_DCS_ID}]}
    assert _extract_avs_dcs_id(avs_info) == _FAKE_DCS_ID


def test_extract_avs_dcs_id_picks_first_when_multiple():
    avs_info = {
        "avsSnapshots": [
            {"dcsID": "first-id"},
            {"dcsID": "second-id"},
        ]
    }
    assert _extract_avs_dcs_id(avs_info) == "first-id"


def test_extract_avs_dcs_id_handles_dcsid_camelcase():
    """Defensive: tolerate `dcsId` (lowercase 'd') as well as `dcsID`."""
    assert _extract_avs_dcs_id({"avsSnapshots": [{"dcsId": "x"}]}) == "x"


def test_extract_avs_dcs_id_skips_dictless_snapshots():
    avs_info = {"avsSnapshots": ["garbage", None, {"dcsID": "kept"}]}
    assert _extract_avs_dcs_id(avs_info) == "kept"


def test_extract_avs_dcs_id_returns_none_for_no_snapshots():
    assert _extract_avs_dcs_id({"avsSnapshots": []}) is None
    assert _extract_avs_dcs_id({"hasShareableAvs": False}) is None


def test_extract_avs_dcs_id_non_dict_returns_none():
    assert _extract_avs_dcs_id(None) is None
    assert _extract_avs_dcs_id("garbage") is None


# --- _provider_name ---


def test_provider_name_picks_name_field():
    assert _provider_name({"name": "DR. X"}) == "DR. X"


def test_provider_name_falls_back_to_displayname():
    assert _provider_name({"displayName": "DR. Y"}) == "DR. Y"


def test_provider_name_assembles_first_last():
    assert _provider_name({"firstName": "Jane", "lastName": "Sample"}) == "Jane Sample"


def test_provider_name_handles_first_only():
    assert _provider_name({"firstName": "Jane"}) == "Jane"


def test_provider_name_empty_dict_returns_none():
    assert _provider_name({}) is None


def test_provider_name_non_dict_returns_none():
    assert _provider_name(None) is None
    assert _provider_name("garbage") is None


# --- HTTP integration: fetch_visit_notes ---


def _make_store() -> MagicMock:
    from openkp.scrapers.auth import KaiserSession

    store = MagicMock()
    store.get_session = AsyncMock(
        return_value=KaiserSession(
            cookies=[{"name": "k", "value": "v", "domain": ".kp.org", "path": "/"}],
            user_agent="ua",
        )
    )
    store.invalidate = AsyncMock()
    return store


def _bind_request(responses: list[httpx.Response]) -> list[httpx.Response]:
    req = httpx.Request("GET", "https://healthy.kaiserpermanente.org/")
    for r in responses:
        r.request = req
    return responses


def _patch_http(responses: list[httpx.Response]):
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(side_effect=_bind_request(responses))
    patched = patch("openkp.scrapers.request.httpx.AsyncClient")
    client_cls = patched.start()
    client_cls.return_value.__aenter__.return_value = mock_client
    client_cls.return_value.__aexit__.return_value = None
    return mock_client, patched


@pytest.mark.asyncio
async def test_fetch_visit_notes_full_chain():
    """One note + one AVS, full flow."""
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, text=_csrf_html(_FAKE_CSRF_PD)),    # 1 CSRF (past-details)
        httpx.Response(200, json=_detail_payload()),            # 2 GetVisitDetailsPast
        httpx.Response(200, json=_notes_payload()),             # 3 GetVisitNotes
        httpx.Response(200, text=_csrf_html(_FAKE_CSRF_NOTE)),  # 4 CSRF (note)
        httpx.Response(200, json=_validate_payload()),          # 5 ValidateVisitNote
        httpx.Response(200, json=_hno_content_payload()),       # 6 LoadReportContent (HNO)
        httpx.Response(200, json=_avs_content_payload()),       # 7 LoadReportContent (AVS)
    ])
    try:
        response = await fetch_visit_notes(KaiserRequest(store), _FAKE_CSN)
    finally:
        p.stop()

    assert isinstance(response, VisitNotesResponse)
    assert response.csn == _FAKE_CSN
    assert response.visit_type == "Office Visit"
    assert response.encounter_date == "Jan 01, 2025"
    assert response.department == "Sample Department"
    assert response.primary_provider == "DR. SAMPLE PROVIDER"
    assert response.avs_pdf_dcs_id == _FAKE_DCS_ID
    assert response.avs_pdf_available is True

    assert len(response.notes) == 1
    note = response.notes[0]
    assert note.note_type == "Progress Notes"
    assert note.iso == "2025-01-01T10:00:00-08:00"
    assert note.is_addendum is False
    assert note.is_sensitive is False
    assert note.content_text and "Patient seen for follow-up." in note.content_text
    assert note.content_text and "<" not in note.content_text
    assert note.content_html and "<p>" in note.content_html

    assert response.after_visit_summary is not None
    avs = response.after_visit_summary
    assert avs.note_type == "After Visit Summary"
    assert avs.iso == "2025-01-01"  # display date "Jan 01, 2025" parsed to date-only ISO
    assert avs.content_text and "After Visit Summary" in avs.content_text

    # 7 HTTP calls total
    assert mock_client.request.await_count == 7

    # Critical: ValidateVisitNote uses the NOTE referer, not the past-details
    # one. That difference matters because Kaiser scopes CSRF tokens by page.
    validate_call = mock_client.request.await_args_list[4]
    assert VALIDATE_NOTE_PATH in validate_call.args[1]
    assert "/visits/note?csn=" in validate_call.kwargs["headers"]["Referer"]
    assert validate_call.kwargs["headers"]["__RequestVerificationToken"] == _FAKE_CSRF_NOTE

    # And the past-details endpoints use the past-details CSRF
    visit_call = mock_client.request.await_args_list[1]
    assert VISIT_DETAILS_PATH in visit_call.args[1]
    assert visit_call.kwargs["headers"]["__RequestVerificationToken"] == _FAKE_CSRF_PD


@pytest.mark.asyncio
async def test_fetch_visit_notes_skips_validate_load_when_no_notes():
    """Visit with no clinical notes still returns the AVS."""
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, text=_csrf_html(_FAKE_CSRF_PD)),   # CSRF
        httpx.Response(200, json=_detail_payload()),           # GetVisitDetailsPast
        httpx.Response(200, json=_notes_payload(notes=[])),    # GetVisitNotes — empty
        httpx.Response(200, json=_avs_content_payload()),      # LoadReportContent (AVS)
    ])
    try:
        response = await fetch_visit_notes(KaiserRequest(store), _FAKE_CSN)
    finally:
        p.stop()

    assert response.notes == []
    assert response.after_visit_summary is not None
    # No note CSRF fetched, no Validate, no HNO LoadReport — just 4 calls
    assert mock_client.request.await_count == 4


@pytest.mark.asyncio
async def test_fetch_visit_notes_handles_visit_with_no_avs():
    """avs_pdf_available=False when no avsSnapshots present."""
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html(_FAKE_CSRF_PD)),
        httpx.Response(200, json=_detail_payload(with_avs=False)),
        httpx.Response(200, json=_notes_payload(notes=[])),
        # AVS LoadReportContent still fires — Kaiser may have a rendered AVS
        # even if no PDF snapshot exists. Empty content for this test.
        httpx.Response(200, json={"reportContent": "", "stylesheets": []}),
    ])
    try:
        response = await fetch_visit_notes(KaiserRequest(store), _FAKE_CSN)
    finally:
        p.stop()

    assert response.avs_pdf_dcs_id is None
    assert response.avs_pdf_available is False
    assert response.after_visit_summary is None


@pytest.mark.asyncio
async def test_fetch_visit_notes_skips_note_with_missing_id_triplet():
    """Defensive: a noteList row missing hnoID/hnoDAT is skipped, others kept."""
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    bad_then_good = _notes_payload(notes=[
        {"hnoID": None, "hnoDAT": None, "displayName": "skipme"},
        {
            "hnoID": _FAKE_HNO_ID,
            "hnoDAT": _FAKE_HNO_DAT,
            "displayName": "Good Note",
            "iso": "2025-01-01T10:00:00-08:00",
            "isAddendum": False,
            "provider": {"name": "DR. EXAMPLE"},
            "isNoteSensitive": False,
        },
    ])
    mock_client, p = _patch_http([
        httpx.Response(200, text=_csrf_html(_FAKE_CSRF_PD)),
        httpx.Response(200, json=_detail_payload()),
        httpx.Response(200, json=bad_then_good),
        httpx.Response(200, text=_csrf_html(_FAKE_CSRF_NOTE)),
        # Validate + LoadReport for the GOOD note only
        httpx.Response(200, json=_validate_payload()),
        httpx.Response(200, json=_hno_content_payload()),
        # AVS at the end
        httpx.Response(200, json=_avs_content_payload()),
    ])
    try:
        response = await fetch_visit_notes(KaiserRequest(store), _FAKE_CSN)
    finally:
        p.stop()

    assert len(response.notes) == 1
    assert response.notes[0].note_type == "Good Note"
    assert response.notes[0].provider_name == "DR. EXAMPLE"


@pytest.mark.asyncio
async def test_fetch_visit_notes_empty_csn_raises():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    with pytest.raises(ValueError):
        await fetch_visit_notes(KaiserRequest(store), "")


# --- HTTP integration: download_visit_avs_pdf ---


@pytest.mark.asyncio
async def test_download_visit_avs_pdf_happy_path(tmp_path):
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, text=_csrf_html(_FAKE_CSRF_PD)),  # CSRF
        httpx.Response(200, json=_detail_payload()),          # GetVisitDetailsPast
        httpx.Response(200, json=_docdetails_payload()),      # GetDocumentDetails
        httpx.Response(200, content=b"%PDF-1.4 fake binary"),  # the PDF GET
    ])
    try:
        result = await download_visit_avs_pdf(
            KaiserRequest(store), _FAKE_CSN, download_dir=tmp_path,
        )
    finally:
        p.stop()

    assert isinstance(result, AvsPdfDownload)
    assert result.status == "downloaded"
    assert result.path is not None
    assert result.display_name == "After Visit Summary Jan 01, 2025"
    # File exists with the binary content
    from pathlib import Path
    saved = Path(result.path)
    assert saved.exists()
    assert saved.read_bytes() == b"%PDF-1.4 fake binary"

    # 4 HTTP calls: CSRF, GetVisitDetailsPast, GetDocumentDetails, PDF GET
    assert mock_client.request.await_count == 4

    # The download GET path goes through /mychartcn (relative path got prefixed)
    pdf_call = mock_client.request.await_args_list[3]
    assert pdf_call.args[0] == "GET"
    assert "/mychartcn/Documents/ViewDocument/DownloadOrStream" in pdf_call.args[1]


@pytest.mark.asyncio
async def test_download_visit_avs_pdf_no_avs_returns_no_pdf_available(tmp_path):
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, text=_csrf_html(_FAKE_CSRF_PD)),
        httpx.Response(200, json=_detail_payload(with_avs=False)),
    ])
    try:
        result = await download_visit_avs_pdf(
            KaiserRequest(store), _FAKE_CSN, download_dir=tmp_path,
        )
    finally:
        p.stop()

    assert result.status == "no_pdf_available"
    assert result.path is None
    # Should NOT have called GetDocumentDetails or the PDF GET
    assert mock_client.request.await_count == 2


@pytest.mark.asyncio
async def test_download_visit_avs_pdf_empty_csn():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    result = await download_visit_avs_pdf(KaiserRequest(store), "")
    assert result.status == "error"
    assert result.reason and "csn is empty" in result.reason


@pytest.mark.asyncio
async def test_download_visit_avs_pdf_no_download_url_in_details(tmp_path):
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    bad_details = {**_docdetails_payload(), "downloadUrl": ""}
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html(_FAKE_CSRF_PD)),
        httpx.Response(200, json=_detail_payload()),
        httpx.Response(200, json=bad_details),
    ])
    try:
        result = await download_visit_avs_pdf(
            KaiserRequest(store), _FAKE_CSN, download_dir=tmp_path,
        )
    finally:
        p.stop()

    assert result.status == "error"
    assert result.reason and "downloadUrl" in result.reason


@pytest.mark.asyncio
async def test_download_visit_avs_pdf_pdf_get_returns_4xx(tmp_path):
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html(_FAKE_CSRF_PD)),
        httpx.Response(200, json=_detail_payload()),
        httpx.Response(200, json=_docdetails_payload()),
        httpx.Response(403, content=b"forbidden"),
    ])
    try:
        result = await download_visit_avs_pdf(
            KaiserRequest(store), _FAKE_CSN, download_dir=tmp_path,
        )
    finally:
        p.stop()

    assert result.status == "error"
    assert result.reason and "403" in result.reason
