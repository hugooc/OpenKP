"""Tests for scrapers/profile.py: parser resilience + end-to-end fetch path.

Fixture is modeled on a real /mycare/v1.0/user response (PHI-free values).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openkp.scrapers.profile import (
    CARE_TEAM_PATH,
    CSRF_PATH,
    USER_PATH,
    Address,
    InsurancePlan,
    Phone,
    Profile,
    _clean_date,
    _fetch_csrf_token,
    _fetch_pcp,
    _format_phone,
    _parse_addresses,
    _parse_pcp,
    _parse_phones,
    _parse_plans,
    _parse_profile,
    _parse_region,
    fetch_profile,
)


# --- sample payload ---


def _sample_payload() -> dict:
    return {
        "UserAccountData": {
            "ebizAccountsWithPersonInfos": {
                "ebizAccountInfos": [
                    {
                        "eBizAccountRoleInfo": [
                            {"primaryRegion": "NCA", "accountRoleRegion": "NCA"}
                        ]
                    }
                ],
                "nameDetails": {
                    "surname": "Sample",
                    "firstName": "Jane",
                    "middleName": "Q",
                },
                "contactInfo": {
                    "addressInfos": [
                        {
                            "type": "MAILING",
                            "label": "Mailing",
                            "street1": "123 Example St",
                            "street2": None,
                            "city": "Oakland",
                            "state": "CA",
                            "postalCode": 90210,
                            "preferredIn": True,
                        },
                        {
                            "type": "HOME",
                            "label": "Home",
                            "street1": "456 Example Ave",
                            "street2": "Apt 2",
                            "city": "Oakland",
                            "state": "CA",
                            "postalCode": 902100000,
                            "preferredIn": False,
                        },
                    ],
                    "phoneInfos": [
                        {
                            "type": "RESIDENCE",
                            "label": "Home phone",
                            "primaryIndicator": True,
                            "phoneNumber": {
                                "area": 510,
                                "exchange": 555,
                                "subscriber": 1234,
                                "country": None,
                                "extension": None,
                            },
                        },
                        {
                            "type": "MOBILE",
                            "label": "Mobile",
                            "primaryIndicator": False,
                            "phoneNumber": {
                                "area": 415,
                                "exchange": 555,
                                "subscriber": 6789,
                                "country": None,
                                "extension": "42",
                            },
                        },
                    ],
                    "emailAddresseInfos": [],
                },
                "dateOfBirth": "1966-07-04",
                "age": 59.0,
                "gender": "M",
                "areaOfCareInfos": [
                    {"guid": 1234567, "mrn": 14776978, "areaOfCare": "NCA", "role": "PRI"},
                ],
                "membershipAccountInfo": {
                    "accountId": 14776978,
                    "region": "NCA",
                    "planInfos": [
                        {
                            "purchaserName": "Sample Employer Group",
                            "consumerPlanType": "HMO-BASE",
                            "coverageStartDate": "2020-01-01",
                            "coverageEndDate": "2030-12-31",
                        }
                    ],
                },
            },
            "userIdentityInfo": {
                "guid": 1234567,
                "email": "jane@example.com",
                "preferredGivenName": "Jane",
            },
        }
    }


# --- _parse_profile happy path ---


def test_parse_happy_path_demographics():
    p = _parse_profile(_sample_payload())
    assert p.first_name == "Jane"
    assert p.middle_name == "Q"
    assert p.last_name == "Sample"
    assert p.preferred_name == "Jane"
    assert p.date_of_birth == "1966-07-04"
    assert p.age == 59
    assert p.gender == "M"
    assert p.email == "jane@example.com"
    assert p.mrn == "14776978"
    assert p.guid == "1234567"
    assert p.region == "NCA"


def test_parse_happy_path_addresses():
    p = _parse_profile(_sample_payload())
    assert len(p.addresses) == 2
    primary = p.addresses[0]
    assert primary.type == "MAILING"
    assert primary.street1 == "123 Example St"
    assert primary.city == "Oakland"
    assert primary.state == "CA"
    assert primary.postal_code == "90210"
    assert primary.is_primary is True
    secondary = p.addresses[1]
    assert secondary.street2 == "Apt 2"
    assert secondary.is_primary is False


def test_parse_happy_path_phones():
    p = _parse_profile(_sample_payload())
    assert len(p.phones) == 2
    assert p.phones[0].number == "510-555-1234"
    assert p.phones[0].is_primary is True
    assert p.phones[1].number == "415-555-6789 x42"
    assert p.phones[1].is_primary is False


def test_parse_happy_path_insurance():
    p = _parse_profile(_sample_payload())
    assert len(p.insurance_plans) == 1
    plan = p.insurance_plans[0]
    assert plan.purchaser_name == "Sample Employer Group"
    assert plan.plan_type == "HMO-BASE"
    assert plan.coverage_start == "2020-01-01"
    assert plan.coverage_end == "2030-12-31"


def test_parse_placeholders_are_empty():
    p = _parse_profile(_sample_payload())
    assert p.pcp is None
    assert p.emergency_contacts == []


# --- _parse_profile resilience ---


def test_parse_missing_user_account_data_returns_empty_profile():
    assert _parse_profile({}) == Profile()
    assert _parse_profile({"UserAccountData": None}) == Profile()
    assert _parse_profile({"UserAccountData": "unexpected"}) == Profile()


def test_parse_handles_mrn_fallback_to_account_id():
    payload = _sample_payload()
    # Remove areaOfCareInfos; MRN should fall back to membershipAccountInfo.accountId
    payload["UserAccountData"]["ebizAccountsWithPersonInfos"]["areaOfCareInfos"] = []
    p = _parse_profile(payload)
    assert p.mrn == "14776978"


def test_parse_handles_guid_fallback_to_area_of_care():
    payload = _sample_payload()
    payload["UserAccountData"]["userIdentityInfo"].pop("guid")
    p = _parse_profile(payload)
    assert p.guid == "1234567"


def test_parse_tolerates_missing_nested_objects():
    payload = {"UserAccountData": {"ebizAccountsWithPersonInfos": {}, "userIdentityInfo": {}}}
    p = _parse_profile(payload)
    assert p.first_name is None
    assert p.addresses == []
    assert p.phones == []
    assert p.insurance_plans == []


def test_parse_age_float_becomes_int():
    payload = _sample_payload()
    payload["UserAccountData"]["ebizAccountsWithPersonInfos"]["age"] = 59.7
    assert _parse_profile(payload).age == 59


def test_parse_age_garbage_returns_none():
    payload = _sample_payload()
    payload["UserAccountData"]["ebizAccountsWithPersonInfos"]["age"] = "not a number"
    assert _parse_profile(payload).age is None


# --- helpers ---


def test_format_phone_handles_ints_and_strings():
    assert _format_phone({"area": 510, "exchange": 555, "subscriber": 1234}) == "510-555-1234"
    assert _format_phone({"area": "510", "exchange": "555", "subscriber": "1234"}) == "510-555-1234"


def test_format_phone_returns_none_for_missing_parts():
    assert _format_phone({"area": 510}) is None
    assert _format_phone(None) is None
    assert _format_phone("not a dict") is None


def test_format_phone_includes_extension():
    assert (
        _format_phone({"area": 510, "exchange": 555, "subscriber": 1234, "extension": "42"})
        == "510-555-1234 x42"
    )


def test_parse_addresses_skips_non_dicts():
    assert _parse_addresses([{"city": "Oakland"}, "bogus", None]) == [
        Address(city="Oakland", is_primary=False)
    ]


def test_parse_addresses_non_list_input():
    assert _parse_addresses(None) == []
    assert _parse_addresses({}) == []


def test_parse_phones_skips_non_dicts():
    phones = _parse_phones(
        [{"type": "MOBILE", "phoneNumber": {"area": 1, "exchange": 2, "subscriber": 3}}, "x"]
    )
    assert phones == [Phone(type="MOBILE", number="1-2-3", is_primary=False)]


def test_parse_plans_skips_non_dicts():
    plans = _parse_plans([{"purchaserName": "Acme"}, None, 42])
    assert plans == [InsurancePlan(purchaser_name="Acme")]


# --- _parse_region (fallback + bad-value sanitization) ---


def test_region_prefers_primary_region():
    ebiz = {
        "ebizAccountInfos": [
            {"eBizAccountRoleInfo": [{"primaryRegion": "NCA", "accountRoleRegion": "SCA"}]}
        ],
        "membershipAccountInfo": {"region": "MRN"},
    }
    assert _parse_region(ebiz) == "NCA"


def test_region_falls_back_to_account_role_region():
    ebiz = {
        "ebizAccountInfos": [{"eBizAccountRoleInfo": [{"accountRoleRegion": "NCA"}]}],
        "membershipAccountInfo": {"region": "MRN"},
    }
    assert _parse_region(ebiz) == "NCA"


def test_region_falls_back_to_membership_when_role_missing():
    ebiz = {"membershipAccountInfo": {"region": "NCA"}}
    assert _parse_region(ebiz) == "NCA"


def test_region_rejects_bogus_mrn_value():
    ebiz = {"membershipAccountInfo": {"region": "MRN"}}
    assert _parse_region(ebiz) is None


def test_region_rejects_bogus_mrn_in_primary_region():
    """Live Kaiser sometimes returns "MRN" in primaryRegion too, not just membership.region."""
    ebiz = {
        "ebizAccountInfos": [
            {"eBizAccountRoleInfo": [{"primaryRegion": "MRN", "accountRoleRegion": "NCA"}]}
        ],
        "membershipAccountInfo": {"region": "MRN"},
    }
    # Should skip the "MRN" at primaryRegion and fall through to accountRoleRegion
    assert _parse_region(ebiz) == "NCA"


def test_region_returns_none_when_all_sources_are_bogus():
    """Observed live: every region field can hold "MRN". Prefer null over garbage."""
    ebiz = {
        "ebizAccountInfos": [
            {"eBizAccountRoleInfo": [{"primaryRegion": "MRN", "accountRoleRegion": "MRN"}]}
        ],
        "membershipAccountInfo": {"region": "MRN"},
    }
    assert _parse_region(ebiz) is None


def test_region_returns_none_when_nothing_found():
    assert _parse_region({}) is None
    assert _parse_region({"ebizAccountInfos": "not a list"}) is None


# --- _clean_date (Z-strip + sentinel handling) ---


def test_clean_date_strips_trailing_z():
    assert _clean_date("1970-01-01Z", allow_sentinel=False) == "1970-01-01"
    assert _clean_date("1970-01-01z", allow_sentinel=False) == "1970-01-01"


def test_clean_date_leaves_normal_dates_alone():
    assert _clean_date("2020-01-01", allow_sentinel=True) == "2020-01-01"


def test_clean_date_maps_sentinel_year_to_none_when_allowed():
    assert _clean_date("4000-12-31Z", allow_sentinel=True) is None
    assert _clean_date("2200-01-01", allow_sentinel=True) is None
    # threshold boundary — 2199 still passes through
    assert _clean_date("2199-12-31", allow_sentinel=True) == "2199-12-31"


def test_clean_date_does_not_apply_sentinel_when_disallowed():
    # DOBs should never be sentinel-mapped (a DOB in 4000 is still a DOB string)
    assert _clean_date("4000-12-31Z", allow_sentinel=False) == "4000-12-31"


def test_clean_date_none_and_empty():
    assert _clean_date(None, allow_sentinel=False) is None
    assert _clean_date("", allow_sentinel=False) is None
    assert _clean_date("   ", allow_sentinel=False) is None


# --- integration: date cleanup via _parse_profile ---


def test_parse_strips_z_from_dob():
    payload = _sample_payload()
    payload["UserAccountData"]["ebizAccountsWithPersonInfos"]["dateOfBirth"] = "1970-01-01Z"
    assert _parse_profile(payload).date_of_birth == "1970-01-01"


def test_parse_sentinel_coverage_end_becomes_none():
    payload = _sample_payload()
    payload["UserAccountData"]["ebizAccountsWithPersonInfos"]["membershipAccountInfo"][
        "planInfos"
    ][0]["coverageEndDate"] = "4000-12-31Z"
    plans = _parse_profile(payload).insurance_plans
    assert plans[0].coverage_end is None
    # coverage_start (not sentinel) is still present and Z-stripped
    assert plans[0].coverage_start == "2020-01-01"


# --- phone primary-flag handling ---


def test_phones_report_all_non_primary_when_none_flagged():
    """Kaiser ships phones in non-deterministic order and often flags none
    as primary. Don't invent a primary — report honestly and let the caller
    pick from type/label."""
    phones = _parse_phones(
        [
            {
                "type": "EVENING",
                "primaryIndicator": False,
                "phoneNumber": {"area": 1, "exchange": 2, "subscriber": 3},
            },
            {
                "type": "MOBILE",
                "primaryIndicator": False,
                "phoneNumber": {"area": 4, "exchange": 5, "subscriber": 6},
            },
        ]
    )
    assert all(p.is_primary is False for p in phones)


def test_phones_respect_explicit_primary():
    phones = _parse_phones(
        [
            {
                "type": "EVENING",
                "primaryIndicator": False,
                "phoneNumber": {"area": 1, "exchange": 2, "subscriber": 3},
            },
            {
                "type": "MOBILE",
                "primaryIndicator": True,
                "phoneNumber": {"area": 4, "exchange": 5, "subscriber": 6},
            },
        ]
    )
    assert phones[0].is_primary is False
    assert phones[1].is_primary is True


def test_phones_empty_list_stays_empty():
    assert _parse_phones([]) == []


# --- fetch_profile (HTTP integration) ---


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
    req = httpx.Request("GET", f"https://healthy.kaiserpermanente.org{USER_PATH}")
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


def _sample_care_team() -> dict:
    return {
        "ProvidersList": [
            {
                "Name": "JANE DOE MD",
                "Specialty": "Family Practice",
                "Relation": "Primary Care Provider",
                "WebPageUrl": "https://mydoctor.kaiserpermanente.org/ncal/doctor/janedoe",
            },
            {
                "Name": "JOHN SMITH MD",
                "Specialty": "Cardiology",
                "Relation": "Cardiologist",
                "WebPageUrl": "https://mydoctor.kaiserpermanente.org/ncal/doctor/johnsmith",
            },
        ],
        "DescriptiveTitle": "Care Team and Recent Providers",
    }


@pytest.mark.asyncio
async def test_fetch_profile_sends_user_endpoint_with_pharmacy_headers():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, json=_sample_payload()),
        httpx.Response(200, text=_csrf_html("tok")),
        httpx.Response(200, json=_sample_care_team()),
    ])
    try:
        profile = await fetch_profile(KaiserRequest(store))
    finally:
        p.stop()

    assert profile.first_name == "Jane"
    # Assert on the first call — /mycare/v1.0/user with the pharmacy contract
    call_args = mock_client.request.await_args_list[0]
    assert call_args.args[0] == "GET"
    assert call_args.args[1].endswith(USER_PATH)
    headers = call_args.kwargs["headers"]
    assert headers["X-apiKey"] == "kprwdpharmctr68973257122561335296"
    assert headers["X-appName"] == "rx-order-management"
    assert headers["X-componentName"] == "User Profile Component"
    # Inclusion paths are semicolon-joined
    assert "nameDetails" in headers["X-inclusionJsonPath"]
    assert ";" in headers["X-inclusionJsonPath"]


@pytest.mark.asyncio
async def test_fetch_profile_raises_on_http_error():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([httpx.Response(502, text="bad gateway")])
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_profile(KaiserRequest(store))
    finally:
        p.stop()


# --- PCP parser ---


def test_parse_pcp_happy_path():
    pcp = _parse_pcp(_sample_care_team())
    assert pcp is not None
    assert pcp.name == "JANE DOE MD"
    assert pcp.specialty == "Family Practice"
    assert pcp.relation == "Primary Care Provider"
    assert pcp.profile_url == "https://mydoctor.kaiserpermanente.org/ncal/doctor/janedoe"


def test_parse_pcp_ignores_non_pcp_providers():
    payload = {
        "ProvidersList": [
            {"Name": "JOHN SMITH MD", "Relation": "Cardiologist", "Specialty": "Cardiology"},
            {"Name": "JANE ROE MD", "Relation": "Dermatologist", "Specialty": "Derm"},
        ]
    }
    assert _parse_pcp(payload) is None


def test_parse_pcp_returns_first_when_multiple_pcps():
    payload = {
        "ProvidersList": [
            {"Name": "FIRST PCP", "Relation": "Primary Care Provider", "Specialty": "FP"},
            {"Name": "SECOND PCP", "Relation": "Primary Care Provider", "Specialty": "IM"},
        ]
    }
    pcp = _parse_pcp(payload)
    assert pcp is not None
    assert pcp.name == "FIRST PCP"


def test_parse_pcp_missing_or_malformed_providers_list():
    assert _parse_pcp({}) is None
    assert _parse_pcp({"ProvidersList": None}) is None
    assert _parse_pcp({"ProvidersList": "not a list"}) is None
    assert _parse_pcp({"ProvidersList": []}) is None


def test_parse_pcp_non_dict_payload_returns_none():
    assert _parse_pcp(None) is None  # type: ignore[arg-type]
    assert _parse_pcp([]) is None  # type: ignore[arg-type]


def test_parse_pcp_skips_non_dict_items():
    payload = {
        "ProvidersList": [
            "not a dict",
            None,
            {"Name": "REAL PCP", "Relation": "Primary Care Provider", "Specialty": "FP"},
        ]
    }
    pcp = _parse_pcp(payload)
    assert pcp is not None
    assert pcp.name == "REAL PCP"


def test_parse_pcp_skips_pcp_entry_without_name():
    payload = {
        "ProvidersList": [
            {"Relation": "Primary Care Provider", "Specialty": "FP"},  # no Name
            {"Name": "REAL PCP", "Relation": "Primary Care Provider"},
        ]
    }
    pcp = _parse_pcp(payload)
    assert pcp is not None
    assert pcp.name == "REAL PCP"


def test_parse_pcp_missing_optional_fields():
    payload = {"ProvidersList": [{"Name": "MINIMAL PCP", "Relation": "Primary Care Provider"}]}
    pcp = _parse_pcp(payload)
    assert pcp is not None
    assert pcp.name == "MINIMAL PCP"
    assert pcp.specialty is None
    assert pcp.profile_url is None


# --- CSRF token fetch ---


@pytest.mark.asyncio
async def test_fetch_csrf_token_extracts_token_value():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([httpx.Response(200, text=_csrf_html("abc-123-XYZ"))])
    try:
        token = await _fetch_csrf_token(KaiserRequest(store))
    finally:
        p.stop()

    assert token == "abc-123-XYZ"
    call = mock_client.request.await_args
    assert call.args[0] == "GET"
    assert CSRF_PATH in call.args[1]
    # Noise-bust query param
    assert "noCache" in call.kwargs.get("params", {})


@pytest.mark.asyncio
async def test_fetch_csrf_token_raises_when_input_missing():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([httpx.Response(200, text="<div>no token here</div>")])
    try:
        with pytest.raises(ValueError, match="CSRF token"):
            await _fetch_csrf_token(KaiserRequest(store))
    finally:
        p.stop()


@pytest.mark.asyncio
async def test_fetch_csrf_token_propagates_http_error():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([httpx.Response(500, text="boom")])
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await _fetch_csrf_token(KaiserRequest(store))
    finally:
        p.stop()


# --- _fetch_pcp (HTTP integration: CSRF then CareTeam) ---


@pytest.mark.asyncio
async def test_fetch_pcp_sends_csrf_then_careteam_with_token():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, text=_csrf_html("my-token-value")),
        httpx.Response(200, json=_sample_care_team()),
    ])
    try:
        pcp = await _fetch_pcp(KaiserRequest(store))
    finally:
        p.stop()

    assert pcp is not None
    assert pcp.name == "JANE DOE MD"
    assert mock_client.request.await_count == 2

    call1 = mock_client.request.await_args_list[0]
    assert call1.args[0] == "GET"
    assert CSRF_PATH in call1.args[1]

    call2 = mock_client.request.await_args_list[1]
    assert call2.args[0] == "POST"
    assert CARE_TEAM_PATH in call2.args[1]
    headers2 = call2.kwargs["headers"]
    assert headers2["__RequestVerificationToken"] == "my-token-value"
    assert headers2["Referer"] == "https://healthy.kaiserpermanente.org/mychartcn/clinical/careteam"
    assert headers2["X-Requested-With"] == "XMLHttpRequest"
    params2 = call2.kwargs["params"]
    assert params2["isPrimaryStandalone"] == "true"
    assert params2["ComponentNumber"] == "2"
    assert call2.kwargs.get("content") == b""


@pytest.mark.asyncio
async def test_fetch_pcp_returns_none_when_response_has_no_pcp():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html("tok")),
        httpx.Response(200, json={"ProvidersList": [
            {"Name": "CARDIO", "Relation": "Cardiologist"}
        ]}),
    ])
    try:
        pcp = await _fetch_pcp(KaiserRequest(store))
    finally:
        p.stop()

    assert pcp is None


# --- fetch_profile full integration (user + CSRF + CareTeam) ---


@pytest.mark.asyncio
async def test_fetch_profile_populates_pcp_on_happy_path():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, json=_sample_payload()),
        httpx.Response(200, text=_csrf_html("session-token")),
        httpx.Response(200, json=_sample_care_team()),
    ])
    try:
        profile = await fetch_profile(KaiserRequest(store))
    finally:
        p.stop()

    # Demographics intact
    assert profile.first_name == "Jane"
    assert profile.region == "NCA"
    # PCP populated from care team
    assert profile.pcp is not None
    assert profile.pcp.name == "JANE DOE MD"
    assert profile.pcp.specialty == "Family Practice"
    assert profile.pcp.relation == "Primary Care Provider"
    assert mock_client.request.await_count == 3


@pytest.mark.asyncio
async def test_fetch_profile_resilient_when_csrf_fetch_fails():
    """Demographics survive even if the CSRF endpoint returns 500."""
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, json=_sample_payload()),
        httpx.Response(500, text="kaboom"),  # CSRF fetch fails
    ])
    try:
        profile = await fetch_profile(KaiserRequest(store))
    finally:
        p.stop()

    assert profile.first_name == "Jane"
    assert profile.pcp is None


@pytest.mark.asyncio
async def test_fetch_profile_resilient_when_csrf_html_malformed():
    """Demographics survive if the CSRF response lacks the token input element."""
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, json=_sample_payload()),
        httpx.Response(200, text="<div>no input element</div>"),
    ])
    try:
        profile = await fetch_profile(KaiserRequest(store))
    finally:
        p.stop()

    assert profile.first_name == "Jane"
    assert profile.pcp is None


@pytest.mark.asyncio
async def test_fetch_profile_resilient_when_careteam_returns_error():
    """Demographics survive if CareTeam/Load itself 500s after CSRF succeeded."""
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, json=_sample_payload()),
        httpx.Response(200, text=_csrf_html("tok")),
        httpx.Response(500, text="care team down"),
    ])
    try:
        profile = await fetch_profile(KaiserRequest(store))
    finally:
        p.stop()

    assert profile.first_name == "Jane"
    assert profile.pcp is None
