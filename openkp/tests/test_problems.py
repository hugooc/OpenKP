"""Tests for scrapers/problems.py: parser + HTTP integration.

Fixtures use fabricated problem names. No PHI.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openkp.scrapers.csrf import CSRF_PATH
from openkp.scrapers.problems import (
    LIST_PATH,
    PAGE_REFERER,
    ProblemsResponse,
    _int_or_none,
    _parse_problem,
    _parse_problems_response,
    _str_or_none,
    fetch_problems,
)


# --- fake data (non-PHI) ---


_FAKE_CSRF = "fake-csrf-token-abc123"


def _csrf_html(token: str = _FAKE_CSRF) -> str:
    return f'<input name="__RequestVerificationToken" type="hidden" value="{token}" />'


def _sample_payload() -> dict:
    """Mirror real KP shape: each DataList entry has both `HealthIssueItem`
    and `LocalItem` keys, both populated with the same payload."""
    item_a = {
        "Name": "Made-up Condition A",
        "ID": "prob-1",
        "EdgID": None,
        "FormattedDateNoted": "1/15/2024",
        "Organization": {"OrganizationName": "Fake Health"},
        "UpdateInformation": None,
        "Action": 0,
        "ReferenceID": None,
        "Comments": None,
        "IsReadOnly": False,
        "TempID": None,
    }
    item_b = {
        "Name": "Made-up Condition B",
        "ID": "prob-2",
        "EdgID": None,
        "FormattedDateNoted": "12/3/2023",
        "Organization": {"OrganizationName": "Fake Health"},
        "UpdateInformation": None,
        "Action": 0,
        "ReferenceID": None,
        "Comments": "Patient asked to track this.",
        "IsReadOnly": False,
        "TempID": None,
    }
    return {
        "DataList": [
            {
                "HealthIssueItem": item_a,
                "LocalItem": item_a,
                "ExternalItems": [],
                "ExternalOrgs": [],
                "ContentLinkURL": "",
                "ContentLinkPath": "",
                "Target": "",
                "HasLocalInstance": True,
            },
            {
                "HealthIssueItem": item_b,
                "LocalItem": item_b,
                "ExternalItems": [],
                "ExternalOrgs": [],
                "ContentLinkURL": "",
                "ContentLinkPath": "",
                "Target": "",
                "HasLocalInstance": True,
            },
        ],
        "DateOfBirth": "1/1/1970",
        "HealthIssuesUrl": "Clinical/HealthIssues",
        "HasUpdateSecurity": False,
        "HasStandAloneUpdateSecurity": False,
        "AlwaysShowSearchMore": False,
    }


# --- _str_or_none / _int_or_none ---


def test_str_or_none_strips_and_handles_empty():
    assert _str_or_none("  hello  ") == "hello"
    assert _str_or_none("") is None
    assert _str_or_none(None) is None
    assert _str_or_none("   ") is None


def test_str_or_none_coerces_non_string():
    assert _str_or_none(42) == "42"
    assert _str_or_none(0) == "0"


def test_int_or_none_accepts_int_rejects_bool():
    assert _int_or_none(0) == 0
    assert _int_or_none(7) == 7
    assert _int_or_none(-1) == -1
    # bools are int subclasses in Python; we explicitly reject them
    assert _int_or_none(True) is None
    assert _int_or_none(False) is None


def test_int_or_none_rejects_other_types():
    assert _int_or_none("0") is None
    assert _int_or_none(1.5) is None
    assert _int_or_none(None) is None


# --- _parse_problem ---


def test_parse_problem_prefers_local_item():
    """When LocalItem and HealthIssueItem both present, LocalItem wins."""
    entry = {
        "HealthIssueItem": {"ID": "x", "Name": "from health-issue-item"},
        "LocalItem": {"ID": "x", "Name": "from local-item"},
    }
    p = _parse_problem(entry)
    assert p is not None
    assert p.name == "from local-item"


def test_parse_problem_falls_back_to_health_issue_item():
    entry = {"HealthIssueItem": {"ID": "x", "Name": "fallback"}}
    p = _parse_problem(entry)
    assert p is not None
    assert p.name == "fallback"


def test_parse_problem_falls_back_to_bare_entry():
    """Defensive fallback for an unwrapped entry shape we haven't observed."""
    entry = {"ID": "x", "Name": "bare"}
    p = _parse_problem(entry)
    assert p is not None
    assert p.name == "bare"


def test_parse_problem_missing_id_returns_none():
    assert _parse_problem({"LocalItem": {"Name": "no id"}}) is None


def test_parse_problem_non_dict_returns_none():
    assert _parse_problem(None) is None
    assert _parse_problem("garbage") is None
    assert _parse_problem(42) is None


def test_parse_problem_full_field_extraction():
    item = {
        "Name": "  Headache  ",
        "ID": "prob-99",
        "FormattedDateNoted": "3/4/2025",
        "Action": 0,
        "IsReadOnly": True,
        "Comments": "Recurring",
    }
    p = _parse_problem({"LocalItem": item})
    assert p is not None
    assert p.id == "prob-99"
    assert p.name == "Headache"
    assert p.date_noted == "3/4/2025"
    assert p.action_code == 0
    assert p.is_read_only is True
    assert p.comments == "Recurring"


def test_parse_problem_missing_optional_fields_yield_none():
    p = _parse_problem({"LocalItem": {"ID": "p1"}})
    assert p is not None
    assert p.id == "p1"
    assert p.name is None
    assert p.date_noted is None
    assert p.action_code is None
    assert p.is_read_only is False
    assert p.comments is None


def test_parse_problem_kaiser_int_id_is_coerced():
    """Kaiser sometimes returns IDs as JSON numbers, not strings."""
    p = _parse_problem({"LocalItem": {"ID": 12345, "Name": "X"}})
    assert p is not None
    assert p.id == "12345"


# --- _parse_problems_response ---


def test_parse_problems_response_happy_path():
    response = _parse_problems_response(_sample_payload())
    assert response.total_count == 2
    assert len(response.problems) == 2
    assert response.problems[0].id == "prob-1"
    assert response.problems[0].name == "Made-up Condition A"
    assert response.problems[1].id == "prob-2"
    assert response.problems[1].comments == "Patient asked to track this."


def test_parse_problems_response_empty_data_list():
    response = _parse_problems_response({"DataList": []})
    assert response.total_count == 0
    assert response.problems == []


def test_parse_problems_response_skips_unparseable_entries():
    payload = {
        "DataList": [
            {"LocalItem": {"ID": "good", "Name": "kept"}},
            {"LocalItem": {"Name": "no id, dropped"}},
            "garbage",
            None,
        ]
    }
    response = _parse_problems_response(payload)
    assert response.total_count == 1
    assert response.problems[0].id == "good"


def test_parse_problems_response_malformed_payload_returns_empty():
    assert _parse_problems_response({}).total_count == 0
    assert _parse_problems_response({"DataList": "not a list"}).total_count == 0
    assert _parse_problems_response(None).total_count == 0
    assert _parse_problems_response("garbage").total_count == 0


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
    req = httpx.Request("GET", "https://healthy.kaiserpermanente.org" + LIST_PATH)
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
async def test_fetch_problems_happy_path():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_sample_payload()),
    ])
    try:
        response = await fetch_problems(KaiserRequest(store))
    finally:
        p.stop()

    assert isinstance(response, ProblemsResponse)
    assert response.total_count == 2
    assert response.problems[0].name == "Made-up Condition A"

    # Two HTTP calls: CSRF GET, then list POST
    assert mock_client.request.await_count == 2

    csrf_call = mock_client.request.await_args_list[0]
    assert csrf_call.args[0] == "GET"
    assert CSRF_PATH in csrf_call.args[1]

    list_call = mock_client.request.await_args_list[1]
    assert list_call.args[0] == "POST"
    assert LIST_PATH in list_call.args[1]
    headers = list_call.kwargs["headers"]
    assert headers["__RequestVerificationToken"] == _FAKE_CSRF
    assert headers["Referer"] == PAGE_REFERER
    # Body is empty JSON object
    assert list_call.kwargs["json"] == {}
    # Query params include the Kaiser-required quirks
    params = list_call.kwargs["params"]
    assert params["csn"] == "undefined"
    assert params["ComponentNumber"] == "4"
    assert "noCache" in params


@pytest.mark.asyncio
async def test_fetch_problems_empty_list():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json={"DataList": []}),
    ])
    try:
        response = await fetch_problems(KaiserRequest(store))
    finally:
        p.stop()

    assert response.total_count == 0
    assert response.problems == []
