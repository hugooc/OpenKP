"""Appointments scraper.

Two MCP tools surface from this module:

- `list_appointments` — upcoming and in-progress visits. Answers
  "when is my next Kaiser appointment".
- `list_past_visits` — completed visits, paginated newest-first.
  Answers "how many appointments did I have in 2025".

Sources:
- Upcoming: legacy MyChart `/mychartcn/Visits/VisitsList/LoadUpcoming`.
  Single call, no pagination.
- Past: legacy MyChart `/mychartcn/Visits/VisitsList/LoadPast`. Serialized-
  cursor pagination, ~10 visits per page.

Both share the same auth + CSRF contract as `problems.py` and `messages.py`.

Docs: `docs/research/endpoints/appointments.md`
"""

from __future__ import annotations

import logging
import random
import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from openkp.scrapers.csrf import fetch_csrf_token
from openkp.scrapers.request import KaiserRequest

logger = logging.getLogger(__name__)

UPCOMING_PATH = "/mychartcn/Visits/VisitsList/LoadUpcoming"
PAST_PATH = "/mychartcn/Visits/VisitsList/LoadPast"
PAGE_REFERER = "https://healthy.kaiserpermanente.org/mychartcn/Visits"

COMPONENT_NUMBER = "5"
PAST_COMPONENT_NUMBER = "7"
DEFAULT_TIMEZONE = "America/Los_Angeles"

# Hard cap on pages we'll walk in one call regardless of the caller's
# `max_pages` request — defensive against infinite-loop bugs in the
# pagination cursor logic.
MAX_PAGES_HARD_CAP = 60

# Kaiser's web UI defaults to 10 visits per LoadPast call but supports a
# `numVisitsToRetrieve` query param. Observed to honor 43 and 78 exactly
# in HAR captures from the filter UI (session 15). Defaulting to 50 cuts
# round trips by 5x for multi-year history walks. Hard-capped at 78
# because that's the largest value we've verified Kaiser accepts.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE_HARD_CAP = 78

# Kaiser embeds timestamps in legacy ASP.NET JSON Date format:
#   "/Date(1779137400000)/"
# The number is Unix epoch milliseconds, UTC.
_NET_DATE_RE = re.compile(r"^/Date\((-?\d+)\)/$")


# --- models ---


class AppointmentDepartment(BaseModel):
    """Where the visit happens. Same shape on past and upcoming visits."""

    id: str | None = None
    name: str | None = None
    address: list[str] = Field(default_factory=list)
    phone: str | None = None
    specialty: str | None = None
    timezone: str | None = None


class AppointmentProvider(BaseModel):
    """Who's seeing the patient."""

    id: str | None = None
    name: str | None = None
    photo_url: str | None = None
    web_page_url: str | None = None
    department: AppointmentDepartment | None = None


class Appointment(BaseModel):
    """One scheduled visit. Fields are normalized; the underlying Kaiser
    payload has ~120 fields, most of them UI capability flags we don't
    surface here.
    """

    id: str | None = None
    csn: str | None = None
    instant_iso: str | None = None       # ISO-8601 from the legacy /Date(ms)/ field
    date_display: str | None = None      # "Monday May 18, 2026"
    time_display: str | None = None      # "1:50 PM" (None when no time, e.g. refills)
    timezone: str | None = None          # "PDT"
    visit_type: str | None = None        # "Office Visit", "Refill", "Surgery", ...
    encounter_type: int | None = None    # Epic enum (1 = Office Visit, 3 = Refill, ...)
    primary_provider: AppointmentProvider | None = None
    other_providers: list[AppointmentProvider] = Field(default_factory=list)
    department: AppointmentDepartment | None = None
    is_telemedicine: bool = False
    is_in_progress: bool = False
    can_reschedule: bool = False
    can_cancel: bool = False
    arrival_time: str | None = None
    duration_minutes: int | None = None
    copay: float | None = None
    is_echeckin_completed: bool = False


class AppointmentsResponse(BaseModel):
    """Wrapper around the upcoming-visit list."""

    appointments: list[Appointment] = Field(default_factory=list)
    total_count: int = 0


class PastVisit(BaseModel):
    """One completed (or canceled / no-show) visit.

    Shares most fields with `Appointment` but adds past-specific status
    flags (canceled, no-show, viewed) and the rough recency bucket Kaiser
    assigns. The capability flags carried by upcoming visits
    (`can_reschedule`, `can_cancel`) are not relevant here and are omitted.
    """

    id: str | None = None
    csn: str | None = None
    instant_iso: str | None = None
    date_display: str | None = None
    time_display: str | None = None
    timezone: str | None = None
    visit_type: str | None = None        # "Office Visit", "Refill", "Surgery", ...
    encounter_type: int | None = None    # NOT a clean type discriminator: Office Visit AND Refill both = 3 in observed data; trust visit_type for human-readable categorization
    primary_provider: AppointmentProvider | None = None
    other_providers: list[AppointmentProvider] = Field(default_factory=list)
    department: AppointmentDepartment | None = None
    is_telemedicine: bool = False        # heuristic; see _is_telemedicine
    is_canceled: bool = False
    is_no_show: bool = False
    left_without_seen: bool = False
    is_unread: bool = False              # Kaiser's "new clinical info" indicator (IsNotViewed)
    has_visit_summary: bool = False      # AVS / patient instructions document available
    has_clinical_note: bool = False
    bucket: str | None = None            # "past6month", "past1year", "past2year", ...
    copay: float | None = None
    is_fully_paid: bool = False


class PastVisitsResponse(BaseModel):
    """Paginated past-visit list."""

    visits: list[PastVisit] = Field(default_factory=list)
    total_count: int = 0
    pages_walked: int = 0
    has_more: bool = False               # True if Kaiser still has older visits we didn't fetch
    oldest_instant_iso: str | None = None  # ISO of the oldest visit in `visits`, useful as a cursor


# --- public ---


async def fetch_appointments(client: KaiserRequest) -> AppointmentsResponse:
    """Fetch upcoming + in-progress visits. One round trip + one CSRF fetch.

    Returns an `AppointmentsResponse`. An empty `appointments` list is a
    valid outcome (patient has nothing on the books). Per ADR-005, never
    raise on missing fields — return whatever we can parse, leave the
    rest as null.
    """
    csrf = await fetch_csrf_token(client, referer=PAGE_REFERER)
    params = {
        "timeZone": DEFAULT_TIMEZONE,
        "ComponentNumber": COMPONENT_NUMBER,
        "noCache": f"{random.random()}",
    }
    response = await client.post(
        UPCOMING_PATH,
        params=params,
        headers=_api_headers(csrf),
    )
    response.raise_for_status()
    return _parse_upcoming_response(response.json())


async def fetch_past_visits(
    client: KaiserRequest,
    *,
    max_pages: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    until_iso: str | None = None,
) -> PastVisitsResponse:
    """Fetch past visits, walking Kaiser's pagination cursor as needed.

    Past visits arrive newest-first. Pagination uses two parameters that
    the caller of this function never sees:

    - `oldestRenderedDate` (query): ISO timestamp of the oldest visit
      seen so far. We seed with `now` and update it from the last visit
      of each page.
    - `serializedIndex` (form body): opaque cursor blob from the previous
      response's top-level `SerializedIndex`. Empty on the first page.

    Stops walking when any of:
      - `max_pages` reached (default 1, hard-capped at MAX_PAGES_HARD_CAP).
      - Kaiser reports no more data (HasMoreData false on every org).
      - `until_iso` is provided AND the oldest visit on the just-fetched
        page is older than that timestamp. We still return that page;
        callers can filter by `instant_iso` if they want strict bounds.

    `page_size` controls how many visits Kaiser returns per call via the
    `numVisitsToRetrieve` query param. Default 50 (5x the front end's
    default of 10), hard-capped at 78 (the highest value we've observed
    Kaiser accept). Higher values mean fewer round trips but bigger
    response bodies.

    Returns a `PastVisitsResponse`. `has_more=True` means Kaiser had more
    older visits we didn't fetch (call again with a larger `max_pages`).
    """
    if max_pages < 1:
        max_pages = 1
    if max_pages > MAX_PAGES_HARD_CAP:
        max_pages = MAX_PAGES_HARD_CAP
    if page_size < 1:
        page_size = 1
    if page_size > MAX_PAGE_SIZE_HARD_CAP:
        page_size = MAX_PAGE_SIZE_HARD_CAP

    csrf = await fetch_csrf_token(client, referer=PAGE_REFERER)

    visits: list[PastVisit] = []
    serialized_index = ""
    oldest_rendered_date = _format_iso_for_query(datetime.now(timezone.utc))
    has_more = True
    pages_walked = 0

    while pages_walked < max_pages and has_more:
        params = {
            "loadpast": "1",
            "searchString": "",
            "oldestRenderedDate": oldest_rendered_date,
            "numVisitsToRetrieve": str(page_size),
            "ComponentNumber": PAST_COMPONENT_NUMBER,
            "noCache": f"{random.random()}",
        }
        response = await client.post(
            PAST_PATH,
            params=params,
            headers=_api_headers_form(csrf),
            content=f"serializedIndex={serialized_index}",
        )
        response.raise_for_status()
        page = _parse_past_response(response.json())
        pages_walked += 1

        visits.extend(page.visits)
        has_more = page.has_more
        serialized_index = page.next_serialized_index

        # Compute next page's oldestRenderedDate from the last visit on
        # this page (oldest one — Kaiser returns newest-first within the
        # page).
        if page.oldest_instant:
            oldest_rendered_date = _format_iso_for_query(page.oldest_instant)
        else:
            # No visits on this page — Kaiser said done, stop walking.
            break

        # Caller wants to stop once we cross `until_iso`. Compare against
        # the page's oldest visit, which is also the next request's lower
        # bound.
        if until_iso is not None and page.oldest_instant_iso is not None:
            if page.oldest_instant_iso < until_iso:
                break

    oldest = None
    for v in visits:
        if v.instant_iso is None:
            continue
        if oldest is None or v.instant_iso < oldest:
            oldest = v.instant_iso

    return PastVisitsResponse(
        visits=visits,
        total_count=len(visits),
        pages_walked=pages_walked,
        has_more=has_more,
        oldest_instant_iso=oldest,
    )


# --- private ---


def _api_headers(csrf_token: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Origin": "https://healthy.kaiserpermanente.org",
        "Referer": PAGE_REFERER,
        "X-Requested-With": "XMLHttpRequest",
        "__RequestVerificationToken": csrf_token,
    }


def _api_headers_form(csrf_token: str) -> dict[str, str]:
    """LoadPast wants form-encoded body, not JSON."""
    return {
        **_api_headers(csrf_token),
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }


class _PastPage(BaseModel):
    """Internal wrapper for one decoded LoadPast response."""

    visits: list[PastVisit] = Field(default_factory=list)
    has_more: bool = False
    next_serialized_index: str = ""
    oldest_instant: datetime | None = None
    oldest_instant_iso: str | None = None


def _parse_upcoming_response(payload: Any) -> AppointmentsResponse:
    """Walk the LoadUpcoming response, produce an `AppointmentsResponse`.

    The response splits visits into three buckets — `InProgressVisits`,
    `NextNDaysVisits`, `LaterVisitsList` — that we flatten into one list
    in chronological order (in-progress, near, later).
    """
    if not isinstance(payload, dict):
        return AppointmentsResponse()

    appointments: list[Appointment] = []
    for key in ("InProgressVisits", "NextNDaysVisits", "LaterVisitsList"):
        bucket = payload.get(key)
        if not isinstance(bucket, list):
            continue
        for entry in bucket:
            appt = _parse_appointment(entry, is_in_progress=(key == "InProgressVisits"))
            if appt is not None:
                appointments.append(appt)

    return AppointmentsResponse(appointments=appointments, total_count=len(appointments))


def _parse_appointment(entry: Any, *, is_in_progress: bool = False) -> Appointment | None:
    if not isinstance(entry, dict):
        return None

    csn = _str_or_none(entry.get("Csn"))
    appt_id = _str_or_none(entry.get("Id")) or csn
    if appt_id is None:
        return None

    return Appointment(
        id=appt_id,
        csn=csn,
        instant_iso=_parse_net_date(entry.get("Instant")),
        date_display=_str_or_none(entry.get("Date")),
        time_display=_str_or_none(entry.get("Time")),
        timezone=_str_or_none(entry.get("TimeZone")),
        visit_type=_str_or_none(entry.get("VisitTypeName")),
        encounter_type=_int_or_none(entry.get("EncounterType")),
        primary_provider=_parse_provider(entry.get("PrimaryProvider")),
        other_providers=[
            p for p in (_parse_provider(x) for x in (entry.get("OtherProviders") or []))
            if p is not None
        ],
        department=_parse_department(entry.get("PrimaryDepartment")),
        is_telemedicine=_is_telemedicine(entry),
        is_in_progress=is_in_progress or bool(entry.get("InProgress")),
        can_reschedule=bool(entry.get("IsRescheduleEnabled")),
        can_cancel=bool(entry.get("IsDirectCancelEnabled") or entry.get("IsRequestCancelEnabled")),
        arrival_time=_str_or_none(entry.get("ArrivalTime")),
        duration_minutes=_int_or_none(entry.get("DurationInMinutes")),
        copay=_float_or_none(entry.get("Copay")),
        is_echeckin_completed=bool(entry.get("IsEcheckInCompleted")),
    )


def _parse_past_response(payload: Any) -> _PastPage:
    """Walk the LoadPast response.

    Shape: `payload["List"]` is a dict keyed by org_id; each value is a
    dict with `List` (the visits array), `HasMoreData`, and a per-org
    `SerializedIndex`. The top-level `payload["SerializedIndex"]` is the
    cursor we feed into the next request's body.

    Real accounts return one org for in-network NorCal patients; the
    parser handles multi-org gracefully (concatenates visits, ORs the
    has-more flags) but we don't have multi-org test data.
    """
    if not isinstance(payload, dict):
        return _PastPage()

    list_block = payload.get("List")
    if not isinstance(list_block, dict):
        return _PastPage()

    visits: list[PastVisit] = []
    has_more = False
    oldest_dt: datetime | None = None
    oldest_iso: str | None = None

    for org_block in list_block.values():
        if not isinstance(org_block, dict):
            continue
        if org_block.get("HasMoreData"):
            has_more = True
        org_visits = org_block.get("List")
        if not isinstance(org_visits, list):
            continue
        for entry in org_visits:
            visit = _parse_past_visit(entry)
            if visit is None:
                continue
            visits.append(visit)
            # Track oldest instant across all orgs for the next page's
            # `oldestRenderedDate`. We need the parsed datetime, not just
            # the ISO string, so compute it from the raw `Instant` field.
            instant_dt = _parse_net_date_to_datetime(
                entry.get("Instant") if isinstance(entry, dict) else None
            )
            if instant_dt is not None:
                if oldest_dt is None or instant_dt < oldest_dt:
                    oldest_dt = instant_dt
                    oldest_iso = visit.instant_iso

    next_index = _str_or_none(payload.get("SerializedIndex")) or ""

    return _PastPage(
        visits=visits,
        has_more=has_more,
        next_serialized_index=next_index,
        oldest_instant=oldest_dt,
        oldest_instant_iso=oldest_iso,
    )


def _parse_past_visit(entry: Any) -> PastVisit | None:
    if not isinstance(entry, dict):
        return None

    csn = _str_or_none(entry.get("Csn"))
    visit_id = _str_or_none(entry.get("Id")) or csn
    if visit_id is None:
        return None

    return PastVisit(
        id=visit_id,
        csn=csn,
        instant_iso=_parse_net_date(entry.get("Instant")),
        date_display=_str_or_none(entry.get("Date")),
        time_display=_str_or_none(entry.get("Time")),
        timezone=_str_or_none(entry.get("TimeZone")),
        visit_type=_str_or_none(entry.get("VisitTypeName")),
        encounter_type=_int_or_none(entry.get("EncounterType")),
        primary_provider=_parse_provider(entry.get("PrimaryProvider")),
        other_providers=[
            p for p in (_parse_provider(x) for x in (entry.get("OtherProviders") or []))
            if p is not None
        ],
        department=_parse_department(entry.get("PrimaryDepartment")),
        is_telemedicine=_is_telemedicine(entry),
        is_canceled=bool(entry.get("IsCanceled")),
        is_no_show=bool(entry.get("IsNoShow")),
        left_without_seen=bool(entry.get("LeftWithoutSeen")),
        is_unread=bool(entry.get("IsNotViewed")),
        has_visit_summary=bool(
            entry.get("HasDownloadSummaryLink")
            or entry.get("IsVisitSummaryEnabled")
        ),
        has_clinical_note=bool(entry.get("IsClinicalNoteAvailable")),
        bucket=_str_or_none(entry.get("PastVisitBucket")),
        copay=_float_or_none(entry.get("Copay")),
        is_fully_paid=bool(entry.get("IsFullyPaid")),
    )


def _parse_provider(entry: Any) -> AppointmentProvider | None:
    if not isinstance(entry, dict):
        return None
    return AppointmentProvider(
        id=_str_or_none(entry.get("EncryptedId")),
        name=_str_or_none(entry.get("Name")),
        photo_url=_str_or_none(entry.get("PhotoUrl")),
        web_page_url=_str_or_none(entry.get("WebPageUrl")),
        department=_parse_department(entry.get("Department")),
    )


def _parse_department(entry: Any) -> AppointmentDepartment | None:
    if not isinstance(entry, dict):
        return None
    address = entry.get("Address")
    if not isinstance(address, list):
        address = []
    specialty_dict = entry.get("Specialty")
    specialty = None
    if isinstance(specialty_dict, dict):
        specialty = _str_or_none(specialty_dict.get("Title"))
    return AppointmentDepartment(
        id=_str_or_none(entry.get("Id")),
        name=_str_or_none(entry.get("Name")),
        address=[a for a in (_str_or_none(x) for x in address) if a is not None],
        phone=_clean_phone(entry.get("PhoneNumber")),
        specialty=specialty,
        timezone=_str_or_none(entry.get("TimeZone")),
    )


def _is_telemedicine(entry: dict) -> bool:
    """Telemedicine signal lives in three places. Any of them is enough."""
    if entry.get("Telemedicine"):
        return True
    if entry.get("EVisit"):
        return True
    if entry.get("CanShowTelemedicine"):
        return True
    return False


def _parse_net_date(value: Any) -> str | None:
    """Convert legacy ASP.NET JSON Date format to ISO-8601 UTC.

    `/Date(1779137400000)/` → `"2026-05-18T20:50:00+00:00"`. Returns None
    for anything that doesn't match the expected shape.
    """
    dt = _parse_net_date_to_datetime(value)
    if dt is None:
        return None
    return dt.isoformat()


def _parse_net_date_to_datetime(value: Any) -> datetime | None:
    """Same as `_parse_net_date` but returns the datetime object."""
    if not isinstance(value, str):
        return None
    match = _NET_DATE_RE.match(value)
    if not match:
        return None
    try:
        ms = int(match.group(1))
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _format_iso_for_query(dt: datetime) -> str:
    """Format a UTC datetime the way Kaiser's front end does for the
    `oldestRenderedDate` query param: ISO-8601 with millisecond precision
    and a literal `Z` suffix. Example: `2026-05-04T15:50:23.129Z`.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    ms = dt.microsecond // 1000
    return f"{base}.{ms:03d}Z"


def _clean_phone(value: Any) -> str | None:
    """Kaiser wraps phones in unicode bidi marks (U+202A / U+202C). Strip them."""
    s = _str_or_none(value)
    if s is None:
        return None
    cleaned = s.replace("‪", "").replace("‬", "").strip()
    return cleaned or None


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


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None
