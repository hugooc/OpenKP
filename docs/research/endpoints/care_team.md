# Care team endpoint

Source HAR: `docs/research/captures/kp-care-team-1.har`, 2026-04-23 (full
response bodies preserved). Implemented as `list_care_team` 2026-05-26.

## What this is

The "Care Team and Recent Providers" panel on the MyChart home page (right
column): the patient's primary care provider, specialists, and recently-seen
clinicians. Page route: `/mychartcn/clinical/careteam`.

This is a strict superset of what `get_profile` exposes. `get_profile` returns
only the PCP. This surface returns the whole care relationship roster plus
per-provider capability flags (can you message them, can you self-schedule).

Two endpoints back it, both on the legacy `/mychartcn/Clinical/CareTeam/Load*`
family — the same `/mychartcn/Clinical/<topic>/Load*` shape as problems,
allergies, and appointments:

| Feature | Endpoint | Status |
| --- | --- | --- |
| Internal KP providers | `POST /mychartcn/Clinical/CareTeam/Load` | ✅ Mapped + shipped. Real bodies. |
| External (non-KP) providers | `POST /mychartcn/Clinical/CareTeam/LoadExternal` | ✅ Mapped + shipped. Returned empty list in recon (no external providers in the captured data). Entry shape assumed identical to internal. |

## Auth / anti-forgery

Standard `/mychartcn/` CSRF contract (see `messages.md` / `problems.md`). Fetch
one `__RequestVerificationToken` and reuse it for **both** POSTs — in the HAR,
`Load` and `LoadExternal` carried the byte-identical token. Referer for both is
`https://healthy.kaiserpermanente.org/mychartcn/clinical/careteam`.

Both are GET-shaped POSTs: **no request body**, everything is in query params.

**Query params** (`Load`):

```
hfrId=          (empty)
sources=        (empty)
actions=        (empty)
isPrimaryStandalone=true
ComponentNumber=2
noCache=<random float>
```

`LoadExternal` is identical minus `isPrimaryStandalone`.

## Response shape

```json
{
  "ProvidersList": [
    {
      "ID": "WP-24...",                       // opaque Epic handle
      "Name": "PAT EXAMPLE MD",               // fabricated for this doc
      "Photo": "https://www.permanente.net/pmdb/photosync/<id>_photoweb.jpg",
      "NationalProviderID": "WP-24...",        // opaque handle, NOT a real NPI
      "WebPageUrl": "https://mydoctor.kaiserpermanente.org/ncal/doctor/<slug>",
      "InfoBlurbUrl": "https://healthy.kaiserpermanente.org/hmdo/...",
      "AboutMeBlurb": [],
      "CanViewProviderDetails": true,
      "CanDirectSchedule": false,
      "CanRequestAppointment": false,
      "CanMessage": false,
      "CommCenterMessageUrl": "",
      "CanRequestCustomAppt": false,
      "HasNoProviderRecord": false,
      "IsNewSchedulingEnabled": true,
      "Specialty": "Family Practice",
      "Relation": "Primary Care Provider",     // "Cardiologist", etc.
      "SchedulableVisitTypes": null,
      "DepartmentID": "WP-24...",              // opaque Epic handle
      "Organizations": null,
      "IsExternal": false,
      "CareTeamStatus": 0,                     // raw int enum; 0 for all observed
      "CanHideProvider": true
    }
  ],
  "DescriptiveTitle": "Care Team and Recent Providers",
  "TabColorClass": "color1",
  "IsCustomApptReqEnabled": false,
  "CustomRequestAppointmentLink": "showform&formname=ApptReqCntr"
}
```

(Provider names and all `WP-24...` IDs above are fabricated/elided — the real
HAR contains the member's actual care relationships, which are PHI-adjacent.)

## Field mapping (scraper → `CareTeamProvider`)

| Model field | Source key | Notes |
| --- | --- | --- |
| `id` | `ID` | Opaque Epic handle. Required — entry dropped if missing. |
| `name` | `Name` | Display name incl. credential suffix. |
| `specialty` | `Specialty` | e.g. "Family Practice", "Cardiology". |
| `relation` | `Relation` | e.g. "Primary Care Provider", "Cardiologist". Populated, unlike the messages recipient catalog's null role. |
| `department_id` | `DepartmentID` | Opaque handle; pairs with scheduling. |
| `is_external` | `IsExternal` | True for `LoadExternal` entries. |
| `can_message` | `CanMessage` | Panel's inline quick-message button only — NOT reachability. See note below. |
| `can_schedule` | `CanDirectSchedule` | This panel's button only. |
| `can_request_appointment` | `CanRequestAppointment` | This panel's button only. |
| `can_view_details` | `CanViewProviderDetails` | |
| `photo_url` | `Photo` | permanente.net headshot URL. |
| `provider_page_url` | `WebPageUrl` | Public mydoctor.kaiserpermanente.org bio. |
| `care_team_status` | `CareTeamStatus` | Raw int; enum meaning unknown, 0 for all observed. |

Fields intentionally dropped: `NationalProviderID` (an opaque handle, not a
true NPI — misleading to surface), `InfoBlurbUrl`, `AboutMeBlurb`,
`CommCenterMessageUrl`, `IsNewSchedulingEnabled`, `SchedulableVisitTypes`,
`Organizations`, `HasNoProviderRecord`, `CanRequestCustomAppt`, `CanHideProvider`.
Easy to add later if a tool needs them.

## Behavior notes

- **`can_message` is not reachability.** It mirrors the care-team panel's inline
  quick-message button, which Kaiser can leave off even for providers you can
  message just fine. Messaging actually runs through a different surface
  (`list_message_recipients` + `send_message`, the "Message your care team"
  compose flow). A provider with `can_message=False` here may still be a valid
  recipient there. Observed live 2026-05-26: both providers in the captured
  data came back `can_message=False` on this panel, which an LLM read as "outreach must go
  through their department" — misleading, since they're messageable via
  `send_message`. The same panel-button-only caveat applies to `can_schedule`
  and `can_request_appointment`. The tool docstring and model comments now spell
  this out so callers don't treat these flags as gates.
- `LoadExternal` is best-effort in the scraper: if it errors, we still return
  the internal roster. External providers are a bonus, not the primary data.
- The external entry shape is **assumed identical** to internal — recon had an
  empty external list, so it is untested against real external data. The parser
  is defensive (never raises) so a shape surprise degrades to partial/empty.
- No pagination. Kaiser returns the full roster in one call each.

## Open questions / future work

- **Live-verify external providers.** Need a member who has a non-KP provider
  on file to confirm the `LoadExternal` entry shape matches internal.
- **`CareTeamStatus` enum.** Only `0` observed. Could distinguish
  active/inactive/recent — capture a roster with a dropped provider to learn.
- **`relation` → recipient linkage.** The care team `id` is a different opaque
  handle than the `send_message` recipient catalog id. If we ever want
  "message my cardiologist" to chain `list_care_team` → `send_message`, we
  need to confirm whether the two ID spaces are reconcilable or whether
  messaging must always go through `list_message_recipients`.
