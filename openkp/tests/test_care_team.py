"""Tests for scrapers/care_team.py: parser + HTTP integration.

Fixtures use fabricated provider names and opaque placeholder IDs. No PHI.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openkp.scrapers.care_team import (
    COMPONENT_NUMBER,
    LOAD_EXTERNAL_PATH,
    LOAD_PATH,
    PAGE_REFERER,
    CareTeamResponse,
    _int_or_none,
    _parse_provider,
    _parse_providers,
    _str_or_none,
    fetch_care_team,
)
from openkp.scrapers.csrf import CSRF_PATH


# --- fake data (non-PHI) ---


_FAKE_CSRF = "fake-csrf-token-abc123"


def _csrf_html(token: str = _FAKE_CSRF) -> str:
    return f'<input name="__RequestVerificationToken" type="hidden" value="{token}" />'


def _provider(**overrides) -> dict:
    """One ProvidersList entry mirroring real KP shape, fabricated values."""
    base = {
        "ID": "prov-1",
        "Name": "PAT EXAMPLE MD",
        "Photo": "https://example.invalid/photo.jpg",
        "NationalProviderID": "npid-1",
        "WebPageUrl": "https://mydoctor.kaiserpermanente.org/example/doctor/patexample",
        "InfoBlurbUrl": "",
        "AboutMeBlurb": [],
        "CanViewProviderDetails": True,
        "CanDirectSchedule": False,
        "CanRequestAppointment": False,
        "CanMessage": False,
        "CommCenterMessageUrl": "",
        "CanRequestCustomAppt": False,
        "HasNoProviderRecord": False,
        "IsNewSchedulingEnabled": True,
        "Specialty": "Family Practice",
        "Relation": "Primary Care Provider",
        "SchedulableVisitTypes": None,
        "DepartmentID": "dept-1",
        "Organizations": None,
        "IsExternal": False,
        "CareTeamStatus": 0,
        "CanHideProvider": True,
    }
    base.update(overrides)
    return base


def _internal_payload() -> dict:
    return {
        "ProvidersList": [
            _provider(),
            _provider(
                ID="prov-2",
                Name="SAM SPECIALIST MD",
                Specialty="Cardiology",
                Relation="Cardiologist",
                DepartmentID="dept-2",
                CanMessage=True,
            ),
        ],
        "DescriptiveTitle": "Care Team and Recent Providers",
        "TabColorClass": "color1",
        "IsCustomApptReqEnabled": False,
        "CustomRequestAppointmentLink": "showform&formname=ApptReqCntr",
    }


def _empty_external_payload() -> dict:
    return {
        "ProvidersList": [],
        "DescriptiveTitle": "Care Team and Recent Providers",
        "TabColorClass": "color1",
        "IsCustomApptReqEnabled": False,
        "CustomRequestAppointmentLink": "showform&formname=ApptReqCntr",
    }


# --- _str_or_none / _int_or_none ---


def test_str_or_none_strips_and_handles_empty():
    assert _str_or_none("  hi  ") == "hi"
    assert _str_or_none("") is None
    assert _str_or_none(None) is None
    assert _str_or_none("   ") is None


def test_str_or_none_coerces_non_string():
    assert _str_or_none(42) == "42"
    assert _str_or_none(0) == "0"


def test_int_or_none_accepts_int_rejects_bool():
    assert _int_or_none(0) == 0
    assert _int_or_none(3) == 3
    assert _int_or_none(True) is None
    assert _int_or_none(False) is None


def test_int_or_none_rejects_other_types():
    assert _int_or_none("0") is None
    assert _int_or_none(1.5) is None
    assert _int_or_none(None) is None


# --- _parse_provider ---


def test_parse_provider_full_field_extraction():
    p = _parse_provider(_provider(Name="  PAT EXAMPLE MD  "))
    assert p is not None
    assert p.id == "prov-1"
    assert p.name == "PAT EXAMPLE MD"
    assert p.specialty == "Family Practice"
    assert p.relation == "Primary Care Provider"
    assert p.department_id == "dept-1"
    assert p.is_external is False
    assert p.can_message is False
    assert p.can_schedule is False
    assert p.can_request_appointment is False
    assert p.can_view_details is True
    assert p.photo_url == "https://example.invalid/photo.jpg"
    assert p.provider_page_url.endswith("patexample")
    assert p.care_team_status == 0


def test_parse_provider_capability_flags_truthy():
    p = _parse_provider(_provider(CanMessage=True, CanDirectSchedule=True, CanRequestAppointment=True))
    assert p is not None
    assert p.can_message is True
    assert p.can_schedule is True
    assert p.can_request_appointment is True


def test_parse_provider_external_flag():
    p = _parse_provider(_provider(IsExternal=True))
    assert p is not None
    assert p.is_external is True


def test_parse_provider_missing_id_returns_none():
    assert _parse_provider({"Name": "no id"}) is None
    assert _parse_provider(_provider(ID="")) is None


def test_parse_provider_non_dict_returns_none():
    assert _parse_provider(None) is None
    assert _parse_provider("garbage") is None
    assert _parse_provider(42) is None


def test_parse_provider_missing_optional_fields_yield_defaults():
    p = _parse_provider({"ID": "x"})
    assert p is not None
    assert p.id == "x"
    assert p.name is None
    assert p.specialty is None
    assert p.relation is None
    assert p.is_external is False
    assert p.can_message is False
    assert p.care_team_status is None


def test_parse_provider_kaiser_int_id_is_coerced():
    p = _parse_provider({"ID": 12345, "Name": "X"})
    assert p is not None
    assert p.id == "12345"


# --- _parse_providers ---


def test_parse_providers_happy_path():
    providers = _parse_providers(_internal_payload())
    assert len(providers) == 2
    assert providers[0].id == "prov-1"
    assert providers[0].relation == "Primary Care Provider"
    assert providers[1].id == "prov-2"
    assert providers[1].specialty == "Cardiology"
    assert providers[1].can_message is True


def test_parse_providers_empty_list():
    assert _parse_providers(_empty_external_payload()) == []


def test_parse_providers_skips_unparseable_entries():
    payload = {
        "ProvidersList": [
            _provider(ID="good"),
            {"Name": "no id, dropped"},
            "garbage",
            None,
        ]
    }
    providers = _parse_providers(payload)
    assert len(providers) == 1
    assert providers[0].id == "good"


def test_parse_providers_malformed_payload_returns_empty():
    assert _parse_providers({}) == []
    assert _parse_providers({"ProvidersList": "not a list"}) == []
    assert _parse_providers(None) == []
    assert _parse_providers("garbage") == []


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
    req = httpx.Request("GET", "https://healthy.kaiserpermanente.org" + LOAD_PATH)
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
async def test_fetch_care_team_happy_path():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_internal_payload()),
        httpx.Response(200, json=_empty_external_payload()),
    ])
    try:
        response = await fetch_care_team(KaiserRequest(store))
    finally:
        p.stop()

    assert isinstance(response, CareTeamResponse)
    assert response.total_count == 2
    assert response.providers[0].name == "PAT EXAMPLE MD"
    assert response.providers[1].relation == "Cardiologist"

    # Three HTTP calls: CSRF GET, internal POST, external POST
    assert mock_client.request.await_count == 3

    csrf_call = mock_client.request.await_args_list[0]
    assert csrf_call.args[0] == "GET"
    assert CSRF_PATH in csrf_call.args[1]

    internal_call = mock_client.request.await_args_list[1]
    assert internal_call.args[0] == "POST"
    assert LOAD_PATH in internal_call.args[1]
    headers = internal_call.kwargs["headers"]
    assert headers["__RequestVerificationToken"] == _FAKE_CSRF
    assert headers["Referer"] == PAGE_REFERER
    params = internal_call.kwargs["params"]
    assert params["ComponentNumber"] == COMPONENT_NUMBER
    assert params["isPrimaryStandalone"] == "true"
    assert "noCache" in params

    external_call = mock_client.request.await_args_list[2]
    assert external_call.args[0] == "POST"
    assert LOAD_EXTERNAL_PATH in external_call.args[1]
    # External call carries the same CSRF token, no isPrimaryStandalone param
    assert external_call.kwargs["headers"]["__RequestVerificationToken"] == _FAKE_CSRF
    assert "isPrimaryStandalone" not in external_call.kwargs["params"]


@pytest.mark.asyncio
async def test_fetch_care_team_merges_external_providers():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    external = {
        "ProvidersList": [
            _provider(ID="ext-1", Name="OUTSIDE DOC MD", IsExternal=True, Specialty="Dermatology"),
        ],
    }
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_internal_payload()),
        httpx.Response(200, json=external),
    ])
    try:
        response = await fetch_care_team(KaiserRequest(store))
    finally:
        p.stop()

    assert response.total_count == 3
    assert response.providers[-1].id == "ext-1"
    assert response.providers[-1].is_external is True


@pytest.mark.asyncio
async def test_fetch_care_team_external_failure_returns_internal():
    """A non-200 from LoadExternal must not lose the internal roster."""
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_internal_payload()),
        httpx.Response(500, text="boom"),
    ])
    try:
        response = await fetch_care_team(KaiserRequest(store))
    finally:
        p.stop()

    assert response.total_count == 2
    assert response.providers[0].id == "prov-1"


@pytest.mark.asyncio
async def test_fetch_care_team_empty_roster():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_empty_external_payload()),
        httpx.Response(200, json=_empty_external_payload()),
    ])
    try:
        response = await fetch_care_team(KaiserRequest(store))
    finally:
        p.stop()

    assert response.total_count == 0
    assert response.providers == []
