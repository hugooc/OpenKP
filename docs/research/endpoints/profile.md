# Profile endpoint

Source: `docs/research/captures/kp-login-2.har`, 2026-04-22.

## Summary

| Field | Endpoint | Status |
| --- | --- | --- |
| Demographics, contact, insurance | `GET /mycare/v1.0/user` (pharmacy header contract) | ✅ Mapped, implemented |
| PCP name / details | `POST /mychartcn/Clinical/CareTeam/Load` (CSRF-protected) | ✅ Mapped, implemented |
| Emergency contacts | `POST /mychartcn/Demographics/Relationships/GetRelationshipList` (CSRF-protected) | ✅ Mapped, implemented (see `emergency_contacts.md`) |

## Why not `/mycare/v1.0/uidatalayer/s/profile`?

That KPDL endpoint is a write-through data layer, not an authoritative source.
It populates as a side effect of `/mycare/v1.0/user` and other calls. A cold
httpx client hitting it directly gets back a minimal shell with empty `data`,
which is what v1 of `get_profile` was returning (all fields `null`).

The HAR shows this clearly: the first five calls to KPDL during page load
return 212-byte responses. After the browser fetches `/mycare/v1.0/user`,
subsequent KPDL calls return 1130+ bytes of populated data.

So we cut out the middle layer and call the source directly.

## `GET /mycare/v1.0/user`

**URL:** `https://healthy.kaiserpermanente.org/mycare/v1.0/user`

**Method:** GET

**Auth:** session cookies from the interactive login.

**Trust boundary caveat:** this endpoint validates the session cookie for user
identity but also inspects `X-apiKey` / `X-appName` / `X-componentName` headers
for consumer routing. OpenKP piggybacks on the pharmacy app's identity
(X-apiKey = `kprwdpharmctr68973257122561335296`, X-appName = `rx-order-management`).
See `docs/adr/006-user-endpoint-piggyback.md` for the tradeoff.

**Required headers (observed — missing any returns 502):**

```
Accept: */*
Content-Type: application/json
Referer: https://healthy.kaiserpermanente.org/mychartcn/Home?lang=en-US
X-apiKey: kprwdpharmctr68973257122561335296
X-appName: rx-order-management
X-componentName: User Profile Component
X-includeEntitlements: false
X-includeProxyEntitlements: false
X-inclusionJsonPath: <semicolon-separated JSONPath list>
X-osVersion: 0
X-Requested-With: XMLHttpRequest
X-retainJsonSchema: true
X-sessionToken: true
X-useragentCategory: B
X-useragentType: Desktop
X-versionId: 3.0.1.2
```

**X-inclusionJsonPath** filters the response server-side to just the requested
fields. OpenKP requests:

```
$.UserAccountData.ebizAccountsWithPersonInfos.nameDetails
$.UserAccountData.ebizAccountsWithPersonInfos.contactInfo.addressInfos
$.UserAccountData.ebizAccountsWithPersonInfos.contactInfo.phoneInfos
$.UserAccountData.ebizAccountsWithPersonInfos.contactInfo.emailAddresseInfos
$.UserAccountData.ebizAccountsWithPersonInfos.dateOfBirth
$.UserAccountData.ebizAccountsWithPersonInfos.age
$.UserAccountData.ebizAccountsWithPersonInfos.gender
$.UserAccountData.ebizAccountsWithPersonInfos.areaOfCareInfos
$.UserAccountData.ebizAccountsWithPersonInfos.membershipAccountInfo.accountId
$.UserAccountData.ebizAccountsWithPersonInfos.membershipAccountInfo.region
$.UserAccountData.ebizAccountsWithPersonInfos.membershipAccountInfo.planInfos[0].purchaserName
$.UserAccountData.ebizAccountsWithPersonInfos.membershipAccountInfo.planInfos[0].consumerPlanType
$.UserAccountData.ebizAccountsWithPersonInfos.membershipAccountInfo.planInfos[0].coverageStartDate
$.UserAccountData.ebizAccountsWithPersonInfos.membershipAccountInfo.planInfos[0].coverageEndDate
$.UserAccountData.userIdentityInfo.guid
$.UserAccountData.userIdentityInfo.email
$.UserAccountData.userIdentityInfo.preferredGivenName
```

Kaiser typo note: `emailAddresseInfos` (singular "Addresse") is the actual
field name, not a typo on our side. It was empty in the observed response;
email lives in `userIdentityInfo.email`.

**Response shape (PHI-free, trimmed to the fields we parse):**

```json
{
  "UserAccountData": {
    "ebizAccountsWithPersonInfos": {
      "nameDetails": {
        "surname": "<string>",
        "firstName": "<string>",
        "middleName": "<string>"
      },
      "contactInfo": {
        "addressInfos": [
          {
            "type": "MAILING",
            "label": "Mailing",
            "street1": "<string>",
            "street2": null,
            "city": "<string>",
            "state": "<2-letter>",
            "postalCode": 90210,
            "preferredIn": true
          }
        ],
        "phoneInfos": [
          {
            "type": "RESIDENCE",
            "label": "Home phone",
            "primaryIndicator": false,
            "phoneNumber": {
              "area": 510,
              "exchange": 555,
              "subscriber": 1234,
              "country": null,
              "extension": null
            }
          }
        ],
        "emailAddresseInfos": []
      },
      "dateOfBirth": "YYYY-MM-DD",
      "age": 59.0,
      "gender": "M",
      "areaOfCareInfos": [
        {"guid": 1234567, "mrn": 12345678, "areaOfCare": "NCA", "role": "PRI"}
      ],
      "membershipAccountInfo": {
        "accountId": 12345678,
        "region": "NCA",
        "planInfos": [
          {
            "purchaserName": "<string>",
            "consumerPlanType": "<string>",
            "coverageStartDate": "YYYY-MM-DD",
            "coverageEndDate": "YYYY-MM-DD"
          }
        ]
      }
    },
    "userIdentityInfo": {
      "guid": 1234567,
      "email": "<string>",
      "preferredGivenName": "<string>"
    }
  }
}
```

**MRN vs account-id:** `areaOfCareInfos[].mrn` and
`membershipAccountInfo.accountId` carry the same value in practice. OpenKP
prefers `areaOfCareInfos[0].mrn` (explicitly labeled) with fallback to
`accountId`.

**postalCode** comes back as an int (including ZIP+4 as a 9-digit int). We
coerce to string.

**phoneNumber** is a structured object; OpenKP formats as `AREA-EXCHANGE-SUBSCRIBER`
with optional `x<extension>` suffix.

**Phone list ordering is non-deterministic.** Observed live 2026-04-23: two
consecutive `get_profile` calls against the same account returned the three
phones in different orders (DAY / SMS / EVENING vs. EVENING / SMS / DAY), with
every entry carrying `primaryIndicator: false`. A previous "default first to
primary" heuristic made the surfaced `is_primary` flag flip between calls,
which was worse than useless. The parser now reports `is_primary` honestly:
if Kaiser doesn't flag one, all come back `false` and callers pick based on
`type`/`label`.

## `POST /mychartcn/Clinical/CareTeam/Load` (PCP)

Source HAR: `docs/research/captures/kp-care-team-1.har`, 2026-04-23.

This is a CSRF-protected form POST. It requires a short-lived anti-forgery
token fetched from a sibling endpoint on the same request.

### Step 1 — fetch a token

**URL:** `GET https://healthy.kaiserpermanente.org/mychartcn/Home/CSRFToken?noCache=<random>`

**Headers:**

```
Accept: */*
Referer: https://healthy.kaiserpermanente.org/mychartcn/clinical/careteam
X-Requested-With: XMLHttpRequest
```

**Response** is an HTML fragment (not JSON):

```html
<input name="__RequestVerificationToken" type="hidden" value="CQEPztGC...Js1" />
```

OpenKP extracts the `value` attribute via a simple regex. No need to parse
the full HTML — the response is always shaped like this single input element.

### Step 2 — fetch the care team

**URL:** `POST https://healthy.kaiserpermanente.org/mychartcn/Clinical/CareTeam/Load`

**Query parameters:**

```
hfrId=                     (empty)
sources=                   (empty)
actions=                   (empty)
isPrimaryStandalone=true
ComponentNumber=2
noCache=<random>
```

**Headers:**

```
Accept: application/json, text/javascript, */*; q=0.01
Referer: https://healthy.kaiserpermanente.org/mychartcn/clinical/careteam
X-Requested-With: XMLHttpRequest
__RequestVerificationToken: <token from step 1>
```

**Body:** empty. `Content-Length: 0`.

**Response:** `application/json; charset=utf-8`, shaped like:

```json
{
  "ProvidersList": [
    {
      "ID": "WP-24...",
      "Name": "DR. EXAMPLE PROVIDER",
      "Photo": "https://www.permanente.net/pmdb/photosync/<id>_photoweb.jpg",
      "NationalProviderID": "WP-24...",
      "WebPageUrl": "https://mydoctor.kaiserpermanente.org/ncal/doctor/exampleprovider",
      "InfoBlurbUrl": "...",
      "Specialty": "Family Practice",
      "Relation": "Primary Care Provider",
      "IsExternal": false,
      "CareTeamStatus": 0,
      "CanViewProviderDetails": true,
      "CanDirectSchedule": false,
      "CanMessage": false
    },
    {
      "Name": "DR. SECOND EXAMPLE",
      "Specialty": "Specialty Name",
      "Relation": "Specialist",
      ...
    }
  ],
  "DescriptiveTitle": "Care Team and Recent Providers",
  "TabColorClass": "color1",
  "IsCustomApptReqEnabled": false,
  "CustomRequestAppointmentLink": "..."
}
```

### PCP selection

Filter `ProvidersList` where `Relation == "Primary Care Provider"`, take the
first. If nothing matches, `pcp = None`. Kaiser returns the rest of the care
team (cardiologist, etc.) in the same response — those aren't surfaced in
the `Profile` today, but a future `list_care_team` tool could consume the
full list from the same response.

### Field mapping

| Kaiser field | `Provider` field |
| --- | --- |
| `Name` | `name` |
| `Specialty` | `specialty` |
| `Relation` | `relation` (always `"Primary Care Provider"` for the PCP entry) |
| `WebPageUrl` | `profile_url` (public mydoctor.kaiserpermanente.org page) |

`ID` and `NationalProviderID` look opaque (base64-ish scrambled Kaiser
internal IDs, not real 10-digit NPIs) — skipped. `Photo` is a public URL to
the provider's headshot — skipped for now, could be added later if it's
useful for a UI surface.

### Why this is graceful on failure

`fetch_profile` wraps the PCP fetch in a try/except. If the token endpoint
returns something we can't parse, or if CareTeam/Load returns an error, the
parent profile still returns demographics/contact/insurance with `pcp=None`
and a warning logged. Demographics is the critical payload; a Kaiser hiccup
on the care-team leg shouldn't blow up the whole tool call.

## Emergency contacts

Mapped and implemented. Lives at `POST /mychartcn/Demographics/Relationships/GetRelationshipList`,
not under `/PersonalInformation/...` as we initially guessed. The endpoint
returns the full relationship roster (emergency contacts, DPOAHC healthcare
agents, conservators, "people in your life") in a single payload — see
`emergency_contacts.md` for the full request/response map.

## Open questions

1. Does Kaiser actually validate `X-apiKey` → `X-appName` pairings per user, or is X-apiKey purely telemetry/routing? (If the former, we'll need a plan B; ADR-006 tracks this risk.)
2. CSRF token lifetime — is it bound to a session, or a one-shot? OpenKP refetches per PCP call (cheap) rather than caching, which sidesteps the question.
