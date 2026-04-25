# Medication endpoints

Source HAR: `docs/research/captures/kp-medications-1.har`, 2026-04-25.
Source body capture: `docs/research/captures/rxDetails-all.json` (response bodies for BFF calls were dropped from the HAR by Chrome DevTools, see "HAR limitations" below).

## Summary

| Feature | Endpoint | Status |
| --- | --- | --- |
| Active + recent meds (full list) | `GET /kp/mycare/pharmacy-microservices/rx-cost-inventory-bff/v1/rxDetails?prescriptions_filter=all` | ✅ Shipped, live-verified (session 8) |
| Refillable subset only | same endpoint with `prescriptions_filter=fillable` | ✅ Shipped — pass `filter="fillable"` to `list_medications` |
| Pending refill orders | `GET /kp/mycare/pharmacy-microservices/rx-order-management-bff/v1/orderStatus` | 🟡 Endpoint observed, response body not captured. Out of scope for v1. |
| Drug education leaflet by NDC | `GET /kp/mycare/pharmacy-microservices/pharmacy-center-kpweb-bff/v1/medGuide?ndc=...` | 🟡 Endpoint observed, deferred (v2). |
| Pill image by NDC | `GET .../pharmacy-center-kpweb-bff/v1/drugImage?ndc=...` | ⚪ Out of scope. |
| Per-Rx auto-refill toggles | `GET .../pharmacy-center-kpweb-bff/v1/rxnotificationpreferences` | 🟡 Read side useful, write side belongs to Phase 3. |
| Cart, shipping address, transfer | `GET .../pharmacy-center-kpweb-bff/v1/{cart,address,rxTransferDetails}` | ⚪ Order placement, Phase 3 territory. |

MCP tool registered (v1): `list_medications`. Backed by a single call to `rxDetails?prescriptions_filter=all` plus a one-shot GUID lookup against `/mycare/v1.0/user`.

**Live-verified 2026-04-25 (session 8).** Cookie-crossover hypothesis confirmed: session cookies set on `.kaiserpermanente.org` ride from `healthy.kaiserpermanente.org` to `apims.kaiserpermanente.org` automatically. No additional auth handshake needed for the BFF.

## Architectural shift — new host, new auth model

This is the first OpenKP scraper that talks to the BFF microservices host
`apims.kaiserpermanente.org`. Every prior tool (`get_profile`, `list_messages`,
`read_message`, `list_lab_results`, `read_lab_result`, `download_lab_result_pdf`)
hits `healthy.kaiserpermanente.org/mychartcn/...` and authenticates by riding
the MyChart session cookie. The medications page abandons that path entirely.
There is no `/mychartcn/api/medications` endpoint — only the BFF.

Implications for the scraper layer:

- Different host means a different `httpx.AsyncClient` base URL (or a per-call
  override on the existing client).
- Different auth header set (see "Required headers" below). The pharmacy
  `X-apiKey` / `X-appName` contract used by `get_profile` does NOT apply.
- CORS behavior on the BFF (`Access-Control-Allow-Credentials: true` + a
  specific allowed origin) tells us the browser was sending cookies cross-site.
  Both hosts are subdomains of `kaiserpermanente.org`, so any session cookie
  scoped to `Domain=.kaiserpermanente.org` rides along automatically. **Live
  probe required to confirm our existing session works.** If it doesn't, we
  have an unknown-shape handshake to discover. Treat this as the highest
  implementation risk for `list_medications`.

## Required headers (observed)

```
Origin: https://healthy.kaiserpermanente.org
X-IBM-client-Id: 9dea3678-801b-4069-b111-4d3c5f56c9de
x-guid: <user GUID, same value we extract from /mycare/v1.0/user>
x-region: MRN
x-idType: guid
Content-Type: application/json
Accept: */*
```

Plus per-call feature flags on `rxDetails`:

```
x-benefitsIndicator-feature: true
x-cost-feature: true
x-prescriptionsfilter: <"all" or "fillable", duplicates the query param>
```

Plus the curiosities we should send verbatim and not wonder about:

```
X-KPSessionID: undefined        (literally the string "undefined")
X-disablecache: false
X-Global-Transaction-Id: <hex string, presumably client-generated tracing ID>
```

`X-IBM-client-Id` is presumably a public client ID hardcoded into Kaiser's React
bundle (analogous to how `X-apiKey` works on `/mycare/v1.0/user`). The same
value appears across all BFF endpoints in the capture.

`x-region: MRN` is the same noisy sentinel we already filter when parsing
profile data. The front end is just passing it through. Send it as observed.

The browser also sends an OPTIONS preflight for each unique URL. Our httpx
client can skip preflights — they're a browser-only enforcement of CORS, not a
server-side requirement.

## HAR limitations

Chrome DevTools dropped the response body for every JSON BFF call in this
capture (`content.text == null` despite non-zero `content.size`, no
`content-encoding`). This is a known DevTools quirk with certain CORS
responses. Hugo pasted the rxDetails response separately into
`docs/research/captures/rxDetails-all.json`.

Bodies we still need if we ever surface these endpoints:

- `rx-order-management-bff/v1/orderStatus` (65 bytes — likely an empty list
  when no pending orders exist)
- `pharmacy-center-kpweb-bff/v1/medGuide?ndc=...`
- `pharmacy-center-kpweb-bff/v1/rxnotificationpreferences`

Capture by re-opening the meds page in DevTools, clicking the request, and
copy/pasting from the Response tab.

## `GET /kp/mycare/pharmacy-microservices/rx-cost-inventory-bff/v1/rxDetails`

**Query parameters:**

- `prescriptions_filter`: `all` (everything) or `fillable` (refill-eligible
  subset). The `all` response is a strict superset — we always call with
  `all` and filter client-side if needed.

**Note the URL has a leading empty parameter:** the captured form is
`?&prescriptions_filter=all` (extra `&` after `?`). httpx normalizes this
away, no special handling needed.

**Response shape (real data, four prescriptions):**

```json
{
  "executionContext": {
    "statusCode": "000",
    "statusDetails": "Success"
  },
  "member": {
    "guid": "1234567",
    "mrn": "14776978",
    "firstName": "HUGO",
    "lastName": "TESTLAST",
    "shipToState": "CA",
    "InsurancePlans": ["COMMERCIAL", "MFAP", "CASH"],
    "isM3PMem": false,
    "isTeenUser": false
  },
  "prescriptions": {
    "rxRefillResponse": true,
    "rxcomid": "72556778",
    "lastDispensedNDCs": ["00378022201", "70010078301", ...],
    "pickUpReadyDispenseLocCodes": [],
    "recentRefillableCount": 2,
    "totalRefillableCount": 3,
    "recentNonRefillableCount": 1,
    "totalNonRefillableCount": 1,
    "fillable": [ /* per-Rx objects */ ],
    "nonFillable": [ /* per-Rx objects */ ]
  }
}
```

**Per-Rx object (the fields that matter for v1):**

| Kaiser field | Type | Notes |
| --- | --- | --- |
| `medicineName` | str | Internal name, e.g. `"CHLORTHALID 25 MG TAB MYLA"`. Capitalization is inconsistent across Rx (some all-caps, some title case). |
| `commonBrandName` | str | Brand name, e.g. `"HYGROTON"`. |
| `consumerInstructions` | str | Sig text, e.g. `"Take 1 tablet by mouth daily"`. |
| `consumerName` | str | Prescriber, e.g. `"PROVIDER TWO MD"`. |
| `prescribedOn` | str | Initial Rx date, US format `"01/26/2026"`. |
| `lastRefillDate` | str | Last refill, US format OR literal `"N/A"` for never-filled. |
| `lastSoldDate` | str/null | When patient picked it up. Null if not yet sold. |
| `nextFillDate` | str (optional) | When next fill happens. Only present on refillable Rx. |
| `refillsRemaining` | str | Count as a string, e.g. `"2"`, `"0"`. |
| `mailable` | str | `"true"` or `"false"`, as a STRING not a bool. |
| `firstFill` | bool | True if no fills have happened yet. |
| `isNewPrescription` | bool | New prescription flag (distinct from `firstFill`). |
| `recentRx` | bool | "Recently filled or active." Drives `recent_*` count buckets. |
| `recentRxDate` | str | **Date format inconsistent across rows in the same response** — see Quirks below. |
| `refillEligible` | bool | Whether a refill can be ordered right now. |
| `autoRefill` | bool | Auto-refill currently ON for this Rx. |
| `autoRefillEligible` | bool | Auto-refill is *available* (not currently on). |
| `rarCodeKey` | str | Reason code when refill is blocked, e.g. `"Rx_NoMoreRefills"`. Empty when no block. |
| `rarStatus` | bool | Whether `rarCodeKey` reflects a real block. |
| `drugEncyclopediaLink` | str | Relative URL to KP's drug encyclopedia entry. |
| `afcInfo.rxnbr` | str | Rx number. |
| `afcInfo.dispenseddayssupply` | int | Days supply. |
| `afcInfo.copay` | float | Copay in dollars. |
| `afcInfo.nextFillEligibleDate` | str | When refill becomes eligible, US format. |
| `afcInfo.lastdispensedndc` | str | NDC code (links to drug-image and med-guide endpoints). |
| `afcInfo.mrnrgn` | str | Patient region — `"NCA"` here. **Clean value, unlike most region fields.** |
| `afcInfo.deacode` | str | DEA schedule. `"6"` = uncontrolled. Need a sample with a controlled substance to confirm encoding for II–V. |
| `afcInfo.prn` | bool | "As needed" indicator. |
| `afcInfo.compound` | bool | Is this a compound? |
| `fillOptions` | list | Per-delivery-method pricing. See below. |
| `RxCustomIndicators` | list | Generic key/value indicators. Only `PRN` observed so far, duplicating `afcInfo.prn`. |

**`fillOptions[]`:**

```json
{
  "deliveryMethod": "L",   // "L" = local pickup, "M" = mail
  "daysSupply": 100,
  "quantity": 100,
  "otherCoverageCode": "8",
  "copaystts": "000",       // "000" = approved
  "copaysttsdtl": "Approved",
  "ucCharge": 170.11,       // Usual & customary charge
  "planPay": 60,            // What the plan pays
  "estimatedCopay": 0
}
```

For v1 we surface `fillOptions` as-is in the model — it's denormalized but
readable. Callers can pluck the L vs M variant.

## `fillable[]` vs `nonFillable[]` — the bucketing is misleading

The two buckets do **not** mean "active" vs "expired." Sample data:

- Dabigatran has `refillsRemaining: "0"`, `refillEligible: false`,
  `rarCodeKey: "Rx_NoMoreRefills"` — and is in `fillable[]`.
- Verapamil has `refillsRemaining: "1"`, `refillEligible: true`,
  `autoRefillEligible: true` — and is in `nonFillable[]`. The blocker
  appears to be `nextFillEligibleDate` being too far in the future
  (May 29 vs today April 25) combined with `mailable: "false"`.

Working theory: `nonFillable[]` = "currently blocked from being placed in a
refill order via this UI right now," for any combination of timing /
delivery / inventory / regulatory reasons. `fillable[]` = "everything else,
including expired-refills Rx that the patient might want to know about."

For our parser: **merge the two buckets into a single list** and let the
per-Rx fields (`refillsRemaining`, `refillEligible`, `nextFillEligibleDate`,
`rarCodeKey`) speak for themselves. We can expose the bucket as a hint field
(`is_currently_orderable: bool`) but should not let it shape the user-visible
structure. Less surprise for downstream callers.

## Quirks

- **Mixed date formats in the same response.** `recentRxDate` is ISO
  (`"2026-01-26"`) on the first Rx and US (`"01/26/2026"`) on every other
  Rx in the same payload. All other date fields are US format (so far).
  Parser must accept both ISO and US.
- **`lastRefillDate` may be the literal string `"N/A"`.** Map to `None`.
- **`mailable` is a string, not a bool.** `"true"` / `"false"`. Coerce
  explicitly.
- **`refillsRemaining` is a string, not an int.** Coerce explicitly.
- **`afcInfo` is missing `compound` on some rows** (e.g. metoprolol). Treat
  as optional in the model.
- **Top-level `executionContext.statusCode == "000"` is the success
  signal.** Same convention as labs/messages. Anything else → raise or
  return an empty list with a status note. Verify the `008x` warning codes
  empirically (we see one warning code `8051` with no message — appears
  benign).
- **`member.firstName` etc. duplicate data we already get from
  `get_profile`.** Ignore. The user identity is established before we ever
  call this endpoint.
- **`/mycare/v1.0/user` returns a different envelope when you ask for a
  single inclusion path.** A request with
  `X-inclusionJsonPath: $.UserAccountData.userIdentityInfo.guid` (one path)
  comes back without the `UserAccountData` wrapper our parser expects. The
  multi-path form (`profile.INCLUSION_PATHS` joined by `;`) returns the
  full envelope. Use the multi-path form even when you only need one
  field — the extra bytes cost less than the response-shape surprise.
  Discovered live in session 8 by `_fetch_user_guid` failing the first
  three live tests.
- **GUID can be a JSON number rather than a string.** `userIdentityInfo.guid`
  comes back as `1234567` (int), not `"1234567"` (str), in real responses.
  `profile._str_or_none` already handles this transparently — `medications.py`
  uses the same `str(value).strip()` coercion via `_coerce_guid`. Don't
  use `isinstance(str)` checks on Kaiser identity fields.

## Field mapping to `Medication` model (planned)

| Kaiser field | Our `Medication` field | Coercion |
| --- | --- | --- |
| `medicineName` | `name` | Title-case if all-caps? Or pass through verbatim? **Open question.** |
| `commonBrandName` | `brand_name` | Pass through. |
| `consumerInstructions` | `instructions` | Pass through. |
| `consumerName` | `prescriber` | Pass through. |
| `afcInfo.rxnbr` | `rx_number` | Pass through. |
| `afcInfo.dispenseddayssupply` | `days_supply` | int. |
| `afcInfo.copay` | `copay` | float. |
| `afcInfo.lastdispensedndc` | `ndc` | Pass through. |
| `afcInfo.mrnrgn` | `region` | Pass through (clean value here). |
| `afcInfo.deacode` | `dea_schedule` | Coerce `"6"` → "uncontrolled", others verified live. |
| `afcInfo.prn` OR `RxCustomIndicators[PRN]` | `is_as_needed` | bool. |
| `prescribedOn` | `prescribed_on` | Date — accept US + ISO. |
| `lastRefillDate` | `last_refill_date` | Date — accept US + ISO + `"N/A"` → None. |
| `lastSoldDate` | `last_sold_date` | Date — accept US + ISO + None. |
| `nextFillDate` | `next_fill_date` | Date or None (field absent on some rows). |
| `afcInfo.nextFillEligibleDate` | `next_fill_eligible_date` | Date. |
| `refillsRemaining` | `refills_remaining` | str → int. |
| `mailable` | `is_mailable` | str → bool. |
| `firstFill` | `is_first_fill` | bool. |
| `isNewPrescription` | `is_new_prescription` | bool. |
| `refillEligible` | `is_refill_eligible` | bool. |
| `autoRefill` | `auto_refill_on` | bool. |
| `autoRefillEligible` | `auto_refill_eligible` | bool. |
| `rarCodeKey` (when `rarStatus == true`) | `refill_blocked_reason` | Map known codes to human strings; pass through unknown. |
| `drugEncyclopediaLink` | `drug_info_link` | Prepend host. |
| `fillOptions` | `fill_options[]` | Pass through as `MedicationFillOption` records. |
| (derived from `fillable` vs `nonFillable`) | `is_currently_orderable` | bool. |

## MCP tool surface (planned)

```python
@mcp.tool()
async def list_medications(filter: str = "all") -> dict:
    """List active and recent prescriptions.

    filter: "all" (default) | "fillable" — fillable filters to refill-eligible only.
    Returns dict with `medications: [Medication]`, plus summary counts.
    """
```

No `read_medication` tool in v1. Every field we'd want at detail level is
already in the list response. If we later decide to surface medGuide
(drug education leaflet), that becomes its own tool keyed by NDC.

## Open questions

1. ~~Does our existing session cookie cross to `apims.kaiserpermanente.org`
   automatically?~~ **Resolved (session 8): yes.** Session cookies scoped
   to `.kaiserpermanente.org` ride to the BFF host with no extra work.
2. Is `X-IBM-client-Id` truly static across users / sessions, or does it
   rotate? If it rotates, where does the React bundle pull it from?
   (Hardcoded in our scraper for now — works in practice.)
3. Does `prescriptions_filter=fillable` return a strict subset of `=all`
   in field count and shape, or does it hide / collapse fields? Surfaced
   as a `filter` arg on `list_medications` but not yet exercised in
   anger.
4. What does the response look like for a member with zero meds? Does
   `prescriptions.fillable` become `[]` or null or absent?
5. DEA schedule encoding: `deacode: "6"` for chlorthalidone (uncontrolled).
   What does it look like for a Schedule II / IV med? (Need a sample with
   a controlled-substance Rx to confirm.)
6. The `executionContext.statusCode` codes beyond `"000"` — what do
   warnings/partial-failures look like in real responses?

## Implementation sketch

1. Add a small `KaiserBFFRequest` helper next to `KaiserRequest` that
   builds the BFF host URL, attaches the standard header set, and reuses
   the existing session cookie jar from `KaiserSession`. Keep
   `KaiserRequest` untouched for the MyChart family.
2. `scrapers/medications.py` calls `rxDetails?prescriptions_filter=all`,
   merges `fillable` + `nonFillable`, parses each into `Medication`.
3. Parser tolerates missing/null fields per ADR-005's "never raise on
   missing fields" rule.
4. Pydantic model `Medication` + `MedicationsResponse` (with summary counts
   on the wrapper).
5. MCP tool `list_medications` returns `MedicationsResponse.model_dump()`.
6. Tests modeled on `test_labs.py`, mocking `httpx.AsyncClient`. Use the
   real captured payload in `rxDetails-all.json` as the success-path
   fixture.
