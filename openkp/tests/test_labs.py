"""Tests for scrapers/labs.py — list, read, and PDF-download flows.

Fixtures use fabricated patient names, provider names, and lab values. No PHI.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openkp.scrapers.csrf import CSRF_PATH
from openkp.scrapers.labs import (
    DEFAULT_DOWNLOAD_DIR,
    DETAILS_PATH,
    DOCDETAILS_PATH,
    DOCGEN_PATH,
    LIST_PATH,
    PAGE_PATH,
    RESULT_TYPE_LAB,
    LabComponent,
    LabPdfDownload,
    LabResult,
    LabResultDetail,
    _fetch_page_nonce,
    _html_to_text,
    _parse_components,
    _parse_reference_range,
    _parse_result_detail,
    _parse_result_list,
    _safe_filename,
    download_lab_result_pdf,
    fetch_lab_result,
    fetch_lab_results,
)

_FAKE_NONCE = "abcd1234567890efabcd1234567890ef"
_FAKE_CSRF = "fake-csrf-token"


def _page_html(nonce: str = _FAKE_NONCE) -> str:
    return (
        "<html><head>"
        f'<style nonce="{nonce}" type="text/css">.x {{display:none}}</style>'
        "</head><body>stub</body></html>"
    )


def _csrf_html(token: str = _FAKE_CSRF) -> str:
    return f'<input name="__RequestVerificationToken" type="hidden" value="{token}" />'


# --- fake list response ---


def _sample_list_payload() -> dict:
    return {
        "areResultsFullyLoaded": True,
        "isGroupingFullyLoaded": True,
        "groupBy": "ORDER",
        "newResultGroups": [
            {
                "key": "group-1",
                "resultList": ["order-lab-a"],
                "sortDate": "2025-10-01T10:00:00-07:00",
                "formattedDate": "Oct 1, 2025",
                "organizationID": "org-nca",
                "isInpatient": False,
            },
            {
                "key": "group-2",
                "resultList": ["order-imaging-b"],
                "sortDate": "2024-06-15T09:00:00-07:00",
                "formattedDate": "Jun 15, 2024",
                "organizationID": "org-nca",
                "isInpatient": False,
            },
            {
                "key": "group-3",
                "resultList": ["order-other-c"],
                "sortDate": "2024-03-20T08:00:00-07:00",
                "formattedDate": "Mar 20, 2024",
                "organizationID": "org-nca",
                "isInpatient": False,
            },
        ],
        "newResults": {
            "order-lab-a^": {
                "key": "order-lab-a",
                "name": "COMPREHENSIVE METABOLIC PANEL",
                "orderMetadata": {
                    "orderProviderName": "DR. FAKE PRIMARY",
                    "authorizingProviderName": "DR. FAKE PRIMARY",
                    "prioritizedInstantISO": "2025-10-01T10:15:00Z",
                    "prioritizedInstantDisplay": "Oct 1, 2025 10:15 AM",
                    "resultType": "LAB",
                },
                "isAbnormal": False,
                "hasComment": False,
            },
            "order-imaging-b^": {
                "key": "order-imaging-b",
                "name": "CHEST XRAY",
                "orderMetadata": {
                    "orderProviderName": "DR. FAKE RADIOLOGIST",
                    "prioritizedInstantISO": "2024-06-15T09:30:00Z",
                    "prioritizedInstantDisplay": "Jun 15, 2024 9:30 AM",
                    "resultType": "IMAGING",
                },
                "isAbnormal": False,
                "hasComment": True,
            },
            "order-other-c^": {
                "key": "order-other-c",
                "name": "PATHOLOGY REPORT",
                "orderMetadata": {
                    "resultType": "OTHER",
                },
                "isAbnormal": True,
                "hasComment": False,
            },
        },
    }


def _sample_lab_detail_payload() -> dict:
    return {
        "orderName": "COMPREHENSIVE METABOLIC PANEL",
        "key": "order-lab-a",
        "results": [
            {
                "name": "COMPREHENSIVE METABOLIC PANEL",
                "key": "order-lab-a",
                "orderMetadata": {
                    "orderProviderName": "DR. FAKE PRIMARY",
                    "authorizingProviderName": "DR. FAKE PRIMARY",
                    "prioritizedInstantISO": "2025-10-01T10:15:00Z",
                    "prioritizedInstantDisplay": "Oct 1, 2025 10:15 AM",
                    "resultType": "LAB",
                    "resultStatus": "Final",
                    "specimensDisplay": "Blood",
                    "collectionTimestampsDisplay": "Oct 1, 2025 9:45 AM",
                },
                "resultComponents": [
                    {
                        "componentInfo": {
                            "componentID": "c1",
                            "name": "SODIUM",
                            "commonName": "Sodium",
                            "units": "mmol/L",
                        },
                        "componentResultInfo": {
                            "value": "140",
                            "numericValue": 140,
                            "referenceRange": {
                                "displayLow": "136",
                                "displayHigh": "145",
                                "formattedReferenceRange": "136-145",
                            },
                            "abnormalFlagCategoryValue": "Normal",
                        },
                        "componentComments": {"hasContent": False, "contentAsHtml": ""},
                    },
                    {
                        "componentInfo": {
                            "componentID": "c2",
                            "name": "POTASSIUM",
                            "commonName": "Potassium",
                            "units": "mmol/L",
                        },
                        "componentResultInfo": {
                            "value": "5.4",
                            "numericValue": 5.4,
                            "referenceRange": {
                                "displayLow": "3.5",
                                "displayHigh": "5.0",
                                "formattedReferenceRange": "3.5-5.0",
                            },
                            "abnormalFlagCategoryValue": "High",
                        },
                        "componentComments": {
                            "isRTF": False,
                            "hasContent": True,
                            "contentAsString": "Sample slightly hemolyzed.",
                            "contentAsHtml": "<p>Sample slightly hemolyzed.</p>",
                        },
                    },
                ],
                "studyResult": {"hasStudyContent": False, "narrative": "", "impression": ""},
                "resultNote": {"hasContent": False, "contentAsString": "", "contentAsHtml": ""},
                "reportDetails": {"isDownloadablePDFReport": True},
                "providerComments": [],
                "isAbnormal": True,
                "hasComment": True,
            }
        ],
    }


def _sample_imaging_detail_payload() -> dict:
    return {
        "orderName": "CARDIAC DEVICE CHECK",
        "key": "order-imaging-b",
        "results": [
            {
                "name": "CARDIAC DEVICE CHECK",
                "key": "order-imaging-b",
                "orderMetadata": {
                    "orderProviderName": "DR. FAKE CARDIOLOGIST",
                    "prioritizedInstantISO": "2024-06-15T09:30:00Z",
                    "resultType": "IMAGING",
                    "resultStatus": "Final",
                },
                "resultComponents": [],
                "studyResult": {
                    "hasStudyContent": True,
                    "narrative": "<p>Device interrogation completed.</p><p>No abnormal activity noted.</p>",
                    "impression": "<p>Device functioning normally.</p>",
                },
                "resultNote": {
                    "hasContent": True,
                    "contentAsHtml": "<p>See attached PDF for full report.</p>",
                },
                "providerComments": [
                    {"contentAsHtml": "<p>Follow up in 6 months.</p>"},
                ],
                "reportDetails": {"isDownloadablePDFReport": True},
                "isAbnormal": False,
                "hasComment": True,
            }
        ],
    }


# --- _html_to_text ---


def test_html_to_text_basic():
    assert _html_to_text("<p>Hello</p>") == "Hello"
    assert _html_to_text("Plain") == "Plain"
    assert _html_to_text(None) is None
    assert _html_to_text("") is None


def test_html_to_text_preserves_paragraphs():
    out = _html_to_text("<p>One.</p><p>Two.</p>")
    assert "One." in out and "Two." in out
    assert "\n\n" in out


def test_html_to_text_decodes_entities():
    assert _html_to_text("<p>A &amp; B</p>") == "A & B"


# --- _safe_filename ---


def test_safe_filename_keeps_normal_text():
    assert _safe_filename("Result Trends - CARDIAC CHECK") == "Result Trends - CARDIAC CHECK"


def test_safe_filename_replaces_path_separators():
    assert "/" not in _safe_filename("a/b/c")
    assert "\\" not in _safe_filename("a\\b\\c")


def test_safe_filename_caps_length():
    long_name = "x" * 300
    assert len(_safe_filename(long_name)) <= 180


def test_safe_filename_handles_empty():
    assert _safe_filename("") == "result"
    assert _safe_filename("   ") == "result"


# --- _parse_reference_range ---


def test_parse_reference_range_prefers_formatted():
    raw = {
        "displayLow": "",
        "displayHigh": "",
        "lowerBoundExclusive": False,
        "upperBoundExclusive": False,
        "formattedReferenceRange": "<140",
    }
    assert _parse_reference_range(raw) == "<140"


def test_parse_reference_range_falls_back_to_low_high():
    raw = {"displayLow": "3.5", "displayHigh": "5.0", "formattedReferenceRange": ""}
    assert _parse_reference_range(raw) == "3.5 - 5.0"


def test_parse_reference_range_single_bound():
    assert _parse_reference_range({"displayLow": "70"}) == "70"
    assert _parse_reference_range({"displayHigh": "120"}) == "120"


def test_parse_reference_range_empty_dict():
    assert _parse_reference_range({}) is None


def test_parse_reference_range_accepts_plain_string():
    """Defensive fallback in case Kaiser ever sends a string instead of a dict."""
    assert _parse_reference_range("70-120") == "70-120"


def test_parse_reference_range_none_and_other_types():
    assert _parse_reference_range(None) is None
    assert _parse_reference_range(42) is None


# --- _parse_components ---


def test_parse_components_lab_values():
    components_raw = _sample_lab_detail_payload()["results"][0]["resultComponents"]
    comps = _parse_components(components_raw)
    assert len(comps) == 2
    assert comps[0].name == "SODIUM"
    assert comps[0].value == "140"
    assert comps[0].numeric_value == 140.0
    assert comps[0].units == "mmol/L"
    assert comps[0].reference_range == "136-145"
    assert comps[0].is_abnormal is False  # Normal flag
    # Potassium is flagged High
    assert comps[1].name == "POTASSIUM"
    assert comps[1].is_abnormal is True
    assert comps[1].comment == "Sample slightly hemolyzed."


def test_parse_components_empty_list():
    assert _parse_components([]) == []
    assert _parse_components(None) == []
    assert _parse_components("garbage") == []


def test_parse_components_skips_non_dict_items():
    raw = ["junk", None, {"componentInfo": {"name": "X"}, "componentResultInfo": {"value": "1"}}]
    comps = _parse_components(raw)
    assert len(comps) == 1
    assert comps[0].name == "X"


def test_parse_components_unknown_flag_is_not_abnormal():
    raw = [
        {
            "componentInfo": {"name": "X"},
            "componentResultInfo": {"value": "1", "abnormalFlagCategoryValue": "Unknown"},
        }
    ]
    comps = _parse_components(raw)
    assert comps[0].is_abnormal is False


def test_parse_components_numeric_coercion_handles_garbage():
    raw = [{"componentInfo": {"name": "X"}, "componentResultInfo": {"numericValue": "not a number"}}]
    comps = _parse_components(raw)
    assert comps[0].numeric_value is None


# --- _parse_result_list ---


def test_parse_result_list_happy_path():
    results = _parse_result_list(_sample_list_payload())
    assert len(results) == 3
    # Sorted newest-first
    assert results[0].order_key == "order-lab-a"
    assert results[0].result_type == "LAB"
    assert results[0].result_date == "2025-10-01T10:15:00Z"
    assert results[0].organization_id == "org-nca"
    # Imaging second
    assert results[1].order_key == "order-imaging-b"
    assert results[1].result_type == "IMAGING"
    # Other last (oldest) — has no ISO date at order level, falls back to group sortDate
    assert results[2].order_key == "order-other-c"
    assert results[2].is_abnormal is True


def test_parse_result_list_malformed_returns_empty():
    assert _parse_result_list({}) == []
    assert _parse_result_list(None) == []
    assert _parse_result_list({"newResults": "not a dict"}) == []


def test_parse_result_list_no_groups():
    # If groups are missing, each order still shows up but with no org_id / group date
    payload = _sample_list_payload()
    payload["newResultGroups"] = None
    results = _parse_result_list(payload)
    assert len(results) == 3
    assert all(r.organization_id is None for r in results)


def test_parse_result_list_skips_non_dict_entries():
    payload = {"newResults": {"a^": "garbage", "b^": {"key": "b", "name": "X", "orderMetadata": {"resultType": "LAB"}}}}
    results = _parse_result_list(payload)
    assert len(results) == 1
    assert results[0].order_key == "b"


# --- _parse_result_detail ---


def test_parse_result_detail_lab_happy_path():
    detail = _parse_result_detail(_sample_lab_detail_payload())
    assert detail is not None
    assert detail.order_key == "order-lab-a"
    assert detail.name == "COMPREHENSIVE METABOLIC PANEL"
    assert detail.result_type == "LAB"
    assert detail.status == "Final"
    assert detail.specimen == "Blood"
    assert detail.has_pdf is True
    assert len(detail.components) == 2
    assert detail.components[1].is_abnormal is True


def test_parse_result_detail_imaging_narrative():
    detail = _parse_result_detail(_sample_imaging_detail_payload())
    assert detail is not None
    assert detail.result_type == "IMAGING"
    assert "Device interrogation completed." in detail.narrative
    assert "No abnormal activity noted." in detail.narrative
    assert detail.impression == "Device functioning normally."
    assert detail.result_note == "See attached PDF for full report."
    assert detail.provider_comments == ["Follow up in 6 months."]
    assert detail.components == []


def test_parse_result_detail_missing_results_returns_none():
    assert _parse_result_detail({}) is None
    assert _parse_result_detail({"results": []}) is None
    assert _parse_result_detail(None) is None


def test_parse_result_detail_missing_order_key_returns_none():
    payload = {"results": [{"name": "X", "orderMetadata": {}}]}
    assert _parse_result_detail(payload) is None


def test_parse_result_detail_derives_has_comment_from_component_comments():
    """Kaiser's details response sets `hasComment: false` at the order level
    even when a component has a long assay comment. We normalize: has_comment
    should be true whenever any real text surfaces."""
    payload = {
        "orderName": "THYROID",
        "key": "order-thyroid",
        "results": [
            {
                "name": "THYROID",
                "key": "order-thyroid",
                "orderMetadata": {"resultType": "LAB"},
                "hasComment": False,  # Kaiser's order-level flag, which lies
                "resultComponents": [
                    {
                        "componentInfo": {"name": "TSI", "units": "%"},
                        "componentResultInfo": {
                            "value": "<89",
                            "referenceRange": {"formattedReferenceRange": "<140"},
                        },
                        "componentComments": {
                            "hasContent": True,
                            "contentAsHtml": "<p>Rich assay note about Graves' disease.</p>",
                        },
                    }
                ],
                "reportDetails": {"isDownloadablePDFReport": False},
            }
        ],
    }
    detail = _parse_result_detail(payload)
    assert detail is not None
    assert detail.has_comment is True
    assert detail.components[0].comment == "Rich assay note about Graves' disease."


def test_parse_result_detail_has_comment_from_provider_comments():
    payload = {
        "results": [
            {
                "key": "order-x",
                "hasComment": False,
                "orderMetadata": {},
                "resultComponents": [],
                "providerComments": [{"contentAsHtml": "<p>Follow up in a month.</p>"}],
            }
        ]
    }
    detail = _parse_result_detail(payload)
    assert detail is not None
    assert detail.has_comment is True


def test_parse_result_detail_has_comment_false_when_nothing_to_show():
    payload = {
        "results": [
            {
                "key": "order-x",
                "hasComment": False,
                "orderMetadata": {},
                "resultComponents": [
                    {
                        "componentInfo": {"name": "X"},
                        "componentResultInfo": {"value": "1"},
                        "componentComments": {"hasContent": False},
                    }
                ],
            }
        ]
    }
    detail = _parse_result_detail(payload)
    assert detail is not None
    assert detail.has_comment is False


def test_parse_result_detail_has_comment_respects_kaiser_flag_even_without_text():
    """If Kaiser says hasComment=True but we can't find the text anywhere,
    we still surface True (faithful to Kaiser's call)."""
    payload = {
        "results": [
            {
                "key": "order-x",
                "hasComment": True,
                "orderMetadata": {},
                "resultComponents": [],
            }
        ]
    }
    detail = _parse_result_detail(payload)
    assert detail is not None
    assert detail.has_comment is True


# --- HTTP integration: shared plumbing ---


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
    req = httpx.Request("GET", f"https://healthy.kaiserpermanente.org{PAGE_PATH}")
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


# --- _fetch_page_nonce ---


@pytest.mark.asyncio
async def test_fetch_page_nonce_extracts_value():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([httpx.Response(200, text=_page_html("deadbeef1234567890"))])
    try:
        nonce = await _fetch_page_nonce(KaiserRequest(store))
    finally:
        p.stop()
    assert nonce == "deadbeef1234567890"


@pytest.mark.asyncio
async def test_fetch_page_nonce_raises_when_missing():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([httpx.Response(200, text="<html>no nonce here</html>")])
    try:
        with pytest.raises(ValueError, match="Page nonce"):
            await _fetch_page_nonce(KaiserRequest(store))
    finally:
        p.stop()


# --- fetch_lab_results (list) ---


@pytest.mark.asyncio
async def test_fetch_lab_results_filters_to_lab_by_default():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_sample_list_payload()),
    ])
    try:
        results = await fetch_lab_results(KaiserRequest(store))
    finally:
        p.stop()

    # Only LAB results
    assert len(results) == 1
    assert results[0].result_type == "LAB"

    # Call 1 = CSRF GET, call 2 = list POST
    assert mock_client.request.await_count == 2
    csrf_call = mock_client.request.await_args_list[0]
    assert CSRF_PATH in csrf_call.args[1]
    list_call = mock_client.request.await_args_list[1]
    assert list_call.args[0] == "POST"
    assert LIST_PATH in list_call.args[1]
    body = list_call.kwargs["json"]
    assert body["groupType"] == "ORDER"
    assert body["searchString"] == ""
    assert body["maxResults"] == 50
    assert list_call.kwargs["headers"]["__RequestVerificationToken"] == _FAKE_CSRF


@pytest.mark.asyncio
async def test_fetch_lab_results_include_all_types():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_sample_list_payload()),
    ])
    try:
        results = await fetch_lab_results(KaiserRequest(store), include_all_types=True)
    finally:
        p.stop()

    assert len(results) == 3
    types = {r.result_type for r in results}
    assert types == {"LAB", "IMAGING", "OTHER"}


@pytest.mark.asyncio
async def test_fetch_lab_results_clamps_limit():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json={"newResults": {}, "newResultGroups": []}),
    ])
    try:
        await fetch_lab_results(KaiserRequest(store), limit=999)
    finally:
        p.stop()

    body = mock_client.request.await_args_list[1].kwargs["json"]
    # Clamped to 200 (module's _MAX_LIMIT)
    assert body["maxResults"] == 200


@pytest.mark.asyncio
async def test_fetch_lab_results_passes_search():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json={"newResults": {}, "newResultGroups": []}),
    ])
    try:
        await fetch_lab_results(KaiserRequest(store), search="creatinine")
    finally:
        p.stop()
    body = mock_client.request.await_args_list[1].kwargs["json"]
    assert body["searchString"] == "creatinine"


# --- fetch_lab_result (read one) ---


@pytest.mark.asyncio
async def test_fetch_lab_result_happy_path():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, text=_page_html()),
        httpx.Response(200, json=_sample_lab_detail_payload()),
    ])
    try:
        detail = await fetch_lab_result(KaiserRequest(store), "order-lab-a")
    finally:
        p.stop()

    assert isinstance(detail, LabResultDetail)
    assert detail.order_key == "order-lab-a"
    assert len(detail.components) == 2
    # Three calls: CSRF, page nonce, details POST
    assert mock_client.request.await_count == 3
    details_call = mock_client.request.await_args_list[2]
    assert details_call.args[0] == "POST"
    assert DETAILS_PATH in details_call.args[1]
    body = details_call.kwargs["json"]
    assert body["orderKey"] == "order-lab-a"
    assert body["PageNonce"] == _FAKE_NONCE
    assert details_call.kwargs["headers"]["__RequestVerificationToken"] == _FAKE_CSRF


@pytest.mark.asyncio
async def test_fetch_lab_result_empty_key_returns_none_without_http():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([httpx.Response(200, text=_csrf_html())])
    try:
        detail = await fetch_lab_result(KaiserRequest(store), "")
    finally:
        p.stop()
    assert detail is None
    assert mock_client.request.await_count == 0


@pytest.mark.asyncio
async def test_fetch_lab_result_propagates_http_errors():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, text=_page_html()),
        httpx.Response(500, text="boom"),
    ])
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_lab_result(KaiserRequest(store), "order-x")
    finally:
        p.stop()


# --- download_lab_result_pdf (three-hop chain) ---


@pytest.mark.asyncio
async def test_download_lab_result_pdf_happy_path(tmp_path: Path):
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    pdf_bytes = b"%PDF-1.7\nFAKE PDF BYTES\n%%EOF"
    download_url_relative = "/Documents/ViewDocument/DownloadOrStream?dcsid=doc-1&displayName=Test+Report&dcsExt=PDF"
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),  # CSRF
        httpx.Response(200, json={"documentID": "doc-1", "generationStatus": "Generated"}),  # DocGen
        httpx.Response(200, json={"downloadUrl": download_url_relative, "displayName": "Test Report"}),  # DocDetails
        httpx.Response(200, content=pdf_bytes, headers={"content-type": "application/pdf"}),  # PDF bytes
    ])
    try:
        outcome = await download_lab_result_pdf(
            KaiserRequest(store),
            "order-lab-a",
            download_dir=tmp_path,
        )
    finally:
        p.stop()

    assert isinstance(outcome, LabPdfDownload)
    assert outcome.status == "downloaded"
    assert outcome.filename == "Test Report.pdf"
    assert outcome.size_bytes == len(pdf_bytes)
    saved = Path(outcome.path)
    assert saved.exists()
    assert saved.read_bytes() == pdf_bytes


@pytest.mark.asyncio
async def test_download_lab_result_pdf_no_document_available(tmp_path: Path):
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json={"documentID": "", "generationStatus": "Pending"}),
    ])
    try:
        outcome = await download_lab_result_pdf(KaiserRequest(store), "order-x", download_dir=tmp_path)
    finally:
        p.stop()

    assert outcome.status == "no_pdf_available"
    assert outcome.path is None
    assert "Pending" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_download_lab_result_pdf_empty_order_key_returns_error():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([])
    try:
        outcome = await download_lab_result_pdf(KaiserRequest(store), "")
    finally:
        p.stop()
    assert outcome.status == "error"
    assert "empty" in (outcome.reason or "").lower()
    assert mock_client.request.await_count == 0


@pytest.mark.asyncio
async def test_download_lab_result_pdf_missing_download_url_returns_error(tmp_path: Path):
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json={"documentID": "doc-1", "generationStatus": "Generated"}),
        httpx.Response(200, json={"downloadUrl": "", "displayName": "Oops"}),
    ])
    try:
        outcome = await download_lab_result_pdf(KaiserRequest(store), "order-x", download_dir=tmp_path)
    finally:
        p.stop()
    assert outcome.status == "error"
    assert "downloadUrl" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_download_lab_result_pdf_sanitizes_filename(tmp_path: Path):
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    pdf_bytes = b"%PDF-1.7\n"
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json={"documentID": "doc-1", "generationStatus": "Generated"}),
        httpx.Response(200, json={
            "downloadUrl": "/Documents/ViewDocument/DownloadOrStream?dcsid=doc-1",
            "displayName": "a/b:c*d?.PDF",
        }),
        httpx.Response(200, content=pdf_bytes, headers={"content-type": "application/pdf"}),
    ])
    try:
        outcome = await download_lab_result_pdf(
            KaiserRequest(store),
            "order-x",
            download_dir=tmp_path,
        )
    finally:
        p.stop()
    assert outcome.status == "downloaded"
    # Unsafe chars replaced
    assert "/" not in outcome.filename
    assert ":" not in outcome.filename
    assert "*" not in outcome.filename
    assert outcome.filename.endswith(".pdf") or outcome.filename.endswith(".PDF")
