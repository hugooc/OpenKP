# Appointments endpoints

Source: `docs/research/captures/kp-appointments-with-sensitive-data.har` (HAR, request side only — Chrome stripped response bodies as the network panel evicted older entries) and `docs/research/captures/recon-appointments-*.json` (full responses, captured 2026-05-04 by `openkp/scripts/recon_appointments.py`).

The Visits landing page at `/mychartcn/Visits` fires four XHRs on load:

| Endpoint | Purpose | Used by OpenKP |
| --- | --- | --- |
| `POST /mychartcn/Visits/VisitsList/VisitsHeaderOptions` | Filter chips (single-org NorCal in our data) | ⚪ Not used. |
| `POST /mychartcn/Visits/VisitsList/LoadAppointmentRequest` | Pending appointment requests / "ready to schedule" cards | ⚪ Not used. Empty array in our recon data. |
| `POST /mychartcn/Visits/VisitsList/LoadFilterOptions` | Filter dropdowns (providers, departments, specialties) + sorted past-visit DAT index | ⚪ Not used. See "Filter index" section below. |
| `POST /mychartcn/Visits/VisitsList/LoadUpcoming` | Upcoming + in-progress visits | ✅ Used by `list_appointments`. |
| `POST /mychartcn/Visits/VisitsList/LoadPast` | Past visits, paginated via serialized cursor | ✅ Used by `list_past_visits`. |
| `GET /mychartcn/Visits/visitdetails?csn=<CSN>` | Per-visit detail page (HTML, not JSON) | ⚪ Not used. The list response already carries the fields we care about. |

MCP tools registered (v1): `list_appointments` (single round trip, no pagination) and `list_past_visits(max_pages=N, until_iso=...)` (paginated walker).

## Required headers

Same contract as `problems.py` and the rest of the legacy `/mychartcn/` family:

```
Accept: application/json, text/javascript, */*; q=0.01
Origin: https://healthy.kaiserpermanente.org
Referer: https://healthy.kaiserpermanente.org/mychartcn/Visits
X-Requested-With: XMLHttpRequest
__RequestVerificationToken: <CSRF token, fetched per call from /mychartcn/Home/CSRFToken>
```

CSRF token must match the request's `Referer`. Reuse `csrf.fetch_csrf_token`.

## `POST /mychartcn/Visits/VisitsList/LoadUpcoming`

**Query parameters (sent verbatim by Kaiser's front end):**

- `timeZone=America/Los_Angeles` — drives Kaiser's display-time formatting. NorCal-specific. Pull from `profile.py` if/when SoCal/NW becomes a target.
- `ComponentNumber=5` — Epic component identifier. Hardcode.
- `noCache=<random float>` — cache-buster.

**Request body:** none.

**Response shape (recon data — abbreviated, real response has ~120 fields per visit):**

```json
{
  "InProgressVisits": [],
  "NextNDaysVisits": [],
  "LaterVisitsList": [
    {
      "Id": "WP-24...",                       // opaque ID
      "Csn": "WP-24...",                      // identical to Id in observed data
      "Dat": "WP-24...",                      // separate opaque ID, used by visitdetails GET
      "Instant": "/Date(1779137400000)/",     // legacy ASP.NET Date, ms since epoch
      "Date": "Monday May 18, 2026",
      "Time": "1:50 PM",
      "TimeZone": "PDT",
      "ClientTimeZoneMarker": "PDT",
      "PrimaryDate": "5/18/2026 1:50:00 PM",
      "ShortDate": "05/18/26",
      "VisitTypeName": "Office Visit",
      "EncounterType": 1,                     // Epic enum: 1 = Office Visit, 3 = Refill, ...
      "PrimaryProviderName": "DR. EXAMPLE",
      "PrimaryProvider": {
        "EncryptedId": "WP-24...",
        "Name": "DR. EXAMPLE",
        "PhotoUrl": "https://...",
        "WebPageUrl": "https://mydoctor.kaiserpermanente.org/...",
        "Department": {
          "Id": "<dept_id>",
          "Name": "Family Medicine Department",
          "Address": ["<street>", "<city, state zip>"],
          "PhoneNumber": "‪<area-prefix-line>‬",   // wrapped in U+202A / U+202C bidi marks
          "Specialty": {
            "Value": "19",
            "Title": "Family Practice",
            "Abbreviation": "FAM MED"
          },
          "TimeZone": "PDT",
          "ArrivalLocation": "",
          "CanShowDrivingDirections": true
        }
      },
      "OtherProviders": [],
      "PrimaryDepartment": { /* same shape as Department above */ },
      "Telemedicine": null,                   // populated for video visits
      "EVisit": null,                         // populated for async e-visits
      "CanShowTelemedicine": false,
      "ArrivalTime": null,
      "DurationInMinutes": null,
      "Copay": null,
      "IsRescheduleEnabled": true,
      "IsDirectCancelEnabled": true,
      "IsRequestCancelEnabled": false,
      "IsEcheckInCompleted": false,
      "IsLocal": true,
      "IsNonEpic": false,
      "Organization": { /* KP-org metadata */ }
    }
  ],
  "HighlightDays": ["5/18/2026"],
  "HasPVG": false                              // patient visit guide
}
```

### Bucket semantics

Kaiser splits upcoming visits into three buckets driven by Epic's `NextNDaysVisits` near-future window (around 7 days):

- **`InProgressVisits`** — currently happening (e.g. you're in the waiting room). Empty when nothing is live.
- **`NextNDaysVisits`** — within the near window.
- **`LaterVisitsList`** — beyond the near window.

`list_appointments` flattens all three in that order so the in-progress / next-up visit is always first.

### Field highlights

- **`Instant` (legacy `/Date(ms)/`)** — converted to ISO-8601 UTC by `_parse_net_date`. The `Date`/`Time`/`PrimaryDate` strings are Kaiser's localized display strings.
- **`Time` is null for some visit types** (notably refills and surgery scheduling). Kaiser also has `IsTimeToBeDetermined` and `IsHideVisitTime` flags but in our recon data refills had both False and just `Time: null`. Treat `time_display=None` as "no clock time" and trust `instant_iso` (which is always present).
- **`PhoneNumber` is wrapped in unicode bidi marks** (U+202A / U+202C). `_clean_phone` strips them.
- **Telemedicine signal lives in three fields** — `Telemedicine`, `EVisit`, `CanShowTelemedicine`. `_is_telemedicine` returns True if any are set, since Kaiser's UI logic appears to OR them.
- **Cancel flags split** — Kaiser distinguishes "patient can cancel directly" (`IsDirectCancelEnabled`) from "patient must request cancel" (`IsRequestCancelEnabled`). We collapse both into a single `can_cancel` field; surface separately if a future tool needs to act on the distinction.
- **`Csn` is the visit token used by `/mychartcn/Visits/visitdetails?csn=...`** if we ever add a `read_appointment` tool. The detail page returns HTML, not JSON.

## `POST /mychartcn/Visits/VisitsList/LoadPast`

**Query parameters:**

- `loadpast=1` — literal flag.
- `searchString=` — empty in our recon. Probably wires to in-page search; OpenKP's `list_past_visits` doesn't expose it.
- `oldestRenderedDate=<ISO>` — UTC ISO-8601 with millisecond precision and a `Z` suffix, e.g. `2026-05-04T15:50:23.129Z`. Front end seeds this with `now` on first load and updates it from the oldest visit on the previous page. Confirmed via HAR: each subsequent call's value matches the previous page's last-visit `Instant` converted to ISO UTC.
- `numVisitsToRetrieve=<int>` — page size. **Optional** in the front end (Kaiser defaults to 10 when absent), but Kaiser honors any value we send within the observed-working range. Verified to work at 43 and 78 in the session-15 HAR. OpenKP's `fetch_past_visits` defaults to 50 and hard-caps at 78.
- `ComponentNumber=7` — Epic component identifier. Hardcode.
- `noCache=<random float>`.

**Request body (form-encoded):** `serializedIndex=<cursor>`. Empty on the first page; subsequent pages echo the **top-level** `SerializedIndex` from the previous response. Note Kaiser also returns a per-org `SerializedIndex` (a JSON-encoded `{"FromDAT":..., "FromInst":...}` blob) that is NOT what goes in the body — use the top-level value.

**Content-Type header:** `application/x-www-form-urlencoded; charset=UTF-8` (different from LoadUpcoming).

**Response shape:**

```json
{
  "ViewBagProperties": {...},
  "SerializedIndex": "WP-24DsFwYK7Ti9pT6nkXcGVf9...",   // cursor for next page's body
  "List": {
    "<org_id>": {
      "Organization": {...},
      "List": [
        {
          "Id": "WP-24...",
          "Csn": "WP-24...",
          "Instant": "/Date(1769414400000)/",
          "Date": "Monday January 26, 2026",
          "Time": null,                                   // refills have no clock time
          "VisitTypeName": "Refill",
          "EncounterType": 3,
          "PrimaryProviderName": "...",
          "PrimaryProvider": {...},
          "PrimaryDepartment": {...},
          "IsCanceled": false,
          "IsNoShow": false,
          "LeftWithoutSeen": false,
          "IsNotViewed": true,                            // "new clinical info" indicator
          "IsNotesOnly": false,
          "PastVisitBucket": "past6month",                // "past6month" | "past1year" | "past2year" | ...
          "HasDownloadSummaryLink": true,
          "HasTransmitSummaryLink": true,
          "IsClinicalInformationAvailable": true,
          "IsClinicalNoteAvailable": false,
          "IsVisitSummaryEnabled": true,
          "TelehealthMode": 3,                            // enum, semantics not fully mapped — don't trust as a virtual-visit signal
          "IsInHomeVisit": true,                          // true for mailed refills — NOT a telemedicine signal
          "IsFullyPaid": true,
          "Telemedicine": null,                           // populated for actual video visits
          "EVisit": null,                                  // populated for async e-visits
          ...
        }
      ],
      "ListSize": 10,
      "HasMoreData": true,
      "SerializedIndex": "{\"FromDAT\":\"54114.97\",...}"   // per-org cursor — DO NOT use for next-page body
    }
  },
  "CanSearch": false,
  "CanAllSearch": false,
  "CanSort": true
}
```

### Past-visit field highlights

- **Visits arrive newest-first within a page** (~10 per page). Across pages, ordering is preserved.
- **`Time` is null for some visit types** — refills, surgery scheduling — same as upcoming. Trust `instant_iso`.
- **`EncounterType` is NOT a clean type discriminator.** Office Visit and Refill both carry `EncounterType=3` in observed data (despite different `VisitTypeName`). Trust `visit_type` for human categorization.
- **`IsInHomeVisit=true` does NOT mean telemedicine.** Mailed-refill events are categorized as in-home in Kaiser's data model. The `Telemedicine` and `EVisit` fields are the more reliable virtual-visit signals (both null for in-person, populated for video / async).
- **`PastVisitBucket`** is the rough recency band Kaiser assigns. Stable values seen: `past6month`, `past1year`, `past2year`. Useful for grouping; don't rely on it for precise date filtering — use `instant_iso`.
- **`HasMoreData` is per-org**, not top-level. `_parse_past_response` ORs across orgs so `has_more` is True if any org still has older visits.

### Pagination algorithm (as implemented in `fetch_past_visits`)

1. Page 1 request: `oldestRenderedDate = now (UTC, ms-precision, Z-suffix)`, body `serializedIndex=`.
2. Parse response. Compute `oldest_instant` = min of all visits' parsed `Instant` values across orgs.
3. Next request: `oldestRenderedDate = oldest_instant_formatted`, body `serializedIndex = <top-level SerializedIndex>`.
4. Stop walking when ANY of: `pages_walked >= max_pages`, every org reports `HasMoreData=false`, page returned no parseable visits, OR (if `until_iso` provided) the page's `oldest_instant` is older than `until_iso`.

## `POST /mychartcn/Visits/VisitsList/LoadFilterOptions` — not used in v1

Reference for a future filter-by-provider tool. Captured in `kp-appointments-filter.har` (session 15).

**Request body (form-encoded):** `loadProviderOptions=true&loadDepartmentOptions=true&loadSpecialtyOptions=true`. All three booleans observed True; we don't know whether subset-only requests are supported.

**Response shape (~145KB on a multi-year history):**

```json
{
  "AllProvidersDATs": {
    "<PROVIDER NAME>": ["<visit_dat_1>", "<visit_dat_2>", ...]
  },
  "AllDepartmentsDATs": { /* same shape, keyed by department name */ },
  "AllSpecialtiesDATs": { /* same shape, keyed by specialty title */ },
  "PastProvidersDATs":   { /* subset that's only past */ },
  "FutureProvidersDATs": { /* subset that's only upcoming */ },
  "SortedPastDATs": ["<dat_newest>", ..., "<dat_oldest>"],
  "ShowCanceledApptsSetting": true
}
```

What this gives us for free:
- The full set of providers / departments / specialties the patient has ever interacted with (autocomplete-friendly).
- The total count of past visits (`SortedPastDATs.length`) — handy for sizing a `max_pages` walk.
- A reverse index from provider/department/specialty name → list of visit DATs. With a future filter-aware LoadPast call (we haven't observed Kaiser accepting a filter ID yet), this could power `list_past_visits(provider="...")`-style queries.

## History depth observation

Kaiser stores patient visit history going back many years. In one verified live account (session 15), `SortedPastDATs.length == 220` covered visits back to before October 2017 (~8.5 years). For walking entire histories with `list_past_visits`, plan on ≥3 round trips at `page_size=78` for a typical multi-year patient.

## Capture / re-recon

To regenerate the recon JSONs (e.g. after a Kaiser response-shape change):

```
.venv/bin/python openkp/scripts/recon_appointments.py
```

Outputs to `docs/research/captures/recon-appointments-{header,pending,upcoming,past}.json` — gitignored, contain PHI.

HAR captures (`kp-appointments*.har`) are useful for the request side but unreliable for response bodies because Chrome's DevTools evicts old response payloads before export. Prefer the recon script for response-shape mapping.

## Adjacent endpoints worth noting

Discovered 2026-05-06 from `kp-capture-various-with-phi.har`:

- `GET /mychartcn/Visits/VisitDetails/GetCalendarFile?csn=<csn>&details=true`
  — returns a `.ics` calendar file for one appointment. Tiny, easy tool
  candidate: `download_appointment_ics(csn)` would let users add a KP
  appointment to their calendar app of choice. The `csn` is the same Epic
  handle already exposed by `list_appointments`.
