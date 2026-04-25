"""Emergency-contact + healthcare-agent scraper.

Closes Phase 2 read tools. Returns the user's relationship roster — emergency
contacts, DPOAHC healthcare agents, conservators, "people in your life." Kaiser
serves them all from a single Epic/MyChart endpoint; we return the full list
and let `is_active_healthcare_agent` / `legal_role` disambiguate.

Endpoint: POST /mychartcn/Demographics/Relationships/GetRelationshipList
Docs:     docs/research/endpoints/emergency_contacts.md
"""

from __future__ import annotations

import logging
import random
from typing import Any

from pydantic import BaseModel

from openkp.scrapers.csrf import fetch_csrf_token
from openkp.scrapers.request import KaiserRequest

logger = logging.getLogger(__name__)

RELATIONSHIPS_PATH = "/mychartcn/Demographics/Relationships/GetRelationshipList"
RELATIONSHIPS_REFERER = "https://healthy.kaiserpermanente.org/mychartcn/AdvancedCarePlanning"

# Form body Kaiser's controller sends. `getEOLDocs=true` asks the server to
# include End-of-Life document references in the response (we don't surface
# those today, but the field is harmless to leave on).
_REQUEST_BODY = b"getEOLDocs=true&disableUTF8=true"

# Kaiser splits relationship lookups into three categories:
#   1000 = social/family roles (Spouse, Daughter, Neighbor, ...)
#   1107 = legal designation (DPOAHC Primary/Alternate, Conservator, DDM)
#   1113 = legal designation (same items as 1107 in observed data)
_RELATIONSHIP_CATEGORY_ID = "1000"
_LEGAL_CATEGORY_IDS = ("1113", "1107")


# --- models ---


class ContactAddress(BaseModel):
    """A single contact's mailing address. Flat projection of Kaiser's nested AddressViewModel."""

    street1: str | None = None
    street2: str | None = None      # picks Unit > Floor > Building from Kaiser's nested fields
    city: str | None = None
    state: str | None = None        # two-letter abbreviation
    postal_code: str | None = None


class EmergencyContact(BaseModel):
    """One emergency contact / healthcare agent / person-in-your-life entry."""

    name: str

    relationship: str | None = None        # e.g. "Spouse", "Daughter"
    legal_role: str | None = None          # e.g. "DPOAHC - Primary Agent"; null for non-legal roles
    is_active_healthcare_agent: bool = False
    is_primary_contact: bool = False

    home_phone: str | None = None
    work_phone: str | None = None
    mobile_phone: str | None = None
    email: str | None = None

    address: ContactAddress | None = None


# --- fetcher ---


async def fetch_emergency_contacts(client: KaiserRequest) -> list[EmergencyContact]:
    """Fetch the patient's relationship roster (emergency contacts + healthcare agents).

    Two-step CSRF dance, identical to the PCP fetch in `profile.py`. Never
    raises on partial / missing fields — returns whatever contacts parse
    cleanly. A failure at the HTTP layer propagates so the caller (typically
    `fetch_profile`) can decide whether to swallow it.
    """
    token = await fetch_csrf_token(client, referer=RELATIONSHIPS_REFERER)
    params = {"noCache": f"{random.random()}"}
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": RELATIONSHIPS_REFERER,
        "X-Requested-With": "XMLHttpRequest",
        "__RequestVerificationToken": token,
    }
    response = await client.post(
        RELATIONSHIPS_PATH,
        params=params,
        headers=headers,
        content=_REQUEST_BODY,
    )
    response.raise_for_status()
    return _parse_relationships(response.json())


# --- parser ---


def _parse_relationships(payload: Any) -> list[EmergencyContact]:
    """Walk the GetRelationshipList response, project each entry to EmergencyContact."""
    if not isinstance(payload, dict):
        return []
    relationships = payload.get("Relationships")
    if not isinstance(relationships, list):
        return []

    relationship_lookup = _build_lookup(payload, _RELATIONSHIP_CATEGORY_ID)
    legal_lookup = _build_legal_lookup(payload)

    out: list[EmergencyContact] = []
    for item in relationships:
        contact = _parse_one(item, relationship_lookup, legal_lookup)
        if contact is not None:
            out.append(contact)
    return out


def _parse_one(
    item: Any,
    relationship_lookup: dict[str, str],
    legal_lookup: dict[str, str],
) -> EmergencyContact | None:
    if not isinstance(item, dict):
        return None
    name = _resolve_name(item)
    if name is None:
        return None
    return EmergencyContact(
        name=name,
        relationship=_resolve_lookup(item.get("RelationToPatient"), relationship_lookup),
        legal_role=_resolve_lookup(item.get("LegalRelationToPatient"), legal_lookup),
        is_active_healthcare_agent=bool(item.get("IsActiveHealthCareAgent")),
        is_primary_contact=bool(item.get("IsPrimary")),
        home_phone=_field_value(item.get("HomePhone")),
        work_phone=_field_value(item.get("WorkPhone")),
        mobile_phone=_field_value(item.get("MobilePhone")),
        email=_field_value(item.get("Email")),
        address=_parse_address(item.get("AddressViewModel")),
    )


def _resolve_name(item: dict[str, Any]) -> str | None:
    """Prefer FirstName + LastName; fall back to FormattedName for legacy entries.

    `FormattedName` can carry user-authored annotations like `"CALL FIRST) <name> (URGENT"` —
    we surface it verbatim because parsing that intent isn't our job.
    """
    first = _str_or_none(item.get("FirstName"))
    last = _str_or_none(item.get("LastName"))
    if first and last:
        return f"{first} {last}"
    if first:
        return first
    if last:
        return last
    return _str_or_none(item.get("FormattedName"))


def _field_value(field: Any) -> str | None:
    """Kaiser wraps each field as `{FieldId, Value}`; pull `Value`, treat empty as null."""
    if not isinstance(field, dict):
        return None
    return _str_or_none(field.get("Value"))


def _resolve_lookup(field: Any, lookup: dict[str, str]) -> str | None:
    """Resolve a `{FieldId, Value: "<code>"}` field against a category lookup.

    Returns the human-readable Title from the lookup, or `null` if the value
    is empty or the code isn't in the lookup. We never return the raw code —
    a number like `"7"` is meaningless to a caller without the dictionary.
    """
    code = _field_value(field)
    if code is None:
        return None
    return lookup.get(code)


def _build_lookup(payload: dict[str, Any], category_id: str) -> dict[str, str]:
    category = (payload.get("CategoryData") or {}).get(category_id)
    if not isinstance(category, dict):
        return {}
    items = category.get("CategoryItems")
    if not isinstance(items, list):
        return {}
    out: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        value = _str_or_none(item.get("Value"))
        title = _str_or_none(item.get("Title"))
        if value and title:
            out[value] = title
    return out


def _build_legal_lookup(payload: dict[str, Any]) -> dict[str, str]:
    """Merge the two observed legal-relationship category dictionaries.

    `1107` and `1113` carry the same items in observed data, but Kaiser
    could (in principle) ship one and not the other. Merging is safer than
    picking one — and 1113 wins on conflict because that's the category
    referenced by the live capture.
    """
    merged: dict[str, str] = {}
    for cat_id in reversed(_LEGAL_CATEGORY_IDS):
        merged.update(_build_lookup(payload, cat_id))
    return merged


def _parse_address(raw: Any) -> ContactAddress | None:
    """Project Kaiser's AddressViewModel down to our 5-field flat address.

    Returns `None` when `Street` is empty — an all-null address object
    would be noise. State arrives as a nested `{Number, Title, Abbreviation}`
    object; we project the two-letter abbreviation.
    """
    if not isinstance(raw, dict):
        return None
    street1 = _str_or_none(raw.get("Street"))
    if street1 is None:
        return None
    return ContactAddress(
        street1=street1,
        street2=_first_present(raw, ("Unit", "Floor", "Building")),
        city=_str_or_none(raw.get("City")),
        state=_state_abbreviation(raw.get("State")),
        postal_code=_str_or_none(raw.get("Zip")),
    )


def _first_present(raw: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = _str_or_none(raw.get(k))
        if v:
            return v
    return None


def _state_abbreviation(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    return _str_or_none(raw.get("Abbreviation"))


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None
