# Emergency contacts endpoint

Source: `docs/research/captures/recon-emergency-contact-life-care-etc.har`, 2026-04-25.

## Summary

Kaiser surfaces emergency contacts (and DPOAHC healthcare agents, conservators,
"people in your life," etc.) under a single Epic/MyChart **Relationships**
endpoint. There's no separate "emergency contacts" API — they're a slice of the
broader relationship roster, distinguished from healthcare agents only by the
`IsActiveHealthCareAgent` flag and the `LegalRelationToPatient` code.

| Field | Endpoint | Status |
| --- | --- | --- |
| Emergency contacts + healthcare agents | `POST /mychartcn/Demographics/Relationships/GetRelationshipList` (CSRF-protected) | ✅ Mapped, implemented |

## `POST /mychartcn/Demographics/Relationships/GetRelationshipList`

Same CSRF dance as `CareTeam/Load`: GET a token from `/mychartcn/Home/CSRFToken`,
then POST it back as a header. Reuses the existing `fetch_csrf_token` helper.

### Step 1 — fetch a token

`GET https://healthy.kaiserpermanente.org/mychartcn/Home/CSRFToken?noCache=<random>`

Headers, response shape: identical to the CareTeam recipe in `profile.md`.

### Step 2 — fetch the relationships

**URL:** `POST https://healthy.kaiserpermanente.org/mychartcn/Demographics/Relationships/GetRelationshipList`

**Query parameters:**

```
noCache=<random>
```

**Headers:**

```
Accept: application/json, text/javascript, */*; q=0.01
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
Referer: https://healthy.kaiserpermanente.org/mychartcn/AdvancedCarePlanning
X-Requested-With: XMLHttpRequest
__RequestVerificationToken: <token from step 1>
```

**Body** (form-encoded, observed verbatim):

```
getEOLDocs=true&disableUTF8=true
```

`getEOLDocs=true` asks the server to also include End-of-Life document
references (advance directive PDFs the user has uploaded). We don't surface
those today but the field is harmless to leave on.

`disableUTF8=true` switches Kaiser to ASCII-fold the relationship category
labels (the response carries both `Title` and `TitleUtf8` per category item;
we read `Title`).

### Response shape

`application/json; charset=utf-8`. Trimmed to the fields we parse:

```json
{
  "Success": "1",
  "HasConversionToEPT1733Run": true,
  "HideRelationships": false,
  "CategoryData": {
    "1000": {"CategoryItems": [{"Value": "7", "Title": "Spouse", ...}, ...]},
    "1107": {"CategoryItems": [...]},
    "1113": {"CategoryItems": [{"Value": "104", "Title": "Designated Decision Maker (not a legal designation)", ...}, ...]}
  },
  "Relationships": [
    {
      "Id": "<opaque Epic id>",
      "FormattedName": "<display name string>",
      "FirstName": null,
      "LastName": null,
      "AddressViewModel": {
        "Street": "<string>",
        "City": "<string>",
        "State": {"Number": "5", "Title": "California", "Abbreviation": "CA"},
        "Zip": "<string>",
        "Country": {"Number": "223", "Title": "UNITED STATES"},
        "Unit": null,
        "Floor": null,
        "Building": null,
        "FormattedValues": ["<line 1>", "<line 2>"]
      },
      "HomePhone": {"FieldId": "HomePhone", "Value": "415-555-1234"},
      "WorkPhone": {"FieldId": "WorkPhone", "Value": ""},
      "MobilePhone": {"FieldId": "MobilePhone", "Value": "415-555-1234"},
      "Email": {"FieldId": "Email", "Value": ""},
      "PreferredDevice": {"FieldId": "PreferredDevice", "Value": "3"},
      "RelationToPatient": {"FieldId": "RelationshipToPatient", "Value": "7"},
      "LegalRelationToPatient": {"FieldId": "LegalRelationshipToPatient", "Value": "104"},
      "IsActiveHealthCareAgent": false,
      "IsPrimary": true,
      "IsViewOnly": false
    }
  ],
  "EndOfLifeDocuments": []
}
```

### Field mapping (Kaiser → `EmergencyContact`)

| Kaiser field | Our field | Notes |
| --- | --- | --- |
| `FirstName` + `LastName` (combined) | `name` | Falls back to `FormattedName` if both name parts are null |
| `RelationToPatient.Value` | `relationship` | Resolved via `CategoryData["1000"]` lookup; a code like `"7"` becomes `"Spouse"` |
| `LegalRelationToPatient.Value` | `legal_role` | Resolved via `CategoryData["1107"]` or `["1113"]` lookup; a code like `"104"` becomes `"Designated Decision Maker (not a legal designation)"`. `null` if empty |
| `IsActiveHealthCareAgent` | `is_active_healthcare_agent` | True for live DPOAHC agents |
| `IsPrimary` | `is_primary_contact` | Kaiser's "primary contact" flag |
| `HomePhone.Value` | `home_phone` | Already formatted as `AAA-EEE-SSSS`; empty string normalized to `null` |
| `WorkPhone.Value` | `work_phone` | Same |
| `MobilePhone.Value` | `mobile_phone` | Same |
| `Email.Value` | `email` | Empty string normalized to `null` |
| `AddressViewModel.Street` | `address.street1` | |
| `AddressViewModel.Unit` | `address.street2` | We pick `Unit` over `Floor` / `Building` (most common in observed data) |
| `AddressViewModel.City` | `address.city` | |
| `AddressViewModel.State.Abbreviation` | `address.state` | Two-letter code |
| `AddressViewModel.Zip` | `address.postal_code` | |

If `AddressViewModel.Street` is blank we return `address: null` rather than an
all-null Address object.

### Why we don't surface `PreferredDevice`

The JS string bundle (`mychartcn/bundles/core-3-en-US`) only declares labels
for `PreferredDeviceOptionLabels_1` (Mobile), `_7` (Home), `_8` (Work). Live
data carries values like `"3"` that aren't in that set, so the code is
unreliable as a UI signal. We expose the three phone fields directly and let
the caller decide which to use; the "preferred" code adds noise without
adding usable information.

### `FormattedName` quirks

Observed live: `FormattedName` can include freeform annotations the user
typed during contact entry — e.g. `"CALL FIRST) <name> (URGENT"` where the
parenthetical fragments are notes the user wedged into the name field
because there was nowhere else to put them. When `FirstName` and `LastName`
are present, prefer combining them; when they're both null (legacy
contacts), fall back to `FormattedName` verbatim. Don't try to clean or
parse the freeform string — it's user-authored intent.

### Relationship category lookup

`CategoryData["1000"]` is the universe of social/family relationships
(67 entries in the observed payload — Spouse, Daughter, Caregiver, Neighbor,
etc.). `CategoryData["1107"]` and `["1113"]` carry the four legal-role
categories (Conservator, DPOAHC Primary, DPOAHC Alternate, Designated
Decision Maker). Both legal categories carry the same items in observed
data; we check `1113` first, fall back to `1107`.

If a code can't be resolved (lookup table missing or value not present), the
field returns `null` rather than the raw code — the raw integer isn't
useful to a caller.

### Healthcare agents vs. emergency contacts

Kaiser doesn't separate "emergency contact" from "healthcare agent" in this
endpoint — both come back in the same `Relationships` array. For v1 we
return everything and let `is_active_healthcare_agent` and `legal_role`
disambiguate. A future tool might split them into two surfaces, but the
data is the same.

### End-of-life documents

`EndOfLifeDocuments` is an array of advance-directive PDFs the user has
uploaded (POLST, DPOAHC document, etc.). Empty in the observed capture.
Not surfaced today; would be a separate `list_advance_directives` tool
sourced from this same response.

### Why this is graceful on failure

Mirror the PCP pattern. `fetch_profile` wraps the emergency-contact fetch
in a try/except: a token failure or a 500 from `GetRelationshipList` logs
a warning and leaves `emergency_contacts: []` rather than blowing up the
whole `get_profile` call. Demographics is the critical payload.
