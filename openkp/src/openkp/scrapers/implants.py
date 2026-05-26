"""Implanted-devices scraper.

One MCP tool surfaces from this module:

- `list_implants` — the patient's implanted (and explanted) medical devices:
  pacemakers, ICDs, leads, stents, intraocular lenses, orthopedic hardware,
  etc. Each device carries manufacturer, model, serial, UDI, body area,
  laterality, status, and the implant/explant procedure (date + provider).

Source: legacy MyChart `/mychartcn/api/implants/GetImplants`. Same auth + CSRF
contract as `problems.py`. No pagination — Kaiser returns the full device list
in one call.

The response splits into two parts: `implantGroupList` (a body-area ordering
index, where the literal area `"zzz"` is Epic's sentinel sorting
unknown-area devices last) and `implantList` (the authoritative per-device
detail, keyed by device id). We iterate the group list purely for ordering and
pull every field from `implantList`.

Docs: `docs/research/endpoints/implants.md`
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from openkp.scrapers.csrf import fetch_csrf_token
from openkp.scrapers.request import KaiserRequest

logger = logging.getLogger(__name__)

LIST_PATH = "/mychartcn/api/implants/GetImplants"
PAGE_REFERER = "https://healthy.kaiserpermanente.org/mychartcn/app/implants"


# --- models ---


class ImplantProcedure(BaseModel):
    """The implant or explant event for one device."""

    date: str | None = None         # Kaiser display string, e.g. "January 3, 2024"
    date_iso: str | None = None     # derived "YYYY-MM-DD", null if unparseable
    provider: str | None = None
    facility: str | None = None
    device_count: str | None = None  # Kaiser sends this as a string ("1") or empty


class Implant(BaseModel):
    """One implanted or explanted device."""

    id: str
    name: str | None = None
    type: str | None = None          # "Pacemaker", "Cardiac Implant", "Ophthalmology"
    area: str | None = None          # body area, e.g. "Chest", "Eye"; null when unknown
    laterality: str | None = None    # "Left", "Right"
    status: str | None = None        # "Implanted", "Explanted", ...
    is_explant: bool = False
    is_external: bool = False        # record sourced from outside KP
    manufacturer: str | None = None
    model: str | None = None
    serial: str | None = None
    udi: str | None = None           # full UDI barcode string when present
    sdi: str | None = None           # device identifier (GTIN portion of the UDI)
    lot: str | None = None
    comments: list[str] = Field(default_factory=list)     # empty in all observed data
    description: list[str] = Field(default_factory=list)   # empty in all observed data
    implanted: ImplantProcedure | None = None
    explanted: ImplantProcedure | None = None


class ImplantsResponse(BaseModel):
    """The full implanted-devices list."""

    implants: list[Implant] = Field(default_factory=list)
    total_count: int = 0


# --- public ---


async def fetch_implants(client: KaiserRequest) -> ImplantsResponse:
    """Fetch the patient's implanted devices. One CSRF fetch + one round trip.

    Returns an `ImplantsResponse`. An empty `implants` list is a valid outcome
    (patient has no implanted devices on record). Per ADR-005, never raise on
    missing fields — return whatever parses, leave the rest null.
    """
    csrf = await fetch_csrf_token(client, referer=PAGE_REFERER)
    response = await client.post(LIST_PATH, headers=_api_headers(csrf), json={})
    response.raise_for_status()
    return _parse_implants_response(response.json())


# --- private ---


def _api_headers(csrf_token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://healthy.kaiserpermanente.org",
        "Referer": PAGE_REFERER,
        "X-Requested-With": "XMLHttpRequest",
        "__RequestVerificationToken": csrf_token,
    }


def _parse_implants_response(payload: Any) -> ImplantsResponse:
    """Walk the GetImplants response, produce an `ImplantsResponse`."""
    if not isinstance(payload, dict):
        return ImplantsResponse()

    implant_map = payload.get("implantList")
    if not isinstance(implant_map, dict):
        return ImplantsResponse()

    implants: list[Implant] = []
    for dev_id in _ordered_ids(payload.get("implantGroupList"), implant_map):
        implant = _parse_implant(dev_id, implant_map.get(dev_id))
        if implant is not None:
            implants.append(implant)

    return ImplantsResponse(implants=implants, total_count=len(implants))


def _ordered_ids(group_list: Any, implant_map: dict[str, Any]) -> list[str]:
    """Device ids in the portal's body-area order.

    Uses `implantGroupList` for ordering, then appends any device present in
    `implantList` but not referenced by a group (defensive — not observed).
    """
    ordered: list[str] = []
    seen: set[str] = set()

    if isinstance(group_list, list):
        for group in group_list:
            if not isinstance(group, dict):
                continue
            ids = group.get("implantIDs")
            if not isinstance(ids, list):
                continue
            for dev_id in ids:
                if isinstance(dev_id, str) and dev_id in implant_map and dev_id not in seen:
                    ordered.append(dev_id)
                    seen.add(dev_id)

    for dev_id in implant_map:
        if dev_id not in seen:
            ordered.append(dev_id)
            seen.add(dev_id)

    return ordered


def _parse_implant(dev_id: Any, entry: Any) -> Implant | None:
    """One implantList value → `Implant`. Returns None if no usable id."""
    if not isinstance(entry, dict):
        return None

    # Prefer the entry's own id; fall back to the map key.
    implant_id = _str_or_none(entry.get("id")) or _str_or_none(dev_id)
    if implant_id is None:
        return None

    return Implant(
        id=implant_id,
        name=_str_or_none(entry.get("name")),
        type=_str_or_none(entry.get("type")),
        area=_str_or_none(entry.get("area")),
        laterality=_str_or_none(entry.get("laterality")),
        status=_str_or_none(entry.get("status")),
        is_explant=bool(entry.get("isExplant")),
        is_external=bool(entry.get("isExternal")),
        manufacturer=_str_or_none(entry.get("manufacturer")),
        model=_str_or_none(entry.get("model")),
        serial=_str_or_none(entry.get("serial")),
        udi=_str_or_none(entry.get("udi")),
        sdi=_str_or_none(entry.get("sdi")),
        lot=_str_or_none(entry.get("lot")),
        comments=_str_list(entry.get("comments")),
        description=_str_list(entry.get("description")),
        implanted=_parse_procedure(entry.get("implantProcedure")),
        explanted=_parse_procedure(entry.get("explantProcedure")),
    )


def _parse_procedure(raw: Any) -> ImplantProcedure | None:
    """Parse an implant/explant procedure block.

    Kaiser always sends both blocks even when nothing happened (every field an
    empty string). We collapse an all-empty block to None.
    """
    if not isinstance(raw, dict):
        return None

    date = _str_or_none(raw.get("isoDate"))  # misnomer: it's a display string
    provider = _str_or_none(raw.get("provider"))
    facility = _str_or_none(raw.get("facility"))
    device_count = _str_or_none(raw.get("deviceCount"))

    if date is None and provider is None and facility is None and device_count is None:
        return None

    return ImplantProcedure(
        date=date,
        date_iso=_display_date_to_iso(date),
        provider=provider,
        facility=facility,
        device_count=device_count,
    )


def _display_date_to_iso(value: str | None) -> str | None:
    """"January 3, 2024" → "2024-01-03". None on anything unparseable."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%B %d, %Y").date().isoformat()
    except (ValueError, TypeError):
        return None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [s.strip() for s in value if isinstance(s, str) and s.strip()]


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None
