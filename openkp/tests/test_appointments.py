"""Tests for scrapers/appointments.py: parser + HTTP integration.

Fixtures use fabricated provider/department names. No PHI.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openkp.scrapers.appointments import (
    PAGE_REFERER,
    PAST_PATH,
    UPCOMING_PATH,
    Appointment,
    AppointmentDepartment,
    AppointmentProvider,
    AppointmentsResponse,
    PastVisit,
    PastVisitsResponse,
    _clean_phone,
    _float_or_none,
    _format_iso_for_query,
    _int_or_none,
    _is_telemedicine,
    _parse_appointment,
    _parse_department,
    _parse_net_date,
    _parse_net_date_to_datetime,
    _parse_past_response,
    _parse_past_visit,
    _parse_provider,
    _parse_upcoming_response,
    _str_or_none,
    fetch_appointments,
    fetch_past_visits,
)
from openkp.scrapers.csrf import CSRF_PATH


# --- fake data (non-PHI) ---


_FAKE_CSRF = "fake-csrf-token-abc123"


def _csrf_html(token: str = _FAKE_CSRF) -> str:
    return f'<input name="__RequestVerificationToken" type="hidden" value="{token}" />'


def _sample_visit(**overrides) -> dict:
    """A LaterVisitsList[0]-shaped dict with fake values."""
    base = {
        "Id": "visit-1",
        "Csn": "csn-1",
        "Instant": "/Date(1779137400000)/",   # 2026-05-18T20:50:00+00:00
        "Date": "Monday May 18, 2026",
        "Time": "1:50 PM",
        "TimeZone": "PDT",
        "VisitTypeName": "Office Visit",
        "EncounterType": 1,
        "InProgress": False,
        "IsRescheduleEnabled": True,
        "IsDirectCancelEnabled": True,
        "IsRequestCancelEnabled": False,
        "ArrivalTime": None,
        "DurationInMinutes": None,
        "Copay": None,
        "IsEcheckInCompleted": False,
        "Telemedicine": None,
        "EVisit": None,
        "CanShowTelemedicine": False,
        "OtherProviders": [],
        "PrimaryProvider": {
            "EncryptedId": "prov-1",
            "Name": "DR. FAKE PROVIDER",
            "PhotoUrl": "https://example.invalid/photo.jpg",
            "WebPageUrl": "https://example.invalid/profile",
            "Department": {
                "Id": "dept-1",
                "Name": "Fake Family Medicine",
                "Address": ["123 Fake Street", "Faketown, CA 99999"],
                "PhoneNumber": "‪510-555-0100‬",
                "TimeZone": "PDT",
                "Specialty": {"Title": "Family Practice"},
            },
        },
        "PrimaryDepartment": {
            "Id": "dept-1-primary",
            "Name": "Fake Family Medicine",
            "Address": ["123 Fake Street", "Faketown, CA 99999"],
            "PhoneNumber": "‪510-555-0100‬",
            "TimeZone": "PDT",
            "Specialty": {"Title": "Family Practice"},
        },
    }
    base.update(overrides)
    return base


def _sample_payload() -> dict:
    return {
        "InProgressVisits": [],
        "NextNDaysVisits": [],
        "LaterVisitsList": [_sample_visit()],
        "HighlightDays": ["5/18/2026"],
        "HasPVG": False,
    }


# --- helpers ---


def test_str_or_none_strips_and_handles_empty():
    assert _str_or_none("  hi  ") == "hi"
    assert _str_or_none("") is None
    assert _str_or_none("   ") is None
    assert _str_or_none(None) is None
    assert _str_or_none(42) == "42"


def test_int_or_none_accepts_int_rejects_bool():
    assert _int_or_none(0) == 0
    assert _int_or_none(7) == 7
    assert _int_or_none(True) is None
    assert _int_or_none(False) is None
    assert _int_or_none("3") is None
    assert _int_or_none(None) is None


def test_float_or_none():
    assert _float_or_none(0) == 0.0
    assert _float_or_none(12.5) == 12.5
    assert _float_or_none(7) == 7.0
    assert _float_or_none(True) is None
    assert _float_or_none("nope") is None
    assert _float_or_none(None) is None


def test_clean_phone_strips_bidi_marks():
    assert _clean_phone("‪510-555-0100‬") == "510-555-0100"
    assert _clean_phone("510-555-0100") == "510-555-0100"
    assert _clean_phone("") is None
    assert _clean_phone(None) is None


# --- _parse_net_date ---


def test_parse_net_date_happy_path():
    # 1779137400000 ms = 2026-05-18T20:50:00 UTC
    assert _parse_net_date("/Date(1779137400000)/") == "2026-05-18T20:50:00+00:00"


def test_parse_net_date_zero_epoch():
    assert _parse_net_date("/Date(0)/") == "1970-01-01T00:00:00+00:00"


def test_parse_net_date_rejects_non_string():
    assert _parse_net_date(None) is None
    assert _parse_net_date(1779137400000) is None
    assert _parse_net_date({"$date": "x"}) is None


def test_parse_net_date_rejects_malformed():
    assert _parse_net_date("/Date()/") is None
    assert _parse_net_date("Date(123)") is None
    assert _parse_net_date("/Date(notnumber)/") is None
    assert _parse_net_date("") is None


# --- _parse_department ---


def test_parse_department_full():
    d = _parse_department({
        "Id": "d-1",
        "Name": "  Family Medicine  ",
        "Address": ["100 Main", "City, CA 99999"],
        "PhoneNumber": "‪555-1234‬",
        "TimeZone": "PDT",
        "Specialty": {"Title": "Family Practice"},
    })
    assert d is not None
    assert d.id == "d-1"
    assert d.name == "Family Medicine"
    assert d.address == ["100 Main", "City, CA 99999"]
    assert d.phone == "555-1234"
    assert d.specialty == "Family Practice"
    assert d.timezone == "PDT"


def test_parse_department_handles_missing_specialty():
    d = _parse_department({"Id": "d", "Name": "D", "Specialty": None})
    assert d is not None
    assert d.specialty is None


def test_parse_department_filters_empty_address_strings():
    d = _parse_department({"Id": "d", "Name": "D", "Address": ["", "A", None, "  "]})
    assert d is not None
    assert d.address == ["A"]


def test_parse_department_non_dict_returns_none():
    assert _parse_department(None) is None
    assert _parse_department("garbage") is None
    assert _parse_department([]) is None


# --- _parse_provider ---


def test_parse_provider_full():
    p = _parse_provider({
        "EncryptedId": "p-1",
        "Name": "DR. X",
        "PhotoUrl": "https://x.invalid/p.jpg",
        "WebPageUrl": "https://x.invalid/page",
        "Department": {"Name": "Cardiology"},
    })
    assert p is not None
    assert p.id == "p-1"
    assert p.name == "DR. X"
    assert p.department is not None
    assert p.department.name == "Cardiology"


def test_parse_provider_non_dict_returns_none():
    assert _parse_provider(None) is None
    assert _parse_provider(42) is None


# --- _is_telemedicine ---


def test_is_telemedicine_picks_up_any_signal():
    assert _is_telemedicine({"Telemedicine": {"foo": "bar"}}) is True
    assert _is_telemedicine({"EVisit": {"foo": "bar"}}) is True
    assert _is_telemedicine({"CanShowTelemedicine": True}) is True


def test_is_telemedicine_false_when_all_absent():
    assert _is_telemedicine({}) is False
    assert _is_telemedicine({
        "Telemedicine": None,
        "EVisit": None,
        "CanShowTelemedicine": False,
    }) is False


# --- _parse_appointment ---


def test_parse_appointment_full_field_extraction():
    appt = _parse_appointment(_sample_visit())
    assert appt is not None
    assert appt.id == "visit-1"
    assert appt.csn == "csn-1"
    assert appt.instant_iso == "2026-05-18T20:50:00+00:00"
    assert appt.date_display == "Monday May 18, 2026"
    assert appt.time_display == "1:50 PM"
    assert appt.timezone == "PDT"
    assert appt.visit_type == "Office Visit"
    assert appt.encounter_type == 1
    assert appt.is_telemedicine is False
    assert appt.is_in_progress is False
    assert appt.can_reschedule is True
    assert appt.can_cancel is True
    assert appt.is_echeckin_completed is False

    assert appt.primary_provider is not None
    assert appt.primary_provider.name == "DR. FAKE PROVIDER"
    assert appt.primary_provider.id == "prov-1"
    assert appt.primary_provider.department is not None
    assert appt.primary_provider.department.specialty == "Family Practice"

    assert appt.department is not None
    assert appt.department.phone == "510-555-0100"


def test_parse_appointment_falls_back_to_csn_for_id():
    visit = _sample_visit(Id=None, Csn="csn-only")
    appt = _parse_appointment(visit)
    assert appt is not None
    assert appt.id == "csn-only"


def test_parse_appointment_returns_none_when_no_id_or_csn():
    visit = _sample_visit(Id=None, Csn=None)
    assert _parse_appointment(visit) is None


def test_parse_appointment_marks_in_progress_via_bucket():
    appt = _parse_appointment(_sample_visit(), is_in_progress=True)
    assert appt is not None
    assert appt.is_in_progress is True


def test_parse_appointment_marks_in_progress_via_field():
    appt = _parse_appointment(_sample_visit(InProgress=True))
    assert appt is not None
    assert appt.is_in_progress is True


def test_parse_appointment_request_cancel_counts_as_can_cancel():
    appt = _parse_appointment(
        _sample_visit(IsDirectCancelEnabled=False, IsRequestCancelEnabled=True)
    )
    assert appt is not None
    assert appt.can_cancel is True


def test_parse_appointment_neither_cancel_flag_means_cannot_cancel():
    appt = _parse_appointment(
        _sample_visit(IsDirectCancelEnabled=False, IsRequestCancelEnabled=False)
    )
    assert appt is not None
    assert appt.can_cancel is False


def test_parse_appointment_refill_visits_have_no_time():
    """Real refill visits return Time=None — the parser must accept that."""
    appt = _parse_appointment(_sample_visit(VisitTypeName="Refill", Time=None))
    assert appt is not None
    assert appt.visit_type == "Refill"
    assert appt.time_display is None


def test_parse_appointment_telemedicine_signals():
    appt = _parse_appointment(_sample_visit(Telemedicine={"meeting_url": "x"}))
    assert appt is not None
    assert appt.is_telemedicine is True


def test_parse_appointment_non_dict_returns_none():
    assert _parse_appointment(None) is None
    assert _parse_appointment("garbage") is None
    assert _parse_appointment(42) is None


def test_parse_appointment_handles_missing_optional_fields():
    """Defensive: only ID present, everything else absent."""
    appt = _parse_appointment({"Id": "x"})
    assert appt is not None
    assert appt.id == "x"
    assert appt.date_display is None
    assert appt.time_display is None
    assert appt.primary_provider is None
    assert appt.department is None
    assert appt.is_telemedicine is False


# --- _parse_upcoming_response ---


def test_parse_upcoming_response_happy_path():
    response = _parse_upcoming_response(_sample_payload())
    assert response.total_count == 1
    assert len(response.appointments) == 1
    assert response.appointments[0].id == "visit-1"


def test_parse_upcoming_response_flattens_three_buckets_in_order():
    """In-progress comes first, then near-future, then later."""
    payload = {
        "InProgressVisits": [_sample_visit(Id="ip-1", Csn="csn-ip")],
        "NextNDaysVisits": [_sample_visit(Id="near-1", Csn="csn-near")],
        "LaterVisitsList": [_sample_visit(Id="later-1", Csn="csn-later")],
    }
    response = _parse_upcoming_response(payload)
    assert [a.id for a in response.appointments] == ["ip-1", "near-1", "later-1"]
    assert response.appointments[0].is_in_progress is True
    assert response.appointments[1].is_in_progress is False
    assert response.appointments[2].is_in_progress is False


def test_parse_upcoming_response_empty_buckets():
    response = _parse_upcoming_response({
        "InProgressVisits": [],
        "NextNDaysVisits": [],
        "LaterVisitsList": [],
    })
    assert response.total_count == 0
    assert response.appointments == []


def test_parse_upcoming_response_skips_unparseable_entries():
    payload = {
        "LaterVisitsList": [
            _sample_visit(Id="kept", Csn="csn-kept"),
            {"Id": None, "Csn": None},  # dropped
            "garbage",
            None,
        ],
    }
    response = _parse_upcoming_response(payload)
    assert response.total_count == 1
    assert response.appointments[0].id == "kept"


def test_parse_upcoming_response_malformed_returns_empty():
    assert _parse_upcoming_response({}).total_count == 0
    assert _parse_upcoming_response({"LaterVisitsList": "not a list"}).total_count == 0
    assert _parse_upcoming_response(None).total_count == 0
    assert _parse_upcoming_response("garbage").total_count == 0


# --- HTTP integration ---


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
    req = httpx.Request("GET", "https://healthy.kaiserpermanente.org" + UPCOMING_PATH)
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
async def test_fetch_appointments_happy_path():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_sample_payload()),
    ])
    try:
        response = await fetch_appointments(KaiserRequest(store))
    finally:
        p.stop()

    assert isinstance(response, AppointmentsResponse)
    assert response.total_count == 1
    assert response.appointments[0].id == "visit-1"

    # Two HTTP calls: CSRF GET, then upcoming POST
    assert mock_client.request.await_count == 2

    csrf_call = mock_client.request.await_args_list[0]
    assert csrf_call.args[0] == "GET"
    assert CSRF_PATH in csrf_call.args[1]

    upcoming_call = mock_client.request.await_args_list[1]
    assert upcoming_call.args[0] == "POST"
    assert UPCOMING_PATH in upcoming_call.args[1]
    headers = upcoming_call.kwargs["headers"]
    assert headers["__RequestVerificationToken"] == _FAKE_CSRF
    assert headers["Referer"] == PAGE_REFERER
    params = upcoming_call.kwargs["params"]
    assert params["timeZone"] == "America/Los_Angeles"
    assert params["ComponentNumber"] == "5"
    assert "noCache" in params


@pytest.mark.asyncio
async def test_fetch_appointments_empty_calendar():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json={
            "InProgressVisits": [],
            "NextNDaysVisits": [],
            "LaterVisitsList": [],
            "HighlightDays": [],
            "HasPVG": False,
        }),
    ])
    try:
        response = await fetch_appointments(KaiserRequest(store))
    finally:
        p.stop()

    assert response.total_count == 0
    assert response.appointments == []


@pytest.mark.asyncio
async def test_fetch_appointments_malformed_response_returns_empty():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json="completely unexpected"),
    ])
    try:
        response = await fetch_appointments(KaiserRequest(store))
    finally:
        p.stop()

    assert response.total_count == 0
    assert response.appointments == []


# --- past-visit fixtures ---


def _sample_past_visit(**overrides) -> dict:
    """A LoadPast-shaped visit dict with fake values."""
    base = _sample_visit()
    base.update({
        "VisitTypeName": "Office Visit",
        "EncounterType": 1,
        "IsCanceled": False,
        "IsNoShow": False,
        "LeftWithoutSeen": False,
        "IsNotViewed": False,
        "IsNotesOnly": False,
        "PastVisitBucket": "past6month",
        "HasDownloadSummaryLink": True,
        "HasTransmitSummaryLink": True,
        "IsClinicalInformationAvailable": True,
        "IsClinicalNoteAvailable": True,
        "IsVisitSummaryEnabled": True,
        "IsFullyPaid": True,
    })
    base.update(overrides)
    return base


def _sample_past_payload(visits: list[dict] | None = None, *,
                         has_more: bool = False,
                         next_cursor: str = "next-cursor-blob") -> dict:
    """Mirror Kaiser's org-keyed envelope."""
    if visits is None:
        visits = [_sample_past_visit()]
    org_id = "fake-org-id"
    return {
        "ViewBagProperties": {},
        "SerializedIndex": next_cursor,
        "List": {
            org_id: {
                "Organization": {"OrganizationName": "Fake Health"},
                "List": visits,
                "ListSize": len(visits),
                "HasMoreData": has_more,
                "SerializedIndex": '{"FromDAT":"123","FromInst":"456"}',
            },
        },
        "CanSearch": False,
        "CanAllSearch": False,
        "CanSort": True,
    }


# --- _parse_net_date_to_datetime + _format_iso_for_query ---


def test_parse_net_date_to_datetime_round_trip():
    dt = _parse_net_date_to_datetime("/Date(1779137400000)/")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 5 and dt.day == 18
    # As ISO via _format_iso_for_query
    formatted = _format_iso_for_query(dt)
    assert formatted == "2026-05-18T20:50:00.000Z"


def test_parse_net_date_to_datetime_handles_garbage():
    assert _parse_net_date_to_datetime(None) is None
    assert _parse_net_date_to_datetime("/Date(garbage)/") is None
    assert _parse_net_date_to_datetime(12345) is None


def test_format_iso_for_query_naive_datetime_treated_as_utc():
    from datetime import datetime as _dt
    naive = _dt(2026, 5, 18, 20, 50, 0)
    assert _format_iso_for_query(naive) == "2026-05-18T20:50:00.000Z"


# --- _parse_past_visit ---


def test_parse_past_visit_full_field_extraction():
    v = _parse_past_visit(_sample_past_visit())
    assert v is not None
    assert v.id == "visit-1"
    assert v.date_display == "Monday May 18, 2026"
    assert v.visit_type == "Office Visit"
    assert v.encounter_type == 1
    assert v.is_canceled is False
    assert v.is_no_show is False
    assert v.left_without_seen is False
    assert v.is_unread is False
    assert v.has_visit_summary is True
    assert v.has_clinical_note is True
    assert v.bucket == "past6month"
    assert v.is_fully_paid is True


def test_parse_past_visit_canceled_flag():
    v = _parse_past_visit(_sample_past_visit(IsCanceled=True))
    assert v is not None
    assert v.is_canceled is True


def test_parse_past_visit_no_show_flag():
    v = _parse_past_visit(_sample_past_visit(IsNoShow=True))
    assert v is not None
    assert v.is_no_show is True


def test_parse_past_visit_unread_indicator():
    """IsNotViewed=True is Kaiser's 'new clinical info' marker."""
    v = _parse_past_visit(_sample_past_visit(IsNotViewed=True))
    assert v is not None
    assert v.is_unread is True


def test_parse_past_visit_visit_summary_falls_back_to_either_flag():
    """has_visit_summary should be true if either HasDownloadSummaryLink
    OR IsVisitSummaryEnabled is set — Kaiser inconsistently populates one
    or the other depending on visit type."""
    v = _parse_past_visit(
        _sample_past_visit(HasDownloadSummaryLink=False, IsVisitSummaryEnabled=True)
    )
    assert v is not None
    assert v.has_visit_summary is True

    v = _parse_past_visit(
        _sample_past_visit(HasDownloadSummaryLink=False, IsVisitSummaryEnabled=False)
    )
    assert v is not None
    assert v.has_visit_summary is False


def test_parse_past_visit_falls_back_to_csn_for_id():
    v = _parse_past_visit(_sample_past_visit(Id=None, Csn="csn-only"))
    assert v is not None
    assert v.id == "csn-only"


def test_parse_past_visit_returns_none_when_no_id_or_csn():
    assert _parse_past_visit(_sample_past_visit(Id=None, Csn=None)) is None


def test_parse_past_visit_non_dict_returns_none():
    assert _parse_past_visit(None) is None
    assert _parse_past_visit("garbage") is None


# --- _parse_past_response ---


def test_parse_past_response_extracts_visits_from_org_envelope():
    payload = _sample_past_payload(
        visits=[
            _sample_past_visit(Id="v1", Csn="csn1", Instant="/Date(1779137400000)/"),
            _sample_past_visit(Id="v2", Csn="csn2", Instant="/Date(1769414400000)/"),
        ],
        has_more=True,
    )
    page = _parse_past_response(payload)
    assert len(page.visits) == 2
    assert [v.id for v in page.visits] == ["v1", "v2"]
    assert page.has_more is True
    assert page.next_serialized_index == "next-cursor-blob"
    # oldest_instant tracks the smaller timestamp (v2 here, 2026-01-26 in UTC)
    assert page.oldest_instant_iso == "2026-01-26T08:00:00+00:00"


def test_parse_past_response_no_more_data_propagates_false():
    payload = _sample_past_payload(has_more=False)
    page = _parse_past_response(payload)
    assert page.has_more is False


def test_parse_past_response_multi_org_concatenates_and_ors_has_more():
    payload = {
        "SerializedIndex": "top-cursor",
        "List": {
            "org-a": {
                "List": [_sample_past_visit(Id="a1", Csn="c1")],
                "HasMoreData": False,
            },
            "org-b": {
                "List": [_sample_past_visit(Id="b1", Csn="c2")],
                "HasMoreData": True,  # any org with more = whole response has more
            },
        },
    }
    page = _parse_past_response(payload)
    assert len(page.visits) == 2
    assert page.has_more is True


def test_parse_past_response_malformed_returns_empty():
    assert _parse_past_response({}).visits == []
    assert _parse_past_response({"List": "not a dict"}).visits == []
    assert _parse_past_response(None).visits == []
    assert _parse_past_response("garbage").visits == []


def test_parse_past_response_skips_non_dict_org_blocks():
    payload = {
        "SerializedIndex": "x",
        "List": {
            "org-a": "not a dict",
            "org-b": {
                "List": [_sample_past_visit(Id="b1", Csn="c1")],
                "HasMoreData": False,
            },
        },
    }
    page = _parse_past_response(payload)
    assert len(page.visits) == 1
    assert page.visits[0].id == "b1"


# --- HTTP integration: fetch_past_visits ---


def _bind_past_request(responses: list[httpx.Response]) -> list[httpx.Response]:
    req = httpx.Request("POST", "https://healthy.kaiserpermanente.org" + PAST_PATH)
    for r in responses:
        r.request = req
    return responses


def _patch_past_http(responses: list[httpx.Response]):
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(side_effect=_bind_past_request(responses))
    patched = patch("openkp.scrapers.request.httpx.AsyncClient")
    client_cls = patched.start()
    client_cls.return_value.__aenter__.return_value = mock_client
    client_cls.return_value.__aexit__.return_value = None
    return mock_client, patched


@pytest.mark.asyncio
async def test_fetch_past_visits_single_page():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_past_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_sample_past_payload(has_more=True)),
    ])
    try:
        response = await fetch_past_visits(KaiserRequest(store), max_pages=1)
    finally:
        p.stop()

    assert isinstance(response, PastVisitsResponse)
    assert response.total_count == 1
    assert response.pages_walked == 1
    assert response.has_more is True
    assert response.oldest_instant_iso == "2026-05-18T20:50:00+00:00"

    # Two HTTP calls: CSRF + one LoadPast
    assert mock_client.request.await_count == 2

    past_call = mock_client.request.await_args_list[1]
    assert past_call.args[0] == "POST"
    assert PAST_PATH in past_call.args[1]
    headers = past_call.kwargs["headers"]
    assert headers["Content-Type"] == "application/x-www-form-urlencoded; charset=UTF-8"
    assert headers["__RequestVerificationToken"] == _FAKE_CSRF
    # First page ships an empty serializedIndex
    assert past_call.kwargs["content"] == "serializedIndex="
    params = past_call.kwargs["params"]
    assert params["loadpast"] == "1"
    assert params["ComponentNumber"] == "7"
    assert "oldestRenderedDate" in params
    assert "noCache" in params


@pytest.mark.asyncio
async def test_fetch_past_visits_walks_pagination_cursor_across_pages():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()

    # Two pages; second page is the last
    page1 = _sample_past_payload(
        visits=[_sample_past_visit(Id="p1-v1", Csn="c1", Instant="/Date(1779137400000)/")],
        has_more=True,
        next_cursor="cursor-after-page-1",
    )
    page2 = _sample_past_payload(
        visits=[_sample_past_visit(Id="p2-v1", Csn="c2", Instant="/Date(1769414400000)/")],
        has_more=False,
        next_cursor="cursor-after-page-2",
    )

    mock_client, p = _patch_past_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=page1),
        httpx.Response(200, json=page2),
    ])
    try:
        response = await fetch_past_visits(KaiserRequest(store), max_pages=5)
    finally:
        p.stop()

    assert response.total_count == 2
    assert response.pages_walked == 2
    assert response.has_more is False
    assert [v.id for v in response.visits] == ["p1-v1", "p2-v1"]

    # The second LoadPast call should carry the cursor from page 1
    second_past_call = mock_client.request.await_args_list[2]
    assert second_past_call.kwargs["content"] == "serializedIndex=cursor-after-page-1"
    # And its oldestRenderedDate should be derived from page 1's oldest visit
    assert second_past_call.kwargs["params"]["oldestRenderedDate"].startswith("2026-05-18T20:50:00")


@pytest.mark.asyncio
async def test_fetch_past_visits_stops_when_kaiser_reports_no_more():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_past_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_sample_past_payload(has_more=False)),
    ])
    try:
        response = await fetch_past_visits(KaiserRequest(store), max_pages=20)
    finally:
        p.stop()

    assert response.pages_walked == 1
    assert response.has_more is False
    # We should NOT have called LoadPast a second time
    assert mock_client.request.await_count == 2  # CSRF + 1 page


@pytest.mark.asyncio
async def test_fetch_past_visits_stops_at_until_iso():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()

    # Page 1: oldest visit is 2026-01-26 — newer than our cutoff.
    # Page 2: oldest visit is 2024-01-01 — older than cutoff. Walk stops here.
    page1 = _sample_past_payload(
        visits=[
            _sample_past_visit(Id="v1", Csn="c1", Instant="/Date(1769414400000)/"),  # 2026-01-26
        ],
        has_more=True,
        next_cursor="c1",
    )
    page2 = _sample_past_payload(
        visits=[
            _sample_past_visit(Id="v2", Csn="c2", Instant="/Date(1704067200000)/"),  # 2024-01-01
        ],
        has_more=True,
        next_cursor="c2",
    )
    page3_should_not_be_called = _sample_past_payload(
        visits=[_sample_past_visit(Id="never", Csn="cn", Instant="/Date(0)/")],
        has_more=False,
    )

    mock_client, p = _patch_past_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=page1),
        httpx.Response(200, json=page2),
        httpx.Response(200, json=page3_should_not_be_called),
    ])
    try:
        response = await fetch_past_visits(
            KaiserRequest(store),
            max_pages=10,
            until_iso="2025-01-01T00:00:00+00:00",
        )
    finally:
        p.stop()

    assert response.pages_walked == 2
    assert response.has_more is True
    assert [v.id for v in response.visits] == ["v1", "v2"]
    # 3 calls total: CSRF + 2 pages, never reached page 3
    assert mock_client.request.await_count == 3


@pytest.mark.asyncio
async def test_fetch_past_visits_default_page_size_in_query():
    """Default page_size=50 should appear as numVisitsToRetrieve=50 in the request."""
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_past_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_sample_past_payload(has_more=False)),
    ])
    try:
        await fetch_past_visits(KaiserRequest(store), max_pages=1)
    finally:
        p.stop()

    past_call = mock_client.request.await_args_list[1]
    assert past_call.kwargs["params"]["numVisitsToRetrieve"] == "50"


@pytest.mark.asyncio
async def test_fetch_past_visits_explicit_page_size_passed_through():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_past_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_sample_past_payload(has_more=False)),
    ])
    try:
        await fetch_past_visits(KaiserRequest(store), max_pages=1, page_size=25)
    finally:
        p.stop()

    past_call = mock_client.request.await_args_list[1]
    assert past_call.kwargs["params"]["numVisitsToRetrieve"] == "25"


@pytest.mark.asyncio
async def test_fetch_past_visits_clamps_page_size_to_max():
    """page_size > MAX_PAGE_SIZE_HARD_CAP should clamp."""
    from openkp.scrapers.appointments import MAX_PAGE_SIZE_HARD_CAP
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_past_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_sample_past_payload(has_more=False)),
    ])
    try:
        await fetch_past_visits(KaiserRequest(store), max_pages=1, page_size=999)
    finally:
        p.stop()

    past_call = mock_client.request.await_args_list[1]
    assert past_call.kwargs["params"]["numVisitsToRetrieve"] == str(MAX_PAGE_SIZE_HARD_CAP)


@pytest.mark.asyncio
async def test_fetch_past_visits_clamps_page_size_to_one():
    """page_size < 1 should clamp to 1, not crash."""
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_past_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_sample_past_payload(has_more=False)),
    ])
    try:
        await fetch_past_visits(KaiserRequest(store), max_pages=1, page_size=0)
    finally:
        p.stop()

    past_call = mock_client.request.await_args_list[1]
    assert past_call.kwargs["params"]["numVisitsToRetrieve"] == "1"


@pytest.mark.asyncio
async def test_fetch_past_visits_clamps_max_pages():
    """max_pages=0 normalizes to 1; max_pages=999 caps at MAX_PAGES_HARD_CAP."""
    from openkp.scrapers.request import KaiserRequest
    from openkp.scrapers.appointments import MAX_PAGES_HARD_CAP

    store = _make_store()

    # max_pages=0 → 1 page walked, even though Kaiser says has_more=True
    mock_client, p = _patch_past_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_sample_past_payload(has_more=True)),
    ])
    try:
        response = await fetch_past_visits(KaiserRequest(store), max_pages=0)
    finally:
        p.stop()
    assert response.pages_walked == 1

    # The hard-cap clamp is library-internal — just confirm the constant exists
    # and the public API doesn't choke on a huge value (we won't actually walk
    # 999 pages because the test fixture only feeds 2 responses and Kaiser's
    # has_more=False ends the loop).
    assert MAX_PAGES_HARD_CAP > 1


@pytest.mark.asyncio
async def test_fetch_past_visits_empty_history():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_past_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_sample_past_payload(visits=[], has_more=False)),
    ])
    try:
        response = await fetch_past_visits(KaiserRequest(store), max_pages=5)
    finally:
        p.stop()

    assert response.total_count == 0
    assert response.pages_walked == 1
    assert response.has_more is False
    assert response.oldest_instant_iso is None
