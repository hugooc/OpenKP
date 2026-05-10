"""Allergy list scraper.

One MCP tool surfaces from this module:

- `list_allergies` — drug, food, and environmental allergies on the patient's
  record. The most common live state is "no known allergies" (empty list +
  `status_code == 0`).

Source: legacy MyChart `/mychartcn/Clinical/Allergies/LoadListData` endpoint.
Mirrors `problems.py` exactly: same auth contract, same envelope shape, same
CSRF + Referer handshake.

Caveat: per-item field names below are inferred from the problems endpoint
(identical envelope) and Epic MyChart conventions. The test patient has no
recorded allergies, so the populated `DataList[i]` shape is not yet observed
live. The parser tolerates both `AllergyItem`/`LocalItem` wrapper conventions
and falls through to the bare entry shape.

Docs: `docs/research/endpoints/allergies.md`
"""

from __future__ import annotations

import logging
import random
from typing import Any

from pydantic import BaseModel, Field

from openkp.scrapers.csrf import fetch_csrf_token
from openkp.scrapers.request import KaiserRequest

logger = logging.getLogger(__name__)

LIST_PATH = "/mychartcn/Clinical/Allergies/LoadListData"
PAGE_REFERER = "https://healthy.kaiserpermanente.org/mychartcn/Clinical/Allergies"

# Same Epic component identifier as the problems endpoint.
COMPONENT_NUMBER = "4"

# AllergiesStatus enum: only `0` observed (test patient with no known
# allergies). We map (DataList empty AND status_code == 0) →
# "no_known_allergies". Non-empty DataList → "recorded". Anything else
# surfaces the raw int and a None status.
STATUS_NO_KNOWN_ALLERGIES = 0


# --- models ---


class Allergy(BaseModel):
    """One entry on the patient's allergy list. Field set is conservative —
    expand once a populated record is observed live."""

    id: str
    name: str | None = None
    date_noted: str | None = None       # Display string, e.g. "1/15/2024"
    action_code: int | None = None
    is_read_only: bool = False
    comments: str | None = None
    reactions: list[str] = Field(default_factory=list)
    severity: str | None = None


class AllergiesResponse(BaseModel):
    """Wrapper around the allergy list with status interpretation."""

    allergies: list[Allergy] = Field(default_factory=list)
    total_count: int = 0
    # Human-readable interpretation of (DataList, AllergiesStatus). One of:
    # "no_known_allergies", "recorded", or None when we can't tell.
    status: str | None = None
    # Raw `AllergiesStatus` int from Kaiser. Surfaced for transparency since
    # the enum isn't fully decoded.
    status_code: int | None = None


# --- public ---


async def fetch_allergies(client: KaiserRequest) -> AllergiesResponse:
    """Fetch the patient's allergy list. One round trip + one CSRF fetch.

    An empty `allergies` list is the typical state for adults — most people
    have no recorded allergies. Per ADR-005, never raise on missing fields.
    """
    csrf = await fetch_csrf_token(client, referer=PAGE_REFERER)
    params = {
        "csn": "undefined",
        "ComponentNumber": COMPONENT_NUMBER,
        "noCache": f"{random.random()}",
    }
    response = await client.post(
        LIST_PATH,
        params=params,
        headers=_api_headers(csrf),
        json={},
    )
    response.raise_for_status()
    return _parse_allergies_response(response.json())


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


def _parse_allergies_response(payload: Any) -> AllergiesResponse:
    """Walk the LoadListData response, produce an `AllergiesResponse`."""
    if not isinstance(payload, dict):
        return AllergiesResponse()

    data_list = payload.get("DataList")
    raw_list: list[Any] = data_list if isinstance(data_list, list) else []

    allergies: list[Allergy] = []
    for entry in raw_list:
        allergy = _parse_allergy(entry)
        if allergy is not None:
            allergies.append(allergy)

    status_code = _int_or_none(payload.get("AllergiesStatus"))
    status = _interpret_status(status_code, len(allergies))

    return AllergiesResponse(
        allergies=allergies,
        total_count=len(allergies),
        status=status,
        status_code=status_code,
    )


def _parse_allergy(entry: Any) -> Allergy | None:
    """One DataList entry → `Allergy`.

    Tries common Kaiser wrapper keys (`LocalItem`, `AllergyItem`) before
    falling back to the bare entry. Field names mirror `problems._parse_problem`
    since the envelopes are structurally identical.
    """
    if not isinstance(entry, dict):
        return None

    item = entry.get("LocalItem")
    if not isinstance(item, dict):
        item = entry.get("AllergyItem")
    if not isinstance(item, dict):
        item = entry

    allergy_id = _str_or_none(item.get("ID"))
    if allergy_id is None:
        return None

    return Allergy(
        id=allergy_id,
        name=_str_or_none(item.get("Name")),
        date_noted=_str_or_none(item.get("FormattedDateNoted")),
        action_code=_int_or_none(item.get("Action")),
        is_read_only=bool(item.get("IsReadOnly")),
        comments=_str_or_none(item.get("Comments")),
        reactions=_parse_reactions(item.get("Reactions")),
        severity=_str_or_none(item.get("Severity")),
    )


def _parse_reactions(raw: Any) -> list[str]:
    """Coerce a Kaiser Reactions field to a list of strings.

    Speculative shape: list of either plain strings or dicts with a `Title`
    or `Name` field (mirroring the `ReactionList` master dropdown shape).
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for r in raw:
        if isinstance(r, str):
            cleaned = r.strip()
            if cleaned:
                out.append(cleaned)
        elif isinstance(r, dict):
            label = _str_or_none(r.get("Title")) or _str_or_none(r.get("Name")) or _str_or_none(r.get("Value"))
            if label:
                out.append(label)
    return out


def _interpret_status(status_code: int | None, count: int) -> str | None:
    """Map (status_code, allergy count) to a human-readable status string."""
    if count > 0:
        return "recorded"
    if status_code == STATUS_NO_KNOWN_ALLERGIES:
        return "no_known_allergies"
    return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None
