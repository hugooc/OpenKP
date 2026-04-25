"""Patient profile scraper.

First Phase 2 read tool. Returns demographics, contact info, and insurance
plan details sourced from Kaiser's `/mycare/v1.0/user` endpoint.

Why /mycare/v1.0/user and not /mycare/v1.0/uidatalayer/s/profile (the KPDL
consumer layer): KPDL is a write-through data layer that populates as a
side effect of other Kaiser calls, primarily /mycare/v1.0/user itself. A
cold httpx call to KPDL returns a minimal shell with empty `data`. The
upstream source is direct and has a richer response. See
`docs/adr/006-user-endpoint-piggyback.md` for the trust-boundary tradeoff
of using the pharmacy consumer's X-apiKey.

Endpoint: GET https://healthy.kaiserpermanente.org/mycare/v1.0/user
Docs:     docs/research/endpoints/profile.md
"""

from __future__ import annotations

import logging
import random
from typing import Any

from pydantic import BaseModel, Field

from openkp.scrapers.csrf import fetch_csrf_token
from openkp.scrapers.emergency_contacts import EmergencyContact, fetch_emergency_contacts
from openkp.scrapers.request import KaiserRequest

logger = logging.getLogger(__name__)

USER_PATH = "/mycare/v1.0/user"
USER_REFERER = "https://healthy.kaiserpermanente.org/mychartcn/Home?lang=en-US"

# Care Team endpoints. See docs/research/endpoints/profile.md.
CARE_TEAM_PATH = "/mychartcn/Clinical/CareTeam/Load"
CARE_TEAM_REFERER = "https://healthy.kaiserpermanente.org/mychartcn/clinical/careteam"
PCP_RELATION = "Primary Care Provider"

# Pharmacy consumer identity. See ADR-006.
PHARMACY_API_KEY = "kprwdpharmctr68973257122561335296"
PHARMACY_APP_NAME = "rx-order-management"
PHARMACY_COMPONENT_NAME = "User Profile Component"
PHARMACY_VERSION_ID = "3.0.1.2"

# JSONPath filter: ask the server for only the fields we care about. Data
# minimization + smaller responses + less to break when Kaiser reshapes
# unrelated pieces of UserAccountData.
INCLUSION_PATHS = [
    "$.UserAccountData.ebizAccountsWithPersonInfos.nameDetails",
    "$.UserAccountData.ebizAccountsWithPersonInfos.contactInfo.addressInfos",
    "$.UserAccountData.ebizAccountsWithPersonInfos.contactInfo.phoneInfos",
    "$.UserAccountData.ebizAccountsWithPersonInfos.contactInfo.emailAddresseInfos",
    "$.UserAccountData.ebizAccountsWithPersonInfos.dateOfBirth",
    "$.UserAccountData.ebizAccountsWithPersonInfos.age",
    "$.UserAccountData.ebizAccountsWithPersonInfos.gender",
    "$.UserAccountData.ebizAccountsWithPersonInfos.areaOfCareInfos",
    "$.UserAccountData.ebizAccountsWithPersonInfos.ebizAccountInfos[0].eBizAccountRoleInfo[0].accountRoleRegion",
    "$.UserAccountData.ebizAccountsWithPersonInfos.ebizAccountInfos[0].eBizAccountRoleInfo[0].primaryRegion",
    "$.UserAccountData.ebizAccountsWithPersonInfos.membershipAccountInfo.accountId",
    "$.UserAccountData.ebizAccountsWithPersonInfos.membershipAccountInfo.region",
    "$.UserAccountData.ebizAccountsWithPersonInfos.membershipAccountInfo.planInfos[0].purchaserName",
    "$.UserAccountData.ebizAccountsWithPersonInfos.membershipAccountInfo.planInfos[0].consumerPlanType",
    "$.UserAccountData.ebizAccountsWithPersonInfos.membershipAccountInfo.planInfos[0].coverageStartDate",
    "$.UserAccountData.ebizAccountsWithPersonInfos.membershipAccountInfo.planInfos[0].coverageEndDate",
    "$.UserAccountData.userIdentityInfo.guid",
    "$.UserAccountData.userIdentityInfo.email",
    "$.UserAccountData.userIdentityInfo.preferredGivenName",
]

# Observed: Kaiser sometimes returns `"region": "MRN"` (a type code) in
# membershipAccountInfo. Treat clearly-bogus values as absent.
_BAD_REGION_VALUES = {"MRN"}

# Coverage dates sometimes come back as a sentinel like "4000-12-31" to
# indicate "no end date." Treat any year >= this threshold as null.
_SENTINEL_DATE_YEAR = 2200


# --- models ---


class Address(BaseModel):
    """A single mailing / home address."""

    type: str | None = None        # e.g. "MAILING", "HOME"
    label: str | None = None
    street1: str | None = None
    street2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    is_primary: bool = False


class Phone(BaseModel):
    """A single phone number."""

    type: str | None = None        # e.g. "RESIDENCE", "MOBILE"
    label: str | None = None
    number: str | None = None      # formatted "AREA-EXCHANGE-SUBSCRIBER"
    is_primary: bool = False


class InsurancePlan(BaseModel):
    """One insurance plan the member is enrolled in."""

    purchaser_name: str | None = None
    plan_type: str | None = None
    coverage_start: str | None = None
    coverage_end: str | None = None
    region: str | None = None


class Provider(BaseModel):
    """A care-team provider."""

    name: str
    specialty: str | None = None
    relation: str | None = None       # Kaiser's label, e.g. "Primary Care Provider"
    profile_url: str | None = None    # Public mydoctor.kaiserpermanente.org page


class Profile(BaseModel):
    """Patient profile. Stable field names; some fields placeholder until mapped."""

    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    preferred_name: str | None = None

    date_of_birth: str | None = None
    age: int | None = None
    gender: str | None = None

    email: str | None = None
    addresses: list[Address] = Field(default_factory=list)
    phones: list[Phone] = Field(default_factory=list)

    mrn: str | None = None
    guid: str | None = None
    region: str | None = None

    insurance_plans: list[InsurancePlan] = Field(default_factory=list)

    pcp: Provider | None = None
    emergency_contacts: list[EmergencyContact] = Field(default_factory=list)


# --- fetcher ---


async def fetch_profile(client: KaiserRequest) -> Profile:
    """Hit /mycare/v1.0/user, then CareTeam + emergency contacts, merge into one `Profile`.

    Never raises on missing fields — returns partial data with `None`s for
    anything we couldn't find. Failures fetching the care team or contact
    list log a warning and leave the corresponding fields empty rather than
    breaking the demographics payload, which is the critical leg.
    """
    response = await client.get(USER_PATH, headers=_user_headers())
    response.raise_for_status()
    profile = _parse_profile(response.json())

    try:
        profile.pcp = await _fetch_pcp(client)
    except Exception as exc:
        logger.warning("PCP fetch failed (%s); returning profile without it", type(exc).__name__)

    try:
        profile.emergency_contacts = await fetch_emergency_contacts(client)
    except Exception as exc:
        logger.warning(
            "emergency contact fetch failed (%s); returning profile without it",
            type(exc).__name__,
        )

    return profile


async def _fetch_pcp(client: KaiserRequest) -> Provider | None:
    """Fetch the user's Primary Care Provider from CareTeam/Load.

    Two-step dance: grab a CSRF token, then POST it back with the form request.
    Returns `None` if no provider is flagged as the PCP, or if the response
    is empty / malformed.
    """
    token = await fetch_csrf_token(client, referer=CARE_TEAM_REFERER)
    params = {
        "hfrId": "",
        "sources": "",
        "actions": "",
        "isPrimaryStandalone": "true",
        "ComponentNumber": "2",
        "noCache": f"{random.random()}",
    }
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": CARE_TEAM_REFERER,
        "X-Requested-With": "XMLHttpRequest",
        "__RequestVerificationToken": token,
    }
    response = await client.post(CARE_TEAM_PATH, params=params, headers=headers, content=b"")
    response.raise_for_status()
    return _parse_pcp(response.json())


def _user_headers() -> dict[str, str]:
    """Build the header set that Kaiser's API gateway requires for /mycare/v1.0/user."""
    return {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Referer": USER_REFERER,
        "X-apiKey": PHARMACY_API_KEY,
        "X-appName": PHARMACY_APP_NAME,
        "X-componentName": PHARMACY_COMPONENT_NAME,
        "X-includeEntitlements": "false",
        "X-includeProxyEntitlements": "false",
        "X-inclusionJsonPath": ";".join(INCLUSION_PATHS),
        "X-osVersion": "0",
        "X-Requested-With": "XMLHttpRequest",
        "X-retainJsonSchema": "true",
        "X-sessionToken": "true",
        "X-useragentCategory": "B",
        "X-useragentType": "Desktop",
        "X-versionId": PHARMACY_VERSION_ID,
    }


# --- parser ---


def _parse_profile(payload: dict[str, Any]) -> Profile:
    """Walk UserAccountData and populate a `Profile`.

    Response shape (trimmed):

        {
          "UserAccountData": {
            "ebizAccountsWithPersonInfos": {
              "nameDetails": {surname, firstName, middleName},
              "contactInfo": {addressInfos: [...], phoneInfos: [...]},
              "dateOfBirth": "YYYY-MM-DD",
              "age": 59,
              "gender": "M",
              "areaOfCareInfos": [{guid, mrn, ...}],
              "membershipAccountInfo": {accountId, region, planInfos: [...]}
            },
            "userIdentityInfo": {guid, email, preferredGivenName, ...}
          }
        }
    """
    user_data = payload.get("UserAccountData")
    if not isinstance(user_data, dict):
        logger.warning("No UserAccountData in response; returning empty profile")
        return Profile()

    ebiz = user_data.get("ebizAccountsWithPersonInfos") or {}
    identity = user_data.get("userIdentityInfo") or {}

    name_details = ebiz.get("nameDetails") or {}
    contact = ebiz.get("contactInfo") or {}
    membership = ebiz.get("membershipAccountInfo") or {}
    area_of_care = ebiz.get("areaOfCareInfos") or []

    # MRN: prefer areaOfCareInfos[0].mrn (explicitly labeled) with fallback to
    # membershipAccountInfo.accountId (same value in practice).
    mrn = None
    if isinstance(area_of_care, list) and area_of_care:
        mrn = _str_or_none(area_of_care[0].get("mrn") if isinstance(area_of_care[0], dict) else None)
    if mrn is None:
        mrn = _str_or_none(membership.get("accountId"))

    # GUID: prefer userIdentityInfo.guid (canonical) with fallback to areaOfCareInfos[0].guid.
    guid = _str_or_none(identity.get("guid"))
    if guid is None and isinstance(area_of_care, list) and area_of_care:
        first = area_of_care[0] if isinstance(area_of_care[0], dict) else {}
        guid = _str_or_none(first.get("guid"))

    return Profile(
        first_name=_str_or_none(name_details.get("firstName")),
        middle_name=_str_or_none(name_details.get("middleName")),
        last_name=_str_or_none(name_details.get("surname")),
        preferred_name=_str_or_none(identity.get("preferredGivenName")),
        date_of_birth=_clean_date(ebiz.get("dateOfBirth"), allow_sentinel=False),
        age=_int_or_none(ebiz.get("age")),
        gender=_str_or_none(ebiz.get("gender")),
        email=_str_or_none(identity.get("email")),
        addresses=_parse_addresses(contact.get("addressInfos")),
        phones=_parse_phones(contact.get("phoneInfos")),
        mrn=mrn,
        guid=guid,
        region=_parse_region(ebiz),
        insurance_plans=_parse_plans(membership.get("planInfos")),
    )


def _parse_region(ebiz: dict[str, Any]) -> str | None:
    """Pick a region code, rejecting bogus type-code values at every source.

    Observed live: Kaiser sometimes returns the type code "MRN" in EVERY
    region-carrying field (primaryRegion, accountRoleRegion,
    membershipAccountInfo.region) instead of a real region code like
    "NCA"/"SCA"/"NW". The bad-value filter has to apply at every source —
    not just the fallback — or we leak "MRN" upstream.

    Priority order:
      1. ebizAccountInfos[0].eBizAccountRoleInfo[0].primaryRegion
      2. ebizAccountInfos[0].eBizAccountRoleInfo[0].accountRoleRegion
      3. membershipAccountInfo.region

    Returns `None` if no source holds a clean value — `/mycare/v1.0/user`
    simply doesn't give us a usable region for some accounts, and we
    prefer honest nulls over garbage.
    """
    candidates: list[str | None] = []

    account_infos = ebiz.get("ebizAccountInfos")
    if isinstance(account_infos, list) and account_infos:
        first_account = account_infos[0] if isinstance(account_infos[0], dict) else {}
        roles = first_account.get("eBizAccountRoleInfo")
        if isinstance(roles, list) and roles:
            first_role = roles[0] if isinstance(roles[0], dict) else {}
            candidates.append(_str_or_none(first_role.get("primaryRegion")))
            candidates.append(_str_or_none(first_role.get("accountRoleRegion")))

    membership = ebiz.get("membershipAccountInfo") or {}
    candidates.append(_str_or_none(membership.get("region")))

    for value in candidates:
        if value and value.upper() not in _BAD_REGION_VALUES:
            return value
    return None


def _parse_addresses(raw: Any) -> list[Address]:
    if not isinstance(raw, list):
        return []
    out: list[Address] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append(
            Address(
                type=_str_or_none(item.get("type")),
                label=_str_or_none(item.get("label")),
                street1=_str_or_none(item.get("street1")),
                street2=_str_or_none(item.get("street2")),
                city=_str_or_none(item.get("city")),
                state=_str_or_none(item.get("state")),
                postal_code=_str_or_none(item.get("postalCode")),
                is_primary=bool(item.get("preferredIn")),
            )
        )
    return out


def _parse_phones(raw: Any) -> list[Phone]:
    """Map Kaiser's phoneInfos to our Phone list, reporting `is_primary` honestly.

    Observed live: Kaiser often returns every phone with `primaryIndicator: false`
    AND lists them in non-deterministic order between calls. A prior heuristic
    defaulted the first entry to primary when nothing was flagged, but that
    made `is_primary` flip across consecutive calls. Better to be honest: if
    Kaiser didn't flag anything, return all `is_primary: false` and let the
    caller pick based on `type`/`label`.
    """
    if not isinstance(raw, list):
        return []
    out: list[Phone] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append(
            Phone(
                type=_str_or_none(item.get("type")),
                label=_str_or_none(item.get("label")),
                number=_format_phone(item.get("phoneNumber")),
                is_primary=bool(item.get("primaryIndicator")),
            )
        )
    return out


def _parse_pcp(payload: dict[str, Any]) -> Provider | None:
    """Walk ProvidersList, pick the first entry flagged as the PCP.

    Response shape (trimmed):

        {
          "ProvidersList": [
            {
              "Name": "PROVIDER ONE MD",
              "Specialty": "Family Practice",
              "Relation": "Primary Care Provider",
              "WebPageUrl": "https://mydoctor.kaiserpermanente.org/...",
              ...
            },
            ...
          ],
          ...
        }

    Returns `None` if ProvidersList is missing / empty, or if no entry has
    Relation == "Primary Care Provider".
    """
    if not isinstance(payload, dict):
        return None
    providers = payload.get("ProvidersList")
    if not isinstance(providers, list):
        return None
    for item in providers:
        if not isinstance(item, dict):
            continue
        if _str_or_none(item.get("Relation")) != PCP_RELATION:
            continue
        name = _str_or_none(item.get("Name"))
        if name is None:
            continue
        return Provider(
            name=name,
            specialty=_str_or_none(item.get("Specialty")),
            relation=PCP_RELATION,
            profile_url=_str_or_none(item.get("WebPageUrl")),
        )
    return None


def _parse_plans(raw: Any) -> list[InsurancePlan]:
    if not isinstance(raw, list):
        return []
    out: list[InsurancePlan] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append(
            InsurancePlan(
                purchaser_name=_str_or_none(item.get("purchaserName")),
                plan_type=_str_or_none(item.get("consumerPlanType")),
                coverage_start=_clean_date(item.get("coverageStartDate"), allow_sentinel=True),
                coverage_end=_clean_date(item.get("coverageEndDate"), allow_sentinel=True),
            )
        )
    return out


def _format_phone(pn: Any) -> str | None:
    """Format Kaiser's {area, exchange, subscriber} shape as 'AAA-EEE-SSSS'."""
    if not isinstance(pn, dict):
        return None
    area = _str_or_none(pn.get("area"))
    exchange = _str_or_none(pn.get("exchange"))
    subscriber = _str_or_none(pn.get("subscriber"))
    if not (area and exchange and subscriber):
        return None
    extension = _str_or_none(pn.get("extension"))
    base = f"{area}-{exchange}-{subscriber}"
    return f"{base} x{extension}" if extension else base


def _str_or_none(value: Any) -> str | None:
    """Coerce to stripped string, or None if empty / missing."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _int_or_none(value: Any) -> int | None:
    """Coerce to int if possible, else None. Handles floats (age: 59.0)."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_date(value: Any, *, allow_sentinel: bool) -> str | None:
    """Normalize a Kaiser date string.

    Kaiser emits dates like "1970-01-01Z" (trailing zulu marker) and "4000-12-31Z"
    as a sentinel for "no end date." This strips the trailing Z and, when
    `allow_sentinel=True`, returns None for dates at or past _SENTINEL_DATE_YEAR.
    """
    s = _str_or_none(value)
    if s is None:
        return None
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1]
    if allow_sentinel:
        # Cheap year check: leading 4 chars, digits, >= sentinel.
        head = s[:4]
        if len(head) == 4 and head.isdigit() and int(head) >= _SENTINEL_DATE_YEAR:
            return None
    return s or None
