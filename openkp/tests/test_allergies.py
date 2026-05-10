"""Tests for scrapers/allergies.py: parser + HTTP integration.

Fixtures use fabricated allergens. No PHI.

The "no known allergies" path is the primary live-verifiable case (the
test patient's real state). The populated path uses synthetic data
modeled on the problems endpoint structure since no live populated
allergy has been observed yet.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openkp.scrapers.allergies import (
    LIST_PATH,
    PAGE_REFERER,
    AllergiesResponse,
    _interpret_status,
    _parse_allergies_response,
    _parse_allergy,
    _parse_reactions,
    fetch_allergies,
)
from openkp.scrapers.csrf import CSRF_PATH


# --- fake data (non-PHI) ---


_FAKE_CSRF = "fake-csrf-token-xyz"


def _csrf_html(token: str = _FAKE_CSRF) -> str:
    return f'<input name="__RequestVerificationToken" type="hidden" value="{token}" />'


def _no_known_allergies_payload() -> dict:
    """Mirror the live empty-allergies response: empty DataList, AllergiesStatus=0."""
    return {
        "DataList": [],
        "ReactionList": [
            {"Value": "3", "Title": "Asthma and/or Shortness of Breath", "IsInactive": False},
            {"Value": "5", "Title": "Hives", "IsInactive": False},
        ],
        "DateOfBirth": "1/1/1970",
        "AllergiesUrl": "Clinical/Allergies",
        "AllergiesStatus": 0,
        "HasUpdateSecurity": False,
    }


def _populated_payload() -> dict:
    """Synthetic — mirrors the problems envelope shape with allergy-specific fields."""
    item = {
        "Name": "Penicillin (synthetic)",
        "ID": "allergy-1",
        "FormattedDateNoted": "5/2/2022",
        "Action": 0,
        "IsReadOnly": False,
        "Comments": "Discovered during dental procedure.",
        "Reactions": [
            {"Value": "5", "Title": "Hives"},
            {"Value": "3", "Title": "Asthma and/or Shortness of Breath"},
        ],
        "Severity": "moderate",
    }
    return {
        "DataList": [
            {
                "AllergyItem": item,
                "LocalItem": item,
            }
        ],
        "ReactionList": [],
        "AllergiesStatus": 1,
    }


# --- _interpret_status ---


def test_interpret_status_no_known_when_zero_and_empty():
    assert _interpret_status(0, 0) == "no_known_allergies"


def test_interpret_status_recorded_when_count_positive():
    assert _interpret_status(0, 1) == "recorded"
    assert _interpret_status(1, 5) == "recorded"
    # Even with unknown status_code, a populated list is "recorded"
    assert _interpret_status(99, 2) == "recorded"
    assert _interpret_status(None, 1) == "recorded"


def test_interpret_status_none_when_unknown_code_and_empty():
    """Empty list + non-zero status code = we don't know what to call it."""
    assert _interpret_status(1, 0) is None
    assert _interpret_status(99, 0) is None
    assert _interpret_status(None, 0) is None


# --- _parse_reactions ---


def test_parse_reactions_handles_dict_with_title():
    raw = [{"Value": "5", "Title": "Hives"}, {"Value": "3", "Title": "Anaphylaxis"}]
    assert _parse_reactions(raw) == ["Hives", "Anaphylaxis"]


def test_parse_reactions_handles_string_list():
    raw = ["Rash", "Swelling"]
    assert _parse_reactions(raw) == ["Rash", "Swelling"]


def test_parse_reactions_falls_back_to_name_or_value():
    raw = [{"Name": "from-name"}, {"Value": "from-value"}, {"Title": "from-title"}]
    assert _parse_reactions(raw) == ["from-name", "from-value", "from-title"]


def test_parse_reactions_skips_garbage():
    raw = [{}, None, "", "  ", {"Title": "kept"}]
    assert _parse_reactions(raw) == ["kept"]


def test_parse_reactions_non_list_returns_empty():
    assert _parse_reactions(None) == []
    assert _parse_reactions("garbage") == []
    assert _parse_reactions({"Title": "ignored"}) == []


# --- _parse_allergy ---


def test_parse_allergy_prefers_local_item_then_allergy_item():
    """LocalItem wins over AllergyItem when both present."""
    entry = {
        "AllergyItem": {"ID": "a", "Name": "from-allergy-item"},
        "LocalItem": {"ID": "a", "Name": "from-local-item"},
    }
    a = _parse_allergy(entry)
    assert a is not None
    assert a.name == "from-local-item"


def test_parse_allergy_falls_back_to_allergy_item():
    a = _parse_allergy({"AllergyItem": {"ID": "a", "Name": "fallback"}})
    assert a is not None
    assert a.name == "fallback"


def test_parse_allergy_falls_back_to_bare_entry():
    a = _parse_allergy({"ID": "a", "Name": "bare"})
    assert a is not None
    assert a.name == "bare"


def test_parse_allergy_full_field_extraction():
    item = {
        "ID": "allergy-99",
        "Name": "  Latex  ",
        "FormattedDateNoted": "8/12/2020",
        "Action": 0,
        "IsReadOnly": True,
        "Comments": "Discovered after surgery.",
        "Reactions": [{"Title": "Hives"}, "Itching"],
        "Severity": "mild",
    }
    a = _parse_allergy({"LocalItem": item})
    assert a is not None
    assert a.id == "allergy-99"
    assert a.name == "Latex"
    assert a.date_noted == "8/12/2020"
    assert a.action_code == 0
    assert a.is_read_only is True
    assert a.comments == "Discovered after surgery."
    assert a.reactions == ["Hives", "Itching"]
    assert a.severity == "mild"


def test_parse_allergy_missing_id_returns_none():
    assert _parse_allergy({"LocalItem": {"Name": "no id"}}) is None


def test_parse_allergy_non_dict_returns_none():
    assert _parse_allergy(None) is None
    assert _parse_allergy("garbage") is None
    assert _parse_allergy(42) is None


# --- _parse_allergies_response ---


def test_parse_no_known_allergies():
    response = _parse_allergies_response(_no_known_allergies_payload())
    assert response.total_count == 0
    assert response.allergies == []
    assert response.status == "no_known_allergies"
    assert response.status_code == 0


def test_parse_populated_response():
    response = _parse_allergies_response(_populated_payload())
    assert response.total_count == 1
    assert response.allergies[0].name == "Penicillin (synthetic)"
    assert response.allergies[0].reactions == ["Hives", "Asthma and/or Shortness of Breath"]
    assert response.status == "recorded"
    assert response.status_code == 1


def test_parse_unknown_status_code_with_empty_list():
    response = _parse_allergies_response({"DataList": [], "AllergiesStatus": 99})
    assert response.total_count == 0
    assert response.status is None
    assert response.status_code == 99


def test_parse_missing_status_code_with_empty_list():
    """No AllergiesStatus field at all — status_code falls through to None."""
    response = _parse_allergies_response({"DataList": []})
    assert response.total_count == 0
    assert response.status is None
    assert response.status_code is None


def test_parse_skips_unparseable_entries():
    payload = {
        "DataList": [
            {"LocalItem": {"ID": "good", "Name": "kept"}},
            {"LocalItem": {"Name": "no id, dropped"}},
            "garbage",
        ],
        "AllergiesStatus": 1,
    }
    response = _parse_allergies_response(payload)
    assert response.total_count == 1
    assert response.allergies[0].id == "good"


def test_parse_malformed_payload_returns_empty():
    assert _parse_allergies_response(None).total_count == 0
    assert _parse_allergies_response("garbage").total_count == 0
    assert _parse_allergies_response({}).total_count == 0


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
async def test_fetch_allergies_no_known_allergies():
    """The most common live state: empty list + status_code 0."""
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_no_known_allergies_payload()),
    ])
    try:
        response = await fetch_allergies(KaiserRequest(store))
    finally:
        p.stop()

    assert isinstance(response, AllergiesResponse)
    assert response.total_count == 0
    assert response.status == "no_known_allergies"
    assert response.status_code == 0

    # Two calls: CSRF GET, list POST
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
    assert list_call.kwargs["json"] == {}
    params = list_call.kwargs["params"]
    assert params["csn"] == "undefined"
    assert params["ComponentNumber"] == "4"


@pytest.mark.asyncio
async def test_fetch_allergies_populated():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_populated_payload()),
    ])
    try:
        response = await fetch_allergies(KaiserRequest(store))
    finally:
        p.stop()

    assert response.total_count == 1
    assert response.status == "recorded"
    assert response.allergies[0].name == "Penicillin (synthetic)"
