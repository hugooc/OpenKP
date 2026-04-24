# Test results endpoints (labs, imaging, cardiac device checks)

Source HAR: `docs/research/captures/kp-labs-1.har`, 2026-04-23.

## Summary

| Feature | Endpoint | Status |
| --- | --- | --- |
| List orders (summaries) | `POST /mychartcn/api/test-results/GetList` | ✅ Mapped, implemented |
| Read one order in full | `POST /mychartcn/api/test-results/GetDetails` | ✅ Mapped, implemented |
| Download PDF report (chain) | `GetDocumentGenerationInfo` → `GetDocumentDetails` → `GET DownloadOrStream` | ✅ Mapped, implemented |
| Community / org metadata | `POST /mychartcn/api/test-results/GetCommunityInfo` | 🟡 Known, not surfaced (UI-only) |

MCP tools registered: `list_lab_results`, `read_lab_result`, `download_lab_result_pdf`.

## Anti-forgery + nonce (same pattern as messages)

All POSTs in this family require a `__RequestVerificationToken` header. The
shared helper `openkp.scrapers.csrf.fetch_csrf_token` fetches one per call.

`GetDetails` additionally requires a `PageNonce` in the JSON body. The nonce
lives as a `nonce="..."` attribute on `<style>` tags in the test-results HTML
page:

```
GET /mychartcn/app/test-results
```

Extraction regex: `nonce=['"]([a-f0-9]{16,})['"]`.

`GetList`, `GetDocumentGenerationInfo`, and `GetDocumentDetails` do not
require the page nonce. Only `GetDetails` does.

## Result types

Kaiser's Test Results page covers three flavors of order under one API:

| `resultType` | What it is | Data shape |
| --- | --- | --- |
| `LAB` | Blood / metabolic panels, individual analyte measurements | `resultComponents[]` populated with `componentInfo` + `componentResultInfo` |
| `IMAGING` | Radiology, ECG, cardiac device checks | `studyResult.narrative` / `.impression` populated; `resultComponents` empty |
| `OTHER` | Path reports, transcriptions, misc | Usually `resultNote.contentAsHtml` populated |

Our `list_lab_results` tool filters to `resultType == "LAB"` by default. Pass
`include_all_types=True` to see imaging and other results too.

## `POST /mychartcn/api/test-results/GetList`

**Request body:**

```json
{
  "groupType": "ORDER",
  "searchString": "",
  "maxResults": 50,
  "isCurAdmFilterEnabled": false
}
```

- `groupType`: "ORDER" for real queries. Observed "UNINITIALIZED" on a
  page-warmup call that returns nothing; we don't use that mode.
- `searchString`: matches name and (empirically) other fields. Empty = unfiltered.
- `maxResults`: capped at 200 in our code.
- `isCurAdmFilterEnabled`: always false in observed traffic. Hardcoded.

**Response shape (trimmed):**

```json
{
  "areResultsFullyLoaded": true,
  "isGroupingFullyLoaded": true,
  "groupBy": "ORDER",
  "newResultGroups": [
    {
      "key": "...",
      "resultList": ["<order_key>"],
      "sortDate": "2025-10-01T10:00:00-07:00",
      "formattedDate": "Oct 1, 2025",
      "organizationID": "..."
    }
  ],
  "newResults": {
    "<order_key>^": {
      "key": "<order_key>",
      "name": "COMPREHENSIVE METABOLIC PANEL",
      "orderMetadata": {
        "orderProviderName": "...",
        "authorizingProviderName": "...",
        "prioritizedInstantISO": "2025-10-01T10:15:00Z",
        "prioritizedInstantDisplay": "Oct 1, 2025 10:15 AM",
        "resultType": "LAB"
      },
      "resultComponents": [],
      "isAbnormal": false,
      "hasComment": false
    }
  }
}
```

**Normalization gotcha:** `newResults` is a dict keyed by order IDs with a
trailing `^` character. We strip it on parse. The in-value `key` field is
the canonical order key (without the suffix). Cross-reference
`newResultGroups[*].resultList[]` to get encounter-level date +
organization.

**Empty `resultComponents` at list level.** Values live only in `GetDetails`
responses. The list endpoint is a cheap summary view.

## `POST /mychartcn/api/test-results/GetDetails`

**Request body:**

```json
{
  "orderKey": "<from list>",
  "organizationID": "",
  "PageNonce": "<nonce from HTML>"
}
```

**Response shape (trimmed):**

```json
{
  "orderName": "...",
  "key": "...",
  "results": [
    {
      "name": "...",
      "key": "<order_key>",
      "orderMetadata": {
        "orderProviderName": "...",
        "authorizingProviderName": "...",
        "prioritizedInstantISO": "...",
        "resultType": "LAB",
        "resultStatus": "Final",
        "specimensDisplay": "Blood",
        "collectionTimestampsDisplay": "..."
      },
      "resultComponents": [
        {
          "componentInfo": {
            "componentID": "...",
            "name": "SODIUM",
            "commonName": "Sodium",
            "units": "mmol/L"
          },
          "componentResultInfo": {
            "value": "140",
            "numericValue": 140,
            "referenceRange": {
              "displayLow": "136",
              "displayHigh": "145",
              "lowerBoundExclusive": false,
              "upperBoundExclusive": false,
              "formattedReferenceRange": "136-145"
            },
            "abnormalFlagCategoryValue": "Normal"
          },
          "componentComments": {
            "hasContent": false,
            "contentAsHtml": ""
          }
        }
      ],
      "studyResult": {
        "hasStudyContent": false,
        "narrative": "",
        "impression": ""
      },
      "resultNote": {
        "hasContent": false,
        "contentAsString": "",
        "contentAsHtml": ""
      },
      "reportDetails": {
        "isDownloadablePDFReport": true
      },
      "providerComments": [],
      "isAbnormal": true,
      "hasComment": true
    }
  ]
}
```

**Field mapping:**

| Kaiser field | Our `LabResultDetail` field |
| --- | --- |
| `results[0].key` | `order_key` |
| `results[0].name` | `name` |
| `results[0].orderMetadata.*` | `result_date`, `ordering_provider`, `authorizing_provider`, `result_type`, `status`, `specimen`, `collection_timestamp` |
| `results[0].resultComponents[]` | `components[]` (parsed into `LabComponent`) |
| `results[0].studyResult.narrative` | `narrative` (HTML-stripped) |
| `results[0].studyResult.impression` | `impression` (HTML-stripped) |
| `results[0].resultNote.contentAsHtml` | `result_note` (HTML-stripped) |
| `results[0].providerComments[].contentAsHtml` | `provider_comments[]` (HTML-stripped) |
| `results[0].reportDetails.isDownloadablePDFReport` | `has_pdf` |

**Abnormal flag handling:** `abnormalFlagCategoryValue` is an enum. Observed
values: `"Unknown"`, `"Normal"`, `"High"`, `"Low"` (inferred). Any value
that isn't in `{Unknown, Normal, empty}` is surfaced as
`LabComponent.is_abnormal = True`. The order-level `isAbnormal` boolean is
Kaiser's own summary call and is reported directly.

**Reference range handling:** Kaiser returns `referenceRange` as a nested
object, never a plain string in observed traffic. The
`formattedReferenceRange` field is Kaiser's pre-rendered display string
(e.g. `"<140"`, `">=60"`, `"136-145"`) and is preferred. If that's empty,
OpenKP falls back to `<displayLow> - <displayHigh>` when both are present.
The `lowerBoundExclusive` / `upperBoundExclusive` flags are ignored since
the formatted string already encodes them.

## PDF download (three-hop chain)

### Step 1: GetDocumentGenerationInfo

```
POST /mychartcn/api/test-results/GetDocumentGenerationInfo
Body: {"orderKey": "<order_key>"}
```

Response:

```json
{"documentID": "<dcsId>", "generationStatus": "Generated"}
```

If `generationStatus != "Generated"` or `documentID` is empty, no PDF is
available. OpenKP returns `LabPdfDownload(status="no_pdf_available")`
without raising.

### Step 2: GetDocumentDetails

```
POST /mychartcn/api/documents/viewer/GetDocumentDetails
Body: {"dcsId": "<from step 1>", "fileExtension": "PDF", "organizationId": "", "useOldMobileLink": false}
```

Response (trimmed):

```json
{
  "dcsId": "...",
  "token": "...",
  "displayName": "Result Trends - CARDIAC DEVICE CHECK - Apr 22, 2026",
  "downloadUrl": "/Documents/ViewDocument/DownloadOrStream?dcsid=...&displayName=...&dcsExt=PDF",
  "mimeType": "application/pdf"
}
```

The `downloadUrl` comes back as a path relative to `/mychartcn`. OpenKP
prepends `/mychartcn` if it's not already there.

### Step 3: GET the PDF

```
GET /mychartcn/Documents/ViewDocument/DownloadOrStream?dcsid=...&displayName=...&dcsExt=PDF
```

Headers: `Accept: application/pdf`, `Referer: <details page URL>`.
**No CSRF header required** for this GET.

Response:
- `Content-Type: application/pdf`
- `Content-Disposition: attachment; filename="..."`
- Body: binary PDF bytes (observed 126 KB for a cardiac device report).

OpenKP saves the bytes to `~/.openkp/downloads/<safe_filename>.pdf` and
returns the local path to Claude. Bytes never flow back through the MCP
transport — too big for LLM context.

## Filename safety

Kaiser's `displayName` can contain characters that are invalid on the local
filesystem (path separators, Windows-reserved chars, control characters).
`_safe_filename` replaces these with underscores and caps length at 180
chars.

## Known unknowns

- **Pagination cursor.** `maxResults` caps one page but there's likely a way
  to page older results. Not in this HAR's traffic. Would need a capture of
  clicking "Load more" or similar if the UI exposes it.
- **Date range filtering.** `loadStartInstantISO` or similar wasn't observed
  on the GetList body. If Kaiser supports it, we haven't mapped it yet.
- **Attachments within a lab result.** We saw `scans` and `imageStudies`
  fields as empty arrays in the details response. Their shape for results
  that actually have inline scans is unknown.
- **`GetDocumentGenerationInfo` status values.** Observed `"Generated"`.
  Other values ("Pending", "Failed"?) are plausible but unconfirmed.
