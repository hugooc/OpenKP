"""Tests for scrapers/medications.py — list_medications + helpers.

Fixtures use fabricated patient, prescriber, and medication names. No PHI.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openkp.scrapers.medications import (
    BFF_CLIENT_ID,
    BFF_ORIGIN,
    BFF_REGION_SENTINEL,
    FILTER_ALL,
    FILTER_FILLABLE,
    RX_DETAILS_PATH,
    RX_DETAILS_URL,
    Medication,
    MedicationFillOption,
    MedicationsResponse,
    _absolute_drug_info_url,
    _bff_headers,
    _fetch_user_guid,
    _has_indicator,
    _parse_date,
    _parse_fill_options,
    _parse_medications_response,
    _parse_one_med,
    _str_to_bool,
    _str_to_int,
    fetch_medications,
)
from openkp.scrapers.profile import USER_PATH

_FAKE_GUID = "9999999"


# --- fabricated payloads (no PHI) ---


def _user_payload() -> dict:
    """Minimal /mycare/v1.0/user response carrying just the GUID."""
    return {
        "UserAccountData": {
            "userIdentityInfo": {"guid": _FAKE_GUID},
        }
    }


def _med_row(
    *,
    rxnbr: str = "111111111111",
    name: str = "FAKEDRUG 10 MG TAB FAKE",
    brand: str = "FAKEBRAND",
    prescribed_on: str = "01/15/2026",
    last_refill: str = "02/01/2026",
    refills_remaining: str = "3",
    next_fill_date: str | None = "05/01/2026",
    rar_status: bool = False,
    rar_code: str = "",
    afc_extras: dict | None = None,
    extras: dict | None = None,
) -> dict:
    """Build a fabricated fillable / nonFillable entry."""
    afc = {
        "rxnbr": rxnbr,
        "drugnm": name,
        "dispenseddayssupply": 90,
        "copay": 12.34,
        "lastdispensedndc": "00000111111",
        "mrnrgn": "NCA",
        "deacode": "6",
        "nextFillEligibleDate": "04/15/2026",
        "prn": False,
        "compound": False,
        "sigText": "Take 1 tablet by mouth daily",
    }
    if afc_extras:
        afc.update(afc_extras)
    row = {
        "medicineName": name,
        "commonBrandName": brand,
        "consumerInstructions": "Take 1 tablet by mouth daily",
        "consumerName": "DR. FAKE PRESCRIBER MD",
        "prescribedOn": prescribed_on,
        "lastRefillDate": last_refill,
        "lastSoldDate": last_refill,
        "refillsRemaining": refills_remaining,
        "mailable": "true",
        "firstFill": False,
        "isNewPrescription": False,
        "refillEligible": True,
        "autoRefill": False,
        "autoRefillEligible": True,
        "rarCodeKey": rar_code,
        "rarStatus": rar_status,
        "drugEncyclopediaLink": "/health-wellness/drug-encyclopedia/drug.fake.123",
        "afcInfo": afc,
        "fillOptions": [
            {
                "deliveryMethod": "L",
                "daysSupply": 90,
                "quantity": 90,
                "ucCharge": 50.0,
                "planPay": 30.0,
                "estimatedCopay": 0,
                "copaystts": "000",
                "copaysttsdtl": "Approved",
            },
            {
                "deliveryMethod": "M",
                "daysSupply": 90,
                "quantity": 90,
                "ucCharge": 50.0,
                "planPay": 25.0,
                "estimatedCopay": 0,
                "copaystts": "000",
                "copaysttsdtl": "Approved",
            },
        ],
        "RxCustomIndicators": [{"key": "PRN", "value": "false"}],
    }
    if next_fill_date is not None:
        row["nextFillDate"] = next_fill_date
    if extras:
        row.update(extras)
    return row


def _rx_details_payload() -> dict:
    """Fabricated rxDetails response: 3 fillable, 1 nonFillable.

    Exercises the format quirks from the real capture:
      - Mixed date formats: one Rx uses ISO recentRxDate, others use US.
      - lastRefillDate = "N/A" on a never-filled Rx.
      - mailable as a string.
      - refillsRemaining as a string.
      - Dabigatran-style expired-refills row (in fillable[] despite refillsRemaining=0).
    """
    chlorthalidone = _med_row(
        rxnbr="225636148952",
        name="FAKE CHLORTHALID 25 MG TAB",
        brand="FAKE HYGROTON",
        prescribed_on="01/26/2026",
        last_refill="N/A",
        refills_remaining="2",
        next_fill_date=None,
        extras={
            # Real Kaiser quirk: ISO date in an otherwise-US response.
            "recentRxDate": "2026-01-26",
            "firstFill": True,
        },
        afc_extras={"rxnbr": "225636148952", "lastdispensedndc": "00000111111"},
    )
    metoprolol = _med_row(
        rxnbr="225634381051",
        name="Fake Metoprol Suc 200 Mg",
        brand="FAKE TOPROL XL",
        prescribed_on="10/31/2025",
        last_refill="01/26/2026",
        refills_remaining="2",
        next_fill_date="04/15/2026",
        extras={"recentRxDate": "01/26/2026"},
    )
    dabigatran = _med_row(
        rxnbr="244704798361",
        name="Fake Dabigatran 150 Mg Cap",
        brand="FAKE PRADAXA",
        prescribed_on="10/13/2025",
        last_refill="10/31/2025",
        refills_remaining="0",
        next_fill_date=None,
        rar_status=True,
        rar_code="Rx_NoMoreRefills",
        extras={"refillEligible": False, "autoRefillEligible": False},
    )
    verapamil_non_fillable = _med_row(
        rxnbr="225631608091",
        name="FAKE VERAPAMIL ER 240 MG",
        brand="FAKE CALAN SR",
        prescribed_on="05/06/2025",
        last_refill="03/10/2026",
        refills_remaining="1",
        next_fill_date=None,
        extras={"mailable": "false"},
    )
    return {
        "executionContext": {"statusCode": "000", "statusDetails": "Success"},
        "member": {"guid": _FAKE_GUID, "firstName": "FAKE", "lastName": "PERSON"},
        "prescriptions": {
            "rxRefillResponse": True,
            "lastDispensedNDCs": ["00000111111"],
            "recentRefillableCount": 2,
            "totalRefillableCount": 3,
            "recentNonRefillableCount": 1,
            "totalNonRefillableCount": 1,
            "fillable": [chlorthalidone, metoprolol, dabigatran],
            "nonFillable": [verapamil_non_fillable],
        },
    }


# --- httpx patch helpers (modeled on test_labs.py) ---


def _make_store() -> MagicMock:
    from openkp.scrapers.auth import KaiserSession

    store = MagicMock()
    store.get_session = AsyncMock(
        return_value=KaiserSession(
            cookies=[{"name": "k", "value": "v", "domain": ".kaiserpermanente.org", "path": "/"}],
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


# --- _parse_date ---


def test_parse_date_us_format():
    assert _parse_date("01/26/2026") == "2026-01-26"


def test_parse_date_iso_format():
    assert _parse_date("2026-01-26") == "2026-01-26"


def test_parse_date_strips_trailing_z():
    assert _parse_date("1970-01-01Z") == "1970-01-01"


def test_parse_date_na_to_none():
    assert _parse_date("N/A") is None
    assert _parse_date("n/a") is None
    assert _parse_date(" N/A ") is None


def test_parse_date_empty_to_none():
    assert _parse_date("") is None
    assert _parse_date("   ") is None


def test_parse_date_none_in_none_out():
    assert _parse_date(None) is None


def test_parse_date_garbage_to_none():
    assert _parse_date("not a date") is None
    assert _parse_date("13/45/9999") is None  # invalid month


# --- _str_to_bool ---


def test_str_to_bool_true_string():
    assert _str_to_bool("true") is True
    assert _str_to_bool("True") is True
    assert _str_to_bool(" TRUE ") is True


def test_str_to_bool_false_string():
    assert _str_to_bool("false") is False


def test_str_to_bool_real_bool():
    assert _str_to_bool(True) is True
    assert _str_to_bool(False) is False


def test_str_to_bool_none_and_garbage_are_false():
    assert _str_to_bool(None) is False
    assert _str_to_bool("yes") is False
    assert _str_to_bool(1) is False


# --- _str_to_int ---


def test_str_to_int_numeric_string():
    assert _str_to_int("2") == 2
    assert _str_to_int("0") == 0
    assert _str_to_int(" 5 ") == 5


def test_str_to_int_real_int():
    assert _str_to_int(7) == 7


def test_str_to_int_none_and_garbage():
    assert _str_to_int(None) is None
    assert _str_to_int("two") is None
    assert _str_to_int("") is None


# --- _has_indicator ---


def test_has_indicator_finds_true():
    row = {"RxCustomIndicators": [{"key": "PRN", "value": "true"}]}
    assert _has_indicator(row, "PRN") is True


def test_has_indicator_returns_false_when_value_false():
    row = {"RxCustomIndicators": [{"key": "PRN", "value": "false"}]}
    assert _has_indicator(row, "PRN") is False


def test_has_indicator_missing_key():
    row = {"RxCustomIndicators": [{"key": "OTHER", "value": "true"}]}
    assert _has_indicator(row, "PRN") is False


def test_has_indicator_no_indicators_field():
    assert _has_indicator({}, "PRN") is False


# --- _absolute_drug_info_url ---


def test_drug_info_url_relative_gets_origin():
    url = _absolute_drug_info_url("/health-wellness/drug-encyclopedia/drug.fake.123")
    assert url == f"{BFF_ORIGIN}/health-wellness/drug-encyclopedia/drug.fake.123"


def test_drug_info_url_absolute_passes_through():
    abs_url = "https://elsewhere.example/foo"
    assert _absolute_drug_info_url(abs_url) == abs_url


def test_drug_info_url_none():
    assert _absolute_drug_info_url(None) is None
    assert _absolute_drug_info_url("") is None


# --- _parse_fill_options ---


def test_parse_fill_options_handles_pair():
    options = _parse_fill_options([
        {
            "deliveryMethod": "L",
            "daysSupply": 90,
            "quantity": 90,
            "ucCharge": 50.0,
            "planPay": 30.0,
            "estimatedCopay": 0,
            "copaystts": "000",
            "copaysttsdtl": "Approved",
        },
        {
            "deliveryMethod": "M",
            "daysSupply": 90,
            "quantity": 90,
            "ucCharge": 50.0,
            "planPay": 25.0,
            "estimatedCopay": 0,
            "copaystts": "000",
            "copaysttsdtl": "Approved",
        },
    ])
    assert len(options) == 2
    assert options[0].delivery_method == "L"
    assert options[1].delivery_method == "M"
    assert options[0].plan_pay == 30.0
    assert options[1].plan_pay == 25.0


def test_parse_fill_options_empty_input():
    assert _parse_fill_options(None) == []
    assert _parse_fill_options([]) == []


# --- _parse_one_med ---


def test_parse_one_med_full_row():
    row = _med_row()
    med = _parse_one_med(row, is_currently_orderable=True)
    assert med is not None
    assert med.rx_number == "111111111111"
    assert med.name == "FAKEDRUG 10 MG TAB FAKE"
    assert med.brand_name == "FAKEBRAND"
    assert med.instructions == "Take 1 tablet by mouth daily"
    assert med.prescriber == "DR. FAKE PRESCRIBER MD"
    assert med.prescribed_on == "2026-01-15"
    assert med.last_refill_date == "2026-02-01"
    assert med.next_fill_date == "2026-05-01"
    assert med.next_fill_eligible_date == "2026-04-15"
    assert med.refills_remaining == 3
    assert med.days_supply == 90
    assert med.copay == 12.34
    assert med.is_mailable is True
    assert med.is_refill_eligible is True
    assert med.is_currently_orderable is True
    assert med.refill_blocked_reason is None
    assert med.region == "NCA"
    assert med.dea_schedule == "6"
    assert len(med.fill_options) == 2


def test_parse_one_med_handles_na_last_refill():
    row = _med_row(last_refill="N/A")
    med = _parse_one_med(row, is_currently_orderable=True)
    assert med is not None
    assert med.last_refill_date is None


def test_parse_one_med_iso_recentrxdate_does_not_break_other_dates():
    """The mixed-date-format quirk: one Rx ships ISO recentRxDate alongside US prescribedOn."""
    row = _med_row(prescribed_on="01/26/2026", extras={"recentRxDate": "2026-01-26"})
    med = _parse_one_med(row, is_currently_orderable=True)
    assert med is not None
    assert med.prescribed_on == "2026-01-26"


def test_parse_one_med_blocked_reason_when_rar_status_true():
    row = _med_row(rar_status=True, rar_code="Rx_NoMoreRefills")
    med = _parse_one_med(row, is_currently_orderable=True)
    assert med is not None
    assert med.refill_blocked_reason == "Rx_NoMoreRefills"


def test_parse_one_med_blocked_reason_ignored_when_rar_status_false():
    row = _med_row(rar_status=False, rar_code="Rx_NoMoreRefills")
    med = _parse_one_med(row, is_currently_orderable=True)
    assert med is not None
    assert med.refill_blocked_reason is None


def test_parse_one_med_orderable_flag_from_bucket():
    row = _med_row()
    med_orderable = _parse_one_med(row, is_currently_orderable=True)
    med_blocked = _parse_one_med(row, is_currently_orderable=False)
    assert med_orderable is not None and med_blocked is not None
    assert med_orderable.is_currently_orderable is True
    assert med_blocked.is_currently_orderable is False


def test_parse_one_med_returns_none_for_non_dict():
    assert _parse_one_med("not a dict", is_currently_orderable=True) is None


def test_parse_one_med_falls_back_to_sigtext_when_consumer_instructions_missing():
    row = _med_row()
    row.pop("consumerInstructions")
    med = _parse_one_med(row, is_currently_orderable=True)
    assert med is not None
    assert med.instructions == "Take 1 tablet by mouth daily"


def test_parse_one_med_prn_from_indicator():
    row = _med_row()
    row["RxCustomIndicators"] = [{"key": "PRN", "value": "true"}]
    med = _parse_one_med(row, is_currently_orderable=True)
    assert med is not None
    assert med.is_as_needed is True


# --- _parse_medications_response ---


def test_parse_response_happy_path():
    payload = _rx_details_payload()
    result = _parse_medications_response(payload)
    assert isinstance(result, MedicationsResponse)
    assert result.status_code == "000"
    assert result.status_details == "Success"
    assert result.total_count == 4  # 3 fillable + 1 nonFillable
    assert result.refillable_count == 3
    assert result.recent_refillable_count == 2

    # Order: fillable bucket first, nonFillable second.
    assert [m.brand_name for m in result.medications] == [
        "FAKE HYGROTON",
        "FAKE TOPROL XL",
        "FAKE PRADAXA",
        "FAKE CALAN SR",
    ]
    # Bucket assignment translates to is_currently_orderable.
    assert [m.is_currently_orderable for m in result.medications] == [True, True, True, False]


def test_parse_response_dabigatran_keeps_blocked_reason():
    payload = _rx_details_payload()
    result = _parse_medications_response(payload)
    dabigatran = next(m for m in result.medications if m.brand_name == "FAKE PRADAXA")
    assert dabigatran.refills_remaining == 0
    assert dabigatran.refill_blocked_reason == "Rx_NoMoreRefills"
    assert dabigatran.is_refill_eligible is False


def test_parse_response_chlorthalidone_handles_iso_and_na():
    """First fillable Rx exercises the ISO-date and N/A quirks."""
    payload = _rx_details_payload()
    result = _parse_medications_response(payload)
    chlorth = next(m for m in result.medications if m.brand_name == "FAKE HYGROTON")
    assert chlorth.last_refill_date is None  # "N/A" mapped to None
    assert chlorth.is_first_fill is True


def test_parse_response_verapamil_in_nonfillable_bucket():
    payload = _rx_details_payload()
    result = _parse_medications_response(payload)
    verap = next(m for m in result.medications if m.brand_name == "FAKE CALAN SR")
    assert verap.is_currently_orderable is False
    assert verap.is_mailable is False


def test_parse_response_non_success_status_returns_empty_meds():
    payload = {
        "executionContext": {"statusCode": "9999", "statusDetails": "Backend error"},
        "prescriptions": {"fillable": [], "nonFillable": []},
    }
    result = _parse_medications_response(payload)
    assert result.medications == []
    assert result.status_code == "9999"
    assert result.status_details == "Backend error"


def test_parse_response_empty_payload():
    assert _parse_medications_response({}).medications == []
    assert _parse_medications_response(None).medications == []  # type: ignore[arg-type]
    assert _parse_medications_response([]).medications == []  # type: ignore[arg-type]


def test_parse_response_missing_prescriptions_key():
    result = _parse_medications_response({"executionContext": {"statusCode": "000"}})
    assert result.medications == []
    assert result.status_code == "000"


# --- _bff_headers ---


def test_bff_headers_contains_required_fields():
    h = _bff_headers("12345", "all")
    assert h["X-IBM-client-Id"] == BFF_CLIENT_ID
    assert h["x-guid"] == "12345"
    assert h["X-region"] == BFF_REGION_SENTINEL
    assert h["Origin"] == BFF_ORIGIN
    assert h["x-prescriptionsfilter"] == "all"
    # Curiosities sent verbatim.
    assert h["X-KPSessionID"] == "undefined"
    assert h["X-disablecache"] == "false"


def test_bff_headers_filter_value_propagates():
    assert _bff_headers("g", "fillable")["x-prescriptionsfilter"] == "fillable"


# --- _fetch_user_guid ---


@pytest.mark.asyncio
async def test_fetch_user_guid_extracts_value():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([httpx.Response(200, json=_user_payload())])
    try:
        guid = await _fetch_user_guid(KaiserRequest(store))
    finally:
        p.stop()
    assert guid == _FAKE_GUID
    # Verify we called /mycare/v1.0/user with the inclusion-path header
    call = mock_client.request.await_args_list[0]
    assert call.args[0] == "GET"
    assert USER_PATH in call.args[1]
    headers = call.kwargs["headers"]
    assert headers["X-apiKey"]
    # Inclusion path includes the GUID node (it's part of the broader path
    # set we share with profile.py — see comment on _GUID_INCLUSION_PATHS).
    assert "userIdentityInfo.guid" in headers["X-inclusionJsonPath"]


@pytest.mark.asyncio
async def test_fetch_user_guid_coerces_int_to_string():
    """Kaiser sometimes returns the GUID as a JSON number, not a string.

    Confirmed in production via the diagnostic ValueError: the scraper kept
    failing with `isinstance(str)` even though `guid` was a key in
    `userIdentityInfo`. The value type was int. Match profile.py's behavior
    of coercing any value to string.
    """
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    int_guid_payload = {
        "UserAccountData": {
            "userIdentityInfo": {"guid": 1234567},  # int, not str
        }
    }
    _, p = _patch_http([httpx.Response(200, json=int_guid_payload)])
    try:
        guid = await _fetch_user_guid(KaiserRequest(store))
    finally:
        p.stop()
    assert guid == "1234567"


@pytest.mark.asyncio
async def test_fetch_user_guid_falls_back_to_area_of_care():
    """When userIdentityInfo.guid is missing, fall back to areaOfCareInfos[0].guid."""
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    fallback_payload = {
        "UserAccountData": {
            "userIdentityInfo": {},  # canonical path absent
            "ebizAccountsWithPersonInfos": {
                "areaOfCareInfos": [{"guid": _FAKE_GUID, "mrn": "0000000"}],
            },
        }
    }
    _, p = _patch_http([httpx.Response(200, json=fallback_payload)])
    try:
        guid = await _fetch_user_guid(KaiserRequest(store))
    finally:
        p.stop()
    assert guid == _FAKE_GUID


@pytest.mark.asyncio
async def test_fetch_user_guid_prefers_identity_over_area_of_care():
    """Canonical path wins when both are populated."""
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    payload = {
        "UserAccountData": {
            "userIdentityInfo": {"guid": "from-identity"},
            "ebizAccountsWithPersonInfos": {
                "areaOfCareInfos": [{"guid": "from-area"}],
            },
        }
    }
    _, p = _patch_http([httpx.Response(200, json=payload)])
    try:
        guid = await _fetch_user_guid(KaiserRequest(store))
    finally:
        p.stop()
    assert guid == "from-identity"


@pytest.mark.asyncio
async def test_fetch_user_guid_raises_when_both_paths_missing():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([httpx.Response(200, json={"UserAccountData": {}})])
    try:
        with pytest.raises(ValueError, match="user GUID"):
            await _fetch_user_guid(KaiserRequest(store))
    finally:
        p.stop()


@pytest.mark.asyncio
async def test_fetch_user_guid_raises_on_empty_response():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([httpx.Response(200, json={})])
    try:
        with pytest.raises(ValueError):
            await _fetch_user_guid(KaiserRequest(store))
    finally:
        p.stop()


# --- fetch_medications (integration) ---


@pytest.mark.asyncio
async def test_fetch_medications_happy_path():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, json=_user_payload()),
        httpx.Response(200, json=_rx_details_payload()),
    ])
    try:
        result = await fetch_medications(KaiserRequest(store), filter=FILTER_ALL)
    finally:
        p.stop()

    assert result.total_count == 4
    assert result.refillable_count == 3
    assert result.status_code == "000"

    # Two HTTP calls: GUID fetch then BFF.
    assert mock_client.request.await_count == 2
    bff_call = mock_client.request.await_args_list[1]
    assert bff_call.args[0] == "GET"
    assert RX_DETAILS_PATH in bff_call.args[1]
    assert bff_call.args[1].startswith("https://apims.kaiserpermanente.org")

    # Headers carry the user GUID.
    bff_headers = bff_call.kwargs["headers"]
    assert bff_headers["x-guid"] == _FAKE_GUID
    assert bff_headers["X-IBM-client-Id"] == BFF_CLIENT_ID

    # prescriptions_filter passed via query param.
    assert bff_call.kwargs["params"] == {"prescriptions_filter": "all"}


@pytest.mark.asyncio
async def test_fetch_medications_fillable_filter_propagates():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, json=_user_payload()),
        httpx.Response(200, json=_rx_details_payload()),
    ])
    try:
        await fetch_medications(KaiserRequest(store), filter=FILTER_FILLABLE)
    finally:
        p.stop()
    bff_call = mock_client.request.await_args_list[1]
    assert bff_call.kwargs["params"] == {"prescriptions_filter": "fillable"}
    assert bff_call.kwargs["headers"]["x-prescriptionsfilter"] == "fillable"


@pytest.mark.asyncio
async def test_fetch_medications_invalid_filter_raises():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    with pytest.raises(ValueError, match="filter must be one of"):
        await fetch_medications(KaiserRequest(store), filter="weird")


# --- model serialization sanity ---


def test_models_round_trip_through_model_dump():
    fill_opt = MedicationFillOption(delivery_method="M", days_supply=90)
    med = Medication(rx_number="123", name="Test", fill_options=[fill_opt])
    response = MedicationsResponse(medications=[med], total_count=1)
    dumped = response.model_dump()
    assert dumped["total_count"] == 1
    assert dumped["medications"][0]["rx_number"] == "123"
    assert dumped["medications"][0]["fill_options"][0]["delivery_method"] == "M"


def test_rx_details_url_constant_matches_path():
    assert RX_DETAILS_URL.endswith(RX_DETAILS_PATH)
    assert RX_DETAILS_URL.startswith("https://apims.kaiserpermanente.org")
