"""Tests for scrapers/implants.py: parser + HTTP integration.

Fixtures use fabricated device names, models, and serials. No PHI.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openkp.scrapers.csrf import CSRF_PATH
from openkp.scrapers.implants import (
    LIST_PATH,
    PAGE_REFERER,
    ImplantsResponse,
    _display_date_to_iso,
    _ordered_ids,
    _parse_implant,
    _parse_implants_response,
    _parse_procedure,
    _str_list,
    _str_or_none,
    fetch_implants,
)


# --- fake data (non-PHI) ---


_FAKE_CSRF = "fake-csrf-token-abc123"


def _csrf_html(token: str = _FAKE_CSRF) -> str:
    return f'<input name="__RequestVerificationToken" type="hidden" value="{token}" />'


def _empty_proc() -> dict:
    return {"isoDate": "", "deviceCount": "", "provider": "", "facility": ""}


def _device(**overrides) -> dict:
    base = {
        "organizationLinks": [],
        "isExternal": False,
        "id": "dev-eye-r",
        "name": "Fake Intraocular Lens X",
        "type": "Ophthalmology",
        "area": "Eye",
        "laterality": "Right",
        "udi": "(01)00000000000000(17)000000(21)SN-EYE-001",
        "sdi": "00000000000000",
        "manufacturer": "ACME OPTICS",
        "serial": "SN-EYE-001",
        "status": "Implanted",
        "model": "FAKE-IOL-1",
        "description": [],
        "comments": [],
        "lot": "",
        "isExplant": False,
        "implantProcedure": {
            "isoDate": "March 5, 2020",
            "deviceCount": "1",
            "provider": "DR FAKE EYE",
            "facility": "Fake Surgery Center",
        },
        "explantProcedure": _empty_proc(),
    }
    base.update(overrides)
    return base


def _sample_payload() -> dict:
    chest = _device(
        id="dev-chest",
        name="Fake Pacemaker Model Z",
        type="Pacemaker",
        area="Chest",
        laterality="Left",
        manufacturer="ACME CARDIAC",
        serial="SN-CHEST-9",
        model="FAKE-PACE",
        udi="",
        sdi="",
        implantProcedure={"isoDate": "January 3, 2024", "deviceCount": "", "provider": "", "facility": ""},
    )
    unknown = _device(
        id="dev-unknown",
        name="Fake Lead",
        type="Cardiac Implant",
        area="",          # ungrouped → lands in the "zzz" sentinel group
        laterality="",
        manufacturer="ACME",
        serial="SN-LEAD-3",
        model="FAKE-LEAD",
        udi="",
        sdi="",
        implantProcedure={"isoDate": "November 19, 2007", "deviceCount": "", "provider": "", "facility": ""},
    )
    eye = _device()
    return {
        "communityActive": False,
        "implantGroupList": [
            {"area": "Chest", "implantIDs": ["dev-chest"]},
            {"area": "Eye", "implantIDs": ["dev-eye-r"]},
            {"area": "zzz", "implantIDs": ["dev-unknown"]},
        ],
        "implantList": {
            "dev-eye-r": eye,
            "dev-chest": chest,
            "dev-unknown": unknown,
        },
    }


# --- helpers ---


def test_str_or_none_strips_and_handles_empty():
    assert _str_or_none("  hi  ") == "hi"
    assert _str_or_none("") is None
    assert _str_or_none(None) is None
    assert _str_or_none("   ") is None


def test_str_list_filters_non_strings_and_blanks():
    assert _str_list(["a", "  b  ", "", "   ", 5, None]) == ["a", "b"]
    assert _str_list("not a list") == []
    assert _str_list(None) == []
    assert _str_list([]) == []


def test_display_date_to_iso():
    assert _display_date_to_iso("January 3, 2024") == "2024-01-03"
    assert _display_date_to_iso("November 19, 2007") == "2007-11-19"
    assert _display_date_to_iso("  March 5, 2020  ") == "2020-03-05"
    # Unparseable / empty → None, never raises
    assert _display_date_to_iso("") is None
    assert _display_date_to_iso(None) is None
    assert _display_date_to_iso("2024-01-03") is None
    assert _display_date_to_iso("garbage") is None


# --- _parse_procedure ---


def test_parse_procedure_full():
    proc = _parse_procedure({
        "isoDate": "March 5, 2020",
        "deviceCount": "1",
        "provider": "DR FAKE EYE",
        "facility": "Fake Surgery Center",
    })
    assert proc is not None
    assert proc.date == "March 5, 2020"
    assert proc.date_iso == "2020-03-05"
    assert proc.provider == "DR FAKE EYE"
    assert proc.facility == "Fake Surgery Center"
    assert proc.device_count == "1"


def test_parse_procedure_all_empty_returns_none():
    assert _parse_procedure(_empty_proc()) is None
    assert _parse_procedure({}) is None
    assert _parse_procedure(None) is None
    assert _parse_procedure("garbage") is None


def test_parse_procedure_partial_date_only():
    proc = _parse_procedure({"isoDate": "January 3, 2024", "deviceCount": "", "provider": "", "facility": ""})
    assert proc is not None
    assert proc.date == "January 3, 2024"
    assert proc.date_iso == "2024-01-03"
    assert proc.provider is None
    assert proc.facility is None
    assert proc.device_count is None


# --- _parse_implant ---


def test_parse_implant_full_field_extraction():
    imp = _parse_implant("dev-eye-r", _device())
    assert imp is not None
    assert imp.id == "dev-eye-r"
    assert imp.name == "Fake Intraocular Lens X"
    assert imp.type == "Ophthalmology"
    assert imp.area == "Eye"
    assert imp.laterality == "Right"
    assert imp.status == "Implanted"
    assert imp.is_explant is False
    assert imp.is_external is False
    assert imp.manufacturer == "ACME OPTICS"
    assert imp.model == "FAKE-IOL-1"
    assert imp.serial == "SN-EYE-001"
    assert imp.udi.startswith("(01)")
    assert imp.sdi == "00000000000000"
    assert imp.comments == []
    assert imp.implanted is not None
    assert imp.implanted.date_iso == "2020-03-05"
    assert imp.explanted is None  # all-empty explant block collapses to None


def test_parse_implant_empty_area_becomes_none():
    imp = _parse_implant("dev-unknown", _device(id="dev-unknown", area="", laterality=""))
    assert imp is not None
    assert imp.area is None
    assert imp.laterality is None


def test_parse_implant_falls_back_to_map_key_when_no_inner_id():
    entry = _device()
    entry.pop("id")
    imp = _parse_implant("map-key-1", entry)
    assert imp is not None
    assert imp.id == "map-key-1"


def test_parse_implant_no_id_anywhere_returns_none():
    entry = _device()
    entry.pop("id")
    assert _parse_implant(None, entry) is None


def test_parse_implant_non_dict_returns_none():
    assert _parse_implant("dev-1", None) is None
    assert _parse_implant("dev-1", "garbage") is None


def test_parse_implant_explanted_device():
    entry = _device(
        id="dev-old",
        status="Explanted",
        isExplant=True,
        explantProcedure={"isoDate": "June 1, 2022", "deviceCount": "1", "provider": "DR FAKE", "facility": ""},
    )
    imp = _parse_implant("dev-old", entry)
    assert imp is not None
    assert imp.is_explant is True
    assert imp.status == "Explanted"
    assert imp.explanted is not None
    assert imp.explanted.date_iso == "2022-06-01"


# --- _ordered_ids ---


def test_ordered_ids_follows_group_order():
    payload = _sample_payload()
    ordered = _ordered_ids(payload["implantGroupList"], payload["implantList"])
    assert ordered == ["dev-chest", "dev-eye-r", "dev-unknown"]


def test_ordered_ids_appends_ungrouped_devices():
    implant_map = {"a": {}, "b": {}, "orphan": {}}
    group_list = [{"area": "X", "implantIDs": ["b", "a"]}]
    ordered = _ordered_ids(group_list, implant_map)
    assert ordered[:2] == ["b", "a"]
    assert "orphan" in ordered
    assert len(ordered) == 3


def test_ordered_ids_handles_malformed_groups():
    implant_map = {"a": {}}
    # group list is junk; we still return the map's devices
    assert _ordered_ids("not a list", implant_map) == ["a"]
    assert _ordered_ids([None, {"implantIDs": "nope"}, {}], implant_map) == ["a"]


def test_ordered_ids_skips_ids_not_in_map():
    implant_map = {"a": {}}
    group_list = [{"area": "X", "implantIDs": ["a", "ghost"]}]
    assert _ordered_ids(group_list, implant_map) == ["a"]


# --- _parse_implants_response ---


def test_parse_implants_response_happy_path_and_ordering():
    response = _parse_implants_response(_sample_payload())
    assert response.total_count == 3
    ids = [i.id for i in response.implants]
    assert ids == ["dev-chest", "dev-eye-r", "dev-unknown"]
    assert response.implants[0].type == "Pacemaker"
    assert response.implants[1].laterality == "Right"
    assert response.implants[2].area is None


def test_parse_implants_response_empty():
    assert _parse_implants_response({"implantList": {}, "implantGroupList": []}).total_count == 0


def test_parse_implants_response_malformed():
    assert _parse_implants_response({}).total_count == 0
    assert _parse_implants_response({"implantList": "nope"}).total_count == 0
    assert _parse_implants_response(None).total_count == 0
    assert _parse_implants_response("garbage").total_count == 0


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
async def test_fetch_implants_happy_path():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_sample_payload()),
    ])
    try:
        response = await fetch_implants(KaiserRequest(store))
    finally:
        p.stop()

    assert isinstance(response, ImplantsResponse)
    assert response.total_count == 3
    assert response.implants[0].name == "Fake Pacemaker Model Z"

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
    assert list_call.kwargs["json"] == {}


@pytest.mark.asyncio
async def test_fetch_implants_empty_list():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json={"implantList": {}, "implantGroupList": []}),
    ])
    try:
        response = await fetch_implants(KaiserRequest(store))
    finally:
        p.stop()

    assert response.total_count == 0
    assert response.implants == []
