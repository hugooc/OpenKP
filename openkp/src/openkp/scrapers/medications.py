"""Medication scraper.

One MCP tool surfaces from this module:

- `list_medications` — current and recent prescriptions with dose, prescriber,
  pharmacy, last fill, refills remaining, copay, and per-Rx auto-refill state.

Architectural note: medications are the first OpenKP tool that talks to the
new pharmacy BFF microservices on `apims.kaiserpermanente.org`, not the
classic `healthy.kaiserpermanente.org/mychartcn/...` family used by labs,
messages, and profile. The BFF takes its own header set including a per-user
`x-guid` value, which we fetch once per call from `/mycare/v1.0/user`.

Cookies set on the parent domain `.kaiserpermanente.org` ride along to the
BFF host automatically — httpx scopes by domain, not by exact host. If the
session cookie is scoped only to `healthy.kaiserpermanente.org`, the BFF call
will return 401/403 and we'll need a separate auth handshake. See the open
questions in `docs/research/endpoints/medications.md`.

Docs: `docs/research/endpoints/medications.md`
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from openkp.scrapers.profile import (
    INCLUSION_PATHS,
    PHARMACY_API_KEY,
    PHARMACY_APP_NAME,
    PHARMACY_COMPONENT_NAME,
    PHARMACY_VERSION_ID,
    USER_PATH,
    USER_REFERER,
)
from openkp.scrapers.request import KaiserRequest

logger = logging.getLogger(__name__)

# BFF endpoint
BFF_HOST = "https://apims.kaiserpermanente.org"
RX_DETAILS_PATH = "/kp/mycare/pharmacy-microservices/rx-cost-inventory-bff/v1/rxDetails"
RX_DETAILS_URL = f"{BFF_HOST}{RX_DETAILS_PATH}"

# BFF auth + identity. The IBM client ID is hardcoded in Kaiser's React
# bundle and is the same for every user — analogous to the pharmacy X-apiKey
# we use against /mycare/v1.0/user (see ADR-006).
BFF_CLIENT_ID = "9dea3678-801b-4069-b111-4d3c5f56c9de"
BFF_ORIGIN = "https://healthy.kaiserpermanente.org"
# Kaiser's own front end sends "MRN" as x-region — the same noisy sentinel we
# already filter out of profile responses. Send it verbatim; the BFF doesn't
# care about the value.
BFF_REGION_SENTINEL = "MRN"

# Filter values for the prescriptions_filter query param.
FILTER_ALL = "all"
FILTER_FILLABLE = "fillable"
_VALID_FILTERS = {FILTER_ALL, FILTER_FILLABLE}

# rxDetails uses the same executionContext envelope as the rest of Kaiser's
# pharmacy APIs. "000" means OK; anything else is some flavor of error or
# warning that we surface in the response.
SUCCESS_STATUS_CODE = "000"

# We piggyback on the exact same inclusion paths that profile.py uses for
# /mycare/v1.0/user. A narrower request (just the GUID path) returned a
# response shape that didn't include the UserAccountData envelope — Kaiser's
# API gateway appears to flatten or strip-wrap when only one path is asked
# for. Reusing the known-working path set costs us a few extra bytes per
# call and avoids guessing about the gateway's behavior.
_GUID_INCLUSION_PATHS = ";".join(INCLUSION_PATHS)

# Date strings come back in mixed formats. Try each, in order, before giving up.
_DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d")
# Values we treat as "no date" rather than trying to parse.
_DATE_NULL_TOKENS = {"", "n/a", "na", "none", "null"}


# --- models ---


class MedicationFillOption(BaseModel):
    """One way Kaiser will dispense this Rx (local pickup vs mail, etc.)."""

    delivery_method: str | None = None      # "L" = local, "M" = mail
    days_supply: int | None = None
    quantity: int | None = None
    plan_pay: float | None = None           # What Kaiser's pharmacy plan covers
    estimated_copay: float | None = None    # What you pay
    usual_customary_charge: float | None = None
    copay_status: str | None = None         # "000" = approved
    copay_status_detail: str | None = None  # Human-readable status


class Medication(BaseModel):
    """One active or recent prescription."""

    rx_number: str | None = None
    name: str | None = None                 # Kaiser's medicineName, e.g. "CHLORTHALID 25 MG TAB MYLA"
    brand_name: str | None = None           # Kaiser's commonBrandName, e.g. "HYGROTON"
    instructions: str | None = None         # Sig text, e.g. "Take 1 tablet by mouth daily"
    prescriber: str | None = None
    ndc: str | None = None                  # National Drug Code (links to drug-image, med-guide endpoints)
    region: str | None = None               # "NCA" etc — clean here, unlike profile region fields
    dea_schedule: str | None = None         # Kaiser's deacode field, raw

    # Dates are normalized to ISO YYYY-MM-DD strings. Null means absent or unparseable.
    prescribed_on: str | None = None
    last_refill_date: str | None = None
    last_sold_date: str | None = None
    next_fill_date: str | None = None
    next_fill_eligible_date: str | None = None

    days_supply: int | None = None
    refills_remaining: int | None = None
    copay: float | None = None              # Last copay seen (afcInfo.copay)

    is_mailable: bool = False
    is_first_fill: bool = False
    is_new_prescription: bool = False
    is_refill_eligible: bool = False
    is_as_needed: bool = False              # PRN
    is_compound: bool = False
    auto_refill_on: bool = False
    auto_refill_eligible: bool = False

    # Derived from which bucket Kaiser put this Rx in. "True" means you can
    # request a refill via the kp.org pharmacy UI right now. "False" means
    # something blocks an immediate refill — see `refill_blocked_reason` for
    # Kaiser's reason code (when present), or check `next_fill_eligible_date`.
    is_currently_orderable: bool = True
    refill_blocked_reason: str | None = None  # rarCodeKey when rarStatus is true

    drug_info_url: str | None = None        # Absolute URL to KP's drug encyclopedia entry

    fill_options: list[MedicationFillOption] = Field(default_factory=list)


class MedicationsResponse(BaseModel):
    """Wrapper around the medication list with summary counts and status."""

    medications: list[Medication] = Field(default_factory=list)
    total_count: int = 0
    refillable_count: int = 0               # Kaiser's totalRefillableCount
    recent_refillable_count: int = 0        # Kaiser's recentRefillableCount
    status_code: str | None = None          # executionContext.statusCode
    status_details: str | None = None       # executionContext.statusDetails


# --- public ---


async def fetch_medications(
    client: KaiserRequest,
    filter: str = FILTER_ALL,
) -> MedicationsResponse:
    """List active and recent prescriptions.

    Args:
      filter: "all" returns the full medication list. "fillable" filters to
        Rx that Kaiser flags as currently refillable. We pass it through to
        the BFF rather than filtering client-side because Kaiser's logic
        considers timing, inventory, and regulatory rules we don't model.

    Returns a `MedicationsResponse`. On a non-success `executionContext.statusCode`,
    the response carries the status fields and an empty medications list — we
    don't raise, since callers can decide whether to show "no meds" or surface
    the status.
    """
    if filter not in _VALID_FILTERS:
        raise ValueError(f"filter must be one of {sorted(_VALID_FILTERS)}, got {filter!r}")

    guid = await _fetch_user_guid(client)

    response = await client.get(
        RX_DETAILS_URL,
        params={"prescriptions_filter": filter},
        headers=_bff_headers(guid, filter),
    )
    response.raise_for_status()
    return _parse_medications_response(response.json())


# --- private: GUID fetch ---


async def _fetch_user_guid(client: KaiserRequest) -> str:
    """One-shot call to /mycare/v1.0/user that returns just the user GUID.

    Mirrors `profile._parse_profile`'s GUID extraction, including its
    fallback chain: prefer `userIdentityInfo.guid` (canonical) but accept
    `ebizAccountsWithPersonInfos.areaOfCareInfos[0].guid` when the canonical
    field is empty. Raises `ValueError` with a key-level diagnostic if the
    GUID can't be found at either source — the BFF is unusable without it.
    """
    response = await client.get(USER_PATH, headers=_user_guid_headers())
    response.raise_for_status()
    data = response.json() if response.content else {}

    user_data = data.get("UserAccountData") or {}
    identity = user_data.get("userIdentityInfo") or {}
    ebiz = user_data.get("ebizAccountsWithPersonInfos") or {}
    area_of_care = ebiz.get("areaOfCareInfos") or []

    # Try canonical path; fall back to areaOfCareInfos[0].guid. Coerce to
    # string regardless of source type — Kaiser sometimes returns the GUID
    # as a JSON number (`1234567`) rather than a string (`"1234567"`), and
    # profile.py handles this transparently via _str_or_none. We do the same.
    guid = _coerce_guid(identity.get("guid"))
    if guid is None and isinstance(area_of_care, list) and area_of_care:
        first = area_of_care[0] if isinstance(area_of_care[0], dict) else {}
        guid = _coerce_guid(first.get("guid"))

    if guid is not None:
        return guid

    # Diagnostic: tell us which level of nesting + value type was missing,
    # without leaking PHI. The type info matters because we've been bitten
    # by string-vs-int mismatches.
    raise ValueError(
        "Could not extract user GUID from /mycare/v1.0/user response. "
        f"top-level keys={sorted(data.keys()) if isinstance(data, dict) else 'not-a-dict'}, "
        f"UserAccountData keys={sorted(user_data.keys())}, "
        f"userIdentityInfo keys={sorted(identity.keys())}, "
        f"userIdentityInfo.guid type={type(identity.get('guid')).__name__}, "
        f"ebizAccountsWithPersonInfos keys={sorted(ebiz.keys())}, "
        f"areaOfCareInfos len={len(area_of_care) if isinstance(area_of_care, list) else 'not-a-list'}"
    )


def _coerce_guid(value: Any) -> str | None:
    """Stringify any non-empty GUID value, matching profile._str_or_none semantics.

    Accepts str, int, or anything else with a sensible str() representation.
    Returns None for None, empty strings, or whitespace-only strings.
    """
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _user_guid_headers() -> dict[str, str]:
    """Pharmacy header contract narrowed to just the GUID inclusion path.

    Matches the full header set in `profile._user_headers` because Kaiser's
    API gateway returns 502 if any of these are missing — only the inclusion
    path differs.
    """
    return {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Referer": USER_REFERER,
        "X-apiKey": PHARMACY_API_KEY,
        "X-appName": PHARMACY_APP_NAME,
        "X-componentName": PHARMACY_COMPONENT_NAME,
        "X-includeEntitlements": "false",
        "X-includeProxyEntitlements": "false",
        "X-inclusionJsonPath": _GUID_INCLUSION_PATHS,
        "X-osVersion": "0",
        "X-Requested-With": "XMLHttpRequest",
        "X-retainJsonSchema": "true",
        "X-sessionToken": "true",
        "X-useragentCategory": "B",
        "X-useragentType": "Desktop",
        "X-versionId": PHARMACY_VERSION_ID,
    }


# --- private: BFF headers ---


def _bff_headers(x_guid: str, prescriptions_filter: str) -> dict[str, str]:
    """Header set required by Kaiser's pharmacy BFF microservices.

    The X-KPSessionID = "undefined" string is intentional — that's literally
    what Kaiser's React bundle sends. Same with x-region: "MRN", which is the
    sentinel we filter out of profile responses but the BFF expects verbatim.
    """
    return {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": BFF_ORIGIN,
        "Referer": f"{BFF_ORIGIN}/",
        "X-IBM-client-Id": BFF_CLIENT_ID,
        "X-KPSessionID": "undefined",
        "X-disablecache": "false",
        "X-idType": "guid",
        "X-region": BFF_REGION_SENTINEL,
        "x-guid": x_guid,
        "x-benefitsIndicator-feature": "true",
        "x-cost-feature": "true",
        "x-prescriptionsfilter": prescriptions_filter,
    }


# --- private: parsers ---


def _parse_medications_response(payload: Any) -> MedicationsResponse:
    """Walk the rxDetails response into a `MedicationsResponse`.

    Tolerant of malformed responses: missing fields become None / empty list,
    non-success status codes return an empty medications list with the
    status surfaced, never raises on unexpected shapes.
    """
    if not isinstance(payload, dict):
        return MedicationsResponse()

    exec_ctx = payload.get("executionContext") or {}
    status_code = _str_or_none(exec_ctx.get("statusCode"))
    status_details = _str_or_none(exec_ctx.get("statusDetails"))

    prescriptions = payload.get("prescriptions") or {}
    fillable_raw = prescriptions.get("fillable") or []
    non_fillable_raw = prescriptions.get("nonFillable") or []

    medications: list[Medication] = []
    if isinstance(fillable_raw, list):
        for row in fillable_raw:
            med = _parse_one_med(row, is_currently_orderable=True)
            if med is not None:
                medications.append(med)
    if isinstance(non_fillable_raw, list):
        for row in non_fillable_raw:
            med = _parse_one_med(row, is_currently_orderable=False)
            if med is not None:
                medications.append(med)

    return MedicationsResponse(
        medications=medications,
        total_count=len(medications),
        refillable_count=_int_or_none(prescriptions.get("totalRefillableCount")) or 0,
        recent_refillable_count=_int_or_none(prescriptions.get("recentRefillableCount")) or 0,
        status_code=status_code,
        status_details=status_details,
    )


def _parse_one_med(row: Any, *, is_currently_orderable: bool) -> Medication | None:
    """Parse one fillable[] or nonFillable[] entry into a `Medication`.

    Returns None if `row` isn't a dict (defensive — Kaiser hasn't done this,
    but mixed buckets would be hostile to crash on).
    """
    if not isinstance(row, dict):
        return None

    afc_info = row.get("afcInfo") or {}

    # Most date fields are MM/DD/YYYY but recentRxDate sometimes shows up as
    # ISO YYYY-MM-DD on a single Rx in an otherwise-US response. _parse_date
    # accepts both formats and "N/A".
    prescribed_on = _parse_date(row.get("prescribedOn"))
    last_refill = _parse_date(row.get("lastRefillDate"))
    last_sold = _parse_date(row.get("lastSoldDate"))
    next_fill = _parse_date(row.get("nextFillDate"))
    next_fill_eligible = _parse_date(afc_info.get("nextFillEligibleDate"))

    # rarCodeKey is non-empty even when rarStatus is false in some cases (we've
    # seen it carry whitespace); only surface as a blocked reason when the
    # status flag confirms it.
    rar_status = bool(row.get("rarStatus"))
    rar_code = _str_or_none(row.get("rarCodeKey"))
    blocked_reason = rar_code if rar_status and rar_code else None

    return Medication(
        rx_number=_str_or_none(afc_info.get("rxnbr")),
        name=_str_or_none(row.get("medicineName")),
        brand_name=_str_or_none(row.get("commonBrandName")),
        instructions=_str_or_none(row.get("consumerInstructions"))
        or _str_or_none(afc_info.get("sigText")),
        prescriber=_str_or_none(row.get("consumerName")),
        ndc=_str_or_none(afc_info.get("lastdispensedndc")),
        region=_str_or_none(afc_info.get("mrnrgn")),
        dea_schedule=_str_or_none(afc_info.get("deacode")),
        prescribed_on=prescribed_on,
        last_refill_date=last_refill,
        last_sold_date=last_sold,
        next_fill_date=next_fill,
        next_fill_eligible_date=next_fill_eligible,
        days_supply=_int_or_none(afc_info.get("dispenseddayssupply")),
        refills_remaining=_str_to_int(row.get("refillsRemaining")),
        copay=_float_or_none(afc_info.get("copay")),
        is_mailable=_str_to_bool(row.get("mailable")),
        is_first_fill=bool(row.get("firstFill")),
        is_new_prescription=bool(row.get("isNewPrescription")),
        is_refill_eligible=bool(row.get("refillEligible")),
        is_as_needed=bool(afc_info.get("prn")) or _has_indicator(row, "PRN"),
        is_compound=bool(afc_info.get("compound")),
        auto_refill_on=bool(row.get("autoRefill")),
        auto_refill_eligible=bool(row.get("autoRefillEligible")),
        is_currently_orderable=is_currently_orderable,
        refill_blocked_reason=blocked_reason,
        drug_info_url=_absolute_drug_info_url(row.get("drugEncyclopediaLink")),
        fill_options=_parse_fill_options(row.get("fillOptions")),
    )


def _parse_fill_options(raw: Any) -> list[MedicationFillOption]:
    """Walk fillOptions[]. Empty list on missing or malformed input."""
    if not isinstance(raw, list):
        return []
    out: list[MedicationFillOption] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append(
            MedicationFillOption(
                delivery_method=_str_or_none(item.get("deliveryMethod")),
                days_supply=_int_or_none(item.get("daysSupply")),
                quantity=_int_or_none(item.get("quantity")),
                plan_pay=_float_or_none(item.get("planPay")),
                estimated_copay=_float_or_none(item.get("estimatedCopay")),
                usual_customary_charge=_float_or_none(item.get("ucCharge")),
                copay_status=_str_or_none(item.get("copaystts")),
                copay_status_detail=_str_or_none(item.get("copaysttsdtl")),
            )
        )
    return out


def _has_indicator(row: dict, key: str) -> bool:
    """Check RxCustomIndicators[] for a "key=value" entry where value is "true"."""
    indicators = row.get("RxCustomIndicators") or []
    if not isinstance(indicators, list):
        return False
    for ind in indicators:
        if not isinstance(ind, dict):
            continue
        if _str_or_none(ind.get("key")) == key:
            return _str_to_bool(ind.get("value"))
    return False


def _absolute_drug_info_url(raw: Any) -> str | None:
    """Promote Kaiser's relative drugEncyclopediaLink to a full URL.

    Returns None for empty / non-string inputs. Already-absolute URLs pass through.
    """
    s = _str_or_none(raw)
    if not s:
        return None
    if s.startswith("http://") or s.startswith("https://"):
        return s
    if s.startswith("/"):
        return f"{BFF_ORIGIN}{s}"
    return f"{BFF_ORIGIN}/{s}"


# --- date / primitive coercers ---


def _parse_date(value: Any) -> str | None:
    """Normalize Kaiser's mixed date strings to ISO YYYY-MM-DD.

    Accepts None, empty string, "N/A", "N/A " (with whitespace), MM/DD/YYYY,
    YYYY-MM-DD, and trailing-Z variants of ISO. Returns None for anything we
    can't parse — never raises.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in _DATE_NULL_TOKENS:
        return None
    # Strip a trailing "Z" so "1970-01-01Z" still parses (same quirk we
    # handle in profile.py).
    candidate = s.rstrip("Z").rstrip()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        return dt.date().isoformat()
    logger.debug("Could not parse date value %r", s)
    return None


def _str_to_bool(value: Any) -> bool:
    """Coerce Kaiser's stringy booleans ('true' / 'false') to real bools.

    Real bool inputs pass through. Anything else (including None) is False.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def _str_to_int(value: Any) -> int | None:
    """Coerce Kaiser's stringy integers ('2', '0') to int. None on failure."""
    if value is None:
        return None
    if isinstance(value, bool):  # bool is a subclass of int — guard explicitly
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
