"""Care team scraper.

One MCP tool surfaces from this module:

- `list_care_team` — the patient's "Care Team and Recent Providers" roster:
  PCP, specialists, and recently-seen clinicians, each with specialty,
  relationship label, and per-provider capability flags (messageable,
  directly schedulable).

Source: legacy MyChart `/mychartcn/Clinical/CareTeam/Load` (internal KP
providers) and `/mychartcn/Clinical/CareTeam/LoadExternal` (non-KP
providers). Same auth + CSRF contract as `problems.py` — a single anti-forgery
token covers both POSTs (Kaiser reuses one token for the whole page). No
pagination: Kaiser returns the full roster in one call each.

This is a strict superset of `get_profile`'s PCP field — that surface gives
only the primary care provider, this one gives the whole care relationship
roster plus what you can do with each provider.

Docs: `docs/research/endpoints/care_team.md`
"""

from __future__ import annotations

import logging
import random
from typing import Any

import httpx
from pydantic import BaseModel, Field

from openkp.scrapers.csrf import fetch_csrf_token
from openkp.scrapers.request import KaiserRequest

logger = logging.getLogger(__name__)

LOAD_PATH = "/mychartcn/Clinical/CareTeam/Load"
LOAD_EXTERNAL_PATH = "/mychartcn/Clinical/CareTeam/LoadExternal"
PAGE_REFERER = "https://healthy.kaiserpermanente.org/mychartcn/clinical/careteam"

# Epic component identifier observed in the captured request. Hardcoded by
# Kaiser's front end — value is stable.
COMPONENT_NUMBER = "2"


# --- models ---


class CareTeamProvider(BaseModel):
    """One clinician on the patient's care team or recent-providers list."""

    id: str
    name: str | None = None
    specialty: str | None = None        # e.g. "Family Practice", "Cardiology"
    relation: str | None = None         # e.g. "Primary Care Provider", "Cardiologist"
    department_id: str | None = None    # opaque Epic handle; pairs with scheduling
    is_external: bool = False           # True == non-KP provider (from LoadExternal)
    # Capability flags from the care-team panel's OWN inline action buttons.
    # They describe what KP's portal UI offers on this panel, NOT what OpenKP's
    # other tools can do. In particular `can_message` is the panel's
    # quick-message button — it is NOT a gate on reachability. Messaging runs
    # through a separate surface (list_message_recipients + send_message), where
    # a provider with can_message=False here may still be a valid recipient.
    # Same caveat applies to can_schedule / can_request_appointment.
    can_message: bool = False
    can_schedule: bool = False
    can_request_appointment: bool = False
    can_view_details: bool = False
    photo_url: str | None = None
    provider_page_url: str | None = None  # public mydoctor.kaiserpermanente.org bio
    care_team_status: int | None = None   # raw int enum; 0 observed for all


class CareTeamResponse(BaseModel):
    """The full care team roster: internal KP providers plus any external ones."""

    providers: list[CareTeamProvider] = Field(default_factory=list)
    total_count: int = 0


# --- public ---


async def fetch_care_team(client: KaiserRequest) -> CareTeamResponse:
    """Fetch the patient's care team roster. One CSRF fetch + two round trips.

    Calls the internal-provider endpoint (the primary, always-present source)
    then the external-provider endpoint. The external call is best-effort: if
    it fails we still return the internal roster rather than losing everything.
    Per ADR-005, never raise on missing fields — return whatever parses, leave
    the rest null. An empty roster is a valid outcome.
    """
    csrf = await fetch_csrf_token(client, referer=PAGE_REFERER)

    internal_payload = await _load(client, LOAD_PATH, csrf, extra_params={"isPrimaryStandalone": "true"})
    providers = _parse_providers(internal_payload)

    try:
        external_payload = await _load(client, LOAD_EXTERNAL_PATH, csrf)
        providers.extend(_parse_providers(external_payload))
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("CareTeam LoadExternal failed, returning internal roster only: %s", exc)

    return CareTeamResponse(providers=providers, total_count=len(providers))


# --- private ---


async def _load(
    client: KaiserRequest,
    path: str,
    csrf_token: str,
    extra_params: dict[str, str] | None = None,
) -> Any:
    """POST one CareTeam Load endpoint and return its parsed JSON body.

    The captured requests carry no request body — these are GET-shaped POSTs
    driven entirely by query params and the anti-forgery token header.
    """
    params = {
        "hfrId": "",
        "sources": "",
        "actions": "",
        "ComponentNumber": COMPONENT_NUMBER,
        "noCache": f"{random.random()}",
    }
    if extra_params:
        params.update(extra_params)
    response = await client.post(path, params=params, headers=_api_headers(csrf_token))
    response.raise_for_status()
    return response.json()


def _api_headers(csrf_token: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Origin": "https://healthy.kaiserpermanente.org",
        "Referer": PAGE_REFERER,
        "X-Requested-With": "XMLHttpRequest",
        "__RequestVerificationToken": csrf_token,
    }


def _parse_providers(payload: Any) -> list[CareTeamProvider]:
    """Walk a CareTeam Load response, produce a list of providers."""
    if not isinstance(payload, dict):
        return []

    raw_list = payload.get("ProvidersList")
    if not isinstance(raw_list, list):
        return []

    providers: list[CareTeamProvider] = []
    for entry in raw_list:
        provider = _parse_provider(entry)
        if provider is not None:
            providers.append(provider)
    return providers


def _parse_provider(entry: Any) -> CareTeamProvider | None:
    """One ProvidersList entry → `CareTeamProvider`. Returns None if no ID."""
    if not isinstance(entry, dict):
        return None

    provider_id = _str_or_none(entry.get("ID"))
    if provider_id is None:
        return None

    return CareTeamProvider(
        id=provider_id,
        name=_str_or_none(entry.get("Name")),
        specialty=_str_or_none(entry.get("Specialty")),
        relation=_str_or_none(entry.get("Relation")),
        department_id=_str_or_none(entry.get("DepartmentID")),
        is_external=bool(entry.get("IsExternal")),
        can_message=bool(entry.get("CanMessage")),
        can_schedule=bool(entry.get("CanDirectSchedule")),
        can_request_appointment=bool(entry.get("CanRequestAppointment")),
        can_view_details=bool(entry.get("CanViewProviderDetails")),
        photo_url=_str_or_none(entry.get("Photo")),
        provider_page_url=_str_or_none(entry.get("WebPageUrl")),
        care_team_status=_int_or_none(entry.get("CareTeamStatus")),
    )


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        # bools are ints in Python; we don't want True → 1 here.
        return None
    if isinstance(value, int):
        return value
    return None
