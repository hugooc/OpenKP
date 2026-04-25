"""Tests for scrapers/emergency_contacts.py: parser + CSRF integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openkp.scrapers.csrf import CSRF_PATH
from openkp.scrapers.emergency_contacts import (
    RELATIONSHIPS_PATH,
    ContactAddress,
    EmergencyContact,
    _build_legal_lookup,
    _build_lookup,
    _parse_address,
    _parse_relationships,
    _resolve_name,
    fetch_emergency_contacts,
)


# --- sample payload (PHI-free, structurally identical to the live capture) ---


def _sample_payload() -> dict:
    return {
        "Success": "1",
        "HasConversionToEPT1733Run": True,
        "HideRelationships": False,
        "CategoryData": {
            "1000": {
                "CategoryItems": [
                    {"Value": "7", "Title": "Spouse"},
                    {"Value": "9", "Title": "Daughter"},
                    {"Value": "1", "Title": "Father"},
                ]
            },
            "1107": {
                "CategoryItems": [
                    {"Value": "104", "Title": "Designated Decision Maker (not a legal designation)"},
                    {"Value": "105", "Title": "DPOAHC - Primary Agent"},
                ]
            },
            "1113": {
                "CategoryItems": [
                    {"Value": "104", "Title": "Designated Decision Maker (not a legal designation)"},
                    {"Value": "105", "Title": "DPOAHC - Primary Agent"},
                ]
            },
        },
        "Relationships": [
            {
                "Id": "opaque-id-1",
                "FormattedName": "Jane Sample",
                "FirstName": "Jane",
                "LastName": "Sample",
                "AddressViewModel": {
                    "Street": "123 Example St",
                    "City": "Oakland",
                    "State": {"Number": "5", "Title": "California", "Abbreviation": "CA"},
                    "Zip": "90210",
                    "Unit": None,
                    "Floor": None,
                    "Building": None,
                },
                "HomePhone": {"FieldId": "HomePhone", "Value": "415-555-1234"},
                "WorkPhone": {"FieldId": "WorkPhone", "Value": ""},
                "MobilePhone": {"FieldId": "MobilePhone", "Value": "415-555-9999"},
                "Email": {"FieldId": "Email", "Value": "jane@example.com"},
                "PreferredDevice": {"FieldId": "PreferredDevice", "Value": "3"},
                "RelationToPatient": {"FieldId": "RelationshipToPatient", "Value": "7"},
                "LegalRelationToPatient": {"FieldId": "LegalRelationshipToPatient", "Value": "104"},
                "IsActiveHealthCareAgent": False,
                "IsPrimary": True,
            },
            {
                "Id": "opaque-id-2",
                "FormattedName": "John Doe",
                "FirstName": "John",
                "LastName": "Doe",
                "AddressViewModel": None,
                "HomePhone": {"FieldId": "HomePhone", "Value": ""},
                "WorkPhone": {"FieldId": "WorkPhone", "Value": "415-555-2222"},
                "MobilePhone": {"FieldId": "MobilePhone", "Value": ""},
                "Email": {"FieldId": "Email", "Value": ""},
                "RelationToPatient": {"FieldId": "RelationshipToPatient", "Value": "9"},
                "LegalRelationToPatient": {"FieldId": "LegalRelationshipToPatient", "Value": "105"},
                "IsActiveHealthCareAgent": True,
                "IsPrimary": False,
            },
        ],
        "EndOfLifeDocuments": [],
    }


# --- _parse_relationships happy path ---


def test_parse_relationships_returns_all_entries():
    contacts = _parse_relationships(_sample_payload())
    assert len(contacts) == 2


def test_parse_first_contact_full_projection():
    c = _parse_relationships(_sample_payload())[0]
    assert c.name == "Jane Sample"
    assert c.relationship == "Spouse"
    assert c.legal_role == "Designated Decision Maker (not a legal designation)"
    assert c.is_active_healthcare_agent is False
    assert c.is_primary_contact is True
    assert c.home_phone == "415-555-1234"
    assert c.work_phone is None  # empty string normalized to None
    assert c.mobile_phone == "415-555-9999"
    assert c.email == "jane@example.com"
    assert c.address is not None
    assert c.address.street1 == "123 Example St"
    assert c.address.city == "Oakland"
    assert c.address.state == "CA"
    assert c.address.postal_code == "90210"


def test_parse_second_contact_active_healthcare_agent():
    c = _parse_relationships(_sample_payload())[1]
    assert c.name == "John Doe"
    assert c.relationship == "Daughter"
    assert c.legal_role == "DPOAHC - Primary Agent"
    assert c.is_active_healthcare_agent is True
    assert c.is_primary_contact is False
    assert c.work_phone == "415-555-2222"
    assert c.address is None  # AddressViewModel was None


# --- _parse_relationships resilience ---


def test_parse_relationships_handles_non_dict_payload():
    assert _parse_relationships(None) == []
    assert _parse_relationships("garbage") == []
    assert _parse_relationships([]) == []


def test_parse_relationships_handles_missing_relationships_key():
    assert _parse_relationships({"CategoryData": {}}) == []


def test_parse_relationships_handles_non_list_relationships():
    assert _parse_relationships({"Relationships": "not a list"}) == []


def test_parse_relationships_skips_entries_without_a_resolvable_name():
    payload = _sample_payload()
    payload["Relationships"] = [
        {"FirstName": None, "LastName": None, "FormattedName": None},
        {"FirstName": "Alice", "LastName": "Smith"},
    ]
    contacts = _parse_relationships(payload)
    assert len(contacts) == 1
    assert contacts[0].name == "Alice Smith"


def test_parse_relationships_skips_non_dict_items():
    payload = _sample_payload()
    payload["Relationships"] = ["bogus", None, payload["Relationships"][0]]
    contacts = _parse_relationships(payload)
    assert len(contacts) == 1


def test_unresolvable_relationship_code_returns_none_not_raw_code():
    """If Kaiser ships a code that isn't in the lookup table, return null —
    callers can't do anything with a raw integer like '99'."""
    payload = _sample_payload()
    payload["Relationships"][0]["RelationToPatient"]["Value"] = "9999"
    c = _parse_relationships(payload)[0]
    assert c.relationship is None


def test_empty_legal_relation_value_yields_null_legal_role():
    payload = _sample_payload()
    payload["Relationships"][0]["LegalRelationToPatient"]["Value"] = ""
    c = _parse_relationships(payload)[0]
    assert c.legal_role is None


# --- name resolution ---


def test_resolve_name_prefers_first_plus_last():
    assert _resolve_name({"FirstName": "Jane", "LastName": "Doe", "FormattedName": "Garbage"}) == "Jane Doe"


def test_resolve_name_falls_back_to_formatted_when_parts_missing():
    """Legacy contacts often have null First/Last and a freeform FormattedName."""
    assert (
        _resolve_name({"FirstName": None, "LastName": None, "FormattedName": "CALL FIRST) Alex Rivera (URGENT"})
        == "CALL FIRST) Alex Rivera (URGENT"
    )


def test_resolve_name_uses_lone_first_or_last():
    assert _resolve_name({"FirstName": "Jane", "LastName": None}) == "Jane"
    assert _resolve_name({"FirstName": None, "LastName": "Doe"}) == "Doe"


def test_resolve_name_returns_none_when_nothing_present():
    assert _resolve_name({}) is None
    assert _resolve_name({"FirstName": "", "LastName": "", "FormattedName": "  "}) is None


# --- address parsing ---


def test_parse_address_picks_unit_over_floor_and_building():
    addr = _parse_address({
        "Street": "1 Main",
        "City": "X",
        "State": {"Abbreviation": "CA"},
        "Zip": "94000",
        "Unit": "Apt 2",
        "Floor": "3",
        "Building": "B",
    })
    assert addr is not None and addr.street2 == "Apt 2"


def test_parse_address_falls_through_to_floor_when_unit_empty():
    addr = _parse_address({
        "Street": "1 Main",
        "Unit": "",
        "Floor": "3",
        "Building": "B",
    })
    assert addr is not None and addr.street2 == "3"


def test_parse_address_returns_none_when_street_blank():
    """All-null address objects are noise; better to return null."""
    assert _parse_address({"Street": "", "City": "Oakland"}) is None
    assert _parse_address({"City": "Oakland"}) is None


def test_parse_address_handles_non_dict():
    assert _parse_address(None) is None
    assert _parse_address("oops") is None


def test_parse_address_state_abbreviation_extracted_from_nested_object():
    addr = _parse_address({"Street": "1 Main", "State": {"Abbreviation": "NY", "Title": "New York"}})
    assert addr is not None and addr.state == "NY"


def test_parse_address_state_missing_or_malformed_yields_null():
    addr = _parse_address({"Street": "1 Main", "State": None})
    assert addr is not None and addr.state is None
    addr2 = _parse_address({"Street": "1 Main", "State": "California"})
    assert addr2 is not None and addr2.state is None


# --- lookup table builders ---


def test_build_lookup_handles_missing_category():
    assert _build_lookup({}, "1000") == {}
    assert _build_lookup({"CategoryData": None}, "1000") == {}
    assert _build_lookup({"CategoryData": {"1000": {"CategoryItems": "no"}}}, "1000") == {}


def test_build_legal_lookup_merges_both_categories():
    payload = {
        "CategoryData": {
            "1107": {"CategoryItems": [{"Value": "999", "Title": "Only In 1107"}]},
            "1113": {"CategoryItems": [{"Value": "104", "Title": "DDM"}]},
        }
    }
    lookup = _build_legal_lookup(payload)
    assert lookup["999"] == "Only In 1107"
    assert lookup["104"] == "DDM"


def test_build_legal_lookup_resolves_conflict_in_favor_of_1113():
    """1113 is the category referenced by the live capture, so it wins on conflict."""
    payload = {
        "CategoryData": {
            "1107": {"CategoryItems": [{"Value": "104", "Title": "Old Label"}]},
            "1113": {"CategoryItems": [{"Value": "104", "Title": "New Label"}]},
        }
    }
    assert _build_legal_lookup(payload)["104"] == "New Label"


# --- HTTP integration: CSRF then GetRelationshipList ---


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
    req = httpx.Request("GET", f"https://healthy.kaiserpermanente.org{RELATIONSHIPS_PATH}")
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


def _csrf_html(token: str) -> str:
    return f'<input name="__RequestVerificationToken" type="hidden" value="{token}" />'


@pytest.mark.asyncio
async def test_fetch_emergency_contacts_sends_csrf_then_post_with_form_body():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, text=_csrf_html("tok-abc")),
        httpx.Response(200, json=_sample_payload()),
    ])
    try:
        contacts = await fetch_emergency_contacts(KaiserRequest(store))
    finally:
        p.stop()

    assert len(contacts) == 2
    assert mock_client.request.await_count == 2

    csrf_call = mock_client.request.await_args_list[0]
    assert csrf_call.args[0] == "GET"
    assert CSRF_PATH in csrf_call.args[1]

    post_call = mock_client.request.await_args_list[1]
    assert post_call.args[0] == "POST"
    assert RELATIONSHIPS_PATH in post_call.args[1]
    headers = post_call.kwargs["headers"]
    assert headers["__RequestVerificationToken"] == "tok-abc"
    assert headers["X-Requested-With"] == "XMLHttpRequest"
    assert headers["Referer"] == "https://healthy.kaiserpermanente.org/mychartcn/AdvancedCarePlanning"
    assert headers["Content-Type"] == "application/x-www-form-urlencoded; charset=UTF-8"
    assert post_call.kwargs["content"] == b"getEOLDocs=true&disableUTF8=true"


@pytest.mark.asyncio
async def test_fetch_emergency_contacts_returns_empty_list_when_no_relationships():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html("tok")),
        httpx.Response(200, json={"Success": "1", "Relationships": [], "CategoryData": {}}),
    ])
    try:
        contacts = await fetch_emergency_contacts(KaiserRequest(store))
    finally:
        p.stop()

    assert contacts == []


@pytest.mark.asyncio
async def test_fetch_emergency_contacts_propagates_http_error():
    """Unlike `fetch_profile`, the standalone fetcher raises on HTTP errors —
    the caller decides whether to swallow."""
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html("tok")),
        httpx.Response(500, text="kaboom"),
    ])
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_emergency_contacts(KaiserRequest(store))
    finally:
        p.stop()


# --- model defaults ---


def test_emergency_contact_defaults_are_safe():
    """A bare-minimum contact (just a name) should validate."""
    c = EmergencyContact(name="x")
    assert c.relationship is None
    assert c.is_active_healthcare_agent is False
    assert c.is_primary_contact is False
    assert c.address is None


def test_contact_address_all_optional():
    a = ContactAddress()
    assert a.street1 is None
