# Refill endpoints

Source HAR: `docs/research/captures/kp-refill-1.har`, 2026-04-25 (real mail-order refill, captured live).
Confirmation page screenshot: `docs/research/captures/kp-refill-1-confirmation.png` (referenced from session 11).

This is the **first Phase 3 write tool** — `request_refill(medication_id)` — and surfaces design decisions that don't apply to any of the read tools.

## v1 scope (locked 2026-04-25)

**Mail-order only.** `request_refill` ships supporting `deliveryMethod: "M"` exclusively in v1. Local pickup (`deliveryMethod: "L"` or its own `placeorderLocal` variant) is **deferred to v2** because:
- The reference patient's typical refill pattern is mail-order, so the v1 capture path is the one we have.
- Mail-order avoids needing in-store pharmacy selection / time-window logic.
- Local pickup needs a separate HAR capture we don't have yet.

**TODO before v2 local-pickup work starts:** capture HAR for an in-store refill on a real prescription. The endpoint is presumably `POST .../rx-place-order-bff/v1/placeorderLocal` (mirroring `placeorderMail`) but we have not confirmed. Until that capture exists, do NOT extend `request_refill` to accept `deliveryMethod: "L"` — the body schema is unknown and a malformed request could either fail or place an order with bad data.

**Payment posture:** KP already has card-on-file (and/or `optionalPayment: true` semantics, see screenshot quote below). We pass through whatever `/walletV3` returns. We do NOT collect, prompt for, or store payment info ourselves. From KP's confirmation screen, verbatim: *"Your estimated copay is $0, and you've provided an optional payment method. If it's determined that a copay is required for this order, this payment method will be charged."* Our `request_refill` response surfaces estimated copay but **does not echo card last-4, expiry, or holder name** — those live in KP's UI and our audit log scrubs them.

## Summary

| Step | Endpoint | Method | Purpose | Status |
| --- | --- | --- | --- | --- |
| 1 (prep) | `GET .../pharmacy-center-kpweb-bff/v1/cart` | GET | Initialize / read server-side cart state | 🟡 Captured (size 814b), body elided by HAR |
| 2 (prep) | `GET .../pharmacy-center-kpweb-bff/v1/address` | GET | Patient's saved shipping address | 🟡 Captured (275b), body elided |
| 3 (prep, mail only) | `GET .../paymentsts-bff-walletbff/v1/walletV3` | GET | Saved payment methods → `walletPaymentToken` | 🟡 Captured (814b), body elided. Required for mail copay > $0. |
| 4 (prep, mail only) | `GET .../pharmacy-center-kpweb-bff/v1/shippingDate?...` | GET | Estimated delivery date string | 🟡 Captured (108b), body elided. Cosmetic, may be omittable. |
| 5 | `POST .../pharmacy-center-kpweb-bff/v1/cart/prescription` | POST | Add prescription to cart | ✅ Captured |
| 6 (mail only) | `POST .../pharmacy-center-kpweb-bff/v1/rxdeliveryeligibility` | POST | Validate delivery to patient zip | ✅ Captured (response 54b, elided) |
| 7 | `POST .../rx-place-order-bff/v1/placeorderMail` | POST | **Commit the refill** | ✅ Captured |
| 7 (alt) | `POST .../rx-place-order-bff/v1/placeorder*` for local pickup | POST | Commit refill to local pharmacy counter | ⚪ Not captured. Reference capture is mail order only. Need a fresh HAR for in-store pickup. |

MCP tool registered (target): `request_refill(medication_id, delivery_method)`. Returns an `OrderConfirmation` with `order_number`, `placed_at`, `delivery_method`, and per-Rx `response_code`.

**Live-verified path:** mail-order, copay = $0.00 (insurance covered), walletPaymentToken present in capture. **Not yet verified:** copay > $0, local pickup, missing payment method, ineligible delivery zip.

## Host and auth

Same BFF host as `list_medications`: `apims.kaiserpermanente.org`. Cookie-crossover from the existing session works (already proven in session 8). The pharmacy `X-apiKey` / `X-appName` contract from `/mycare/v1.0/user` does NOT apply to the BFF.

## Required headers (observed)

Pattern matches `medications.py` but with subtle naming variations across endpoints. **Send headers verbatim** — the BFF appears strict about case and exact header names.

For all calls:

```
Origin: https://healthy.kaiserpermanente.org
Referer: https://healthy.kaiserpermanente.org/
X-IBM-client-Id: 9dea3678-801b-4069-b111-4d3c5f56c9de
Content-Type: application/json   (POSTs only)
```

For `walletV3` specifically (uses pharmacy `X-apiKey` style, not BFF style):

```
X-apiKey: kprwdpharmctr68973257122561335296
X-appName: rwd
X-region: MRN
X-idType: guid
X-Requested-With: XMLHttpRequest
X-sessionToken: false
X-env: undefined
X-osversion: Mac OS 10.15.7
X-useragenttype: Macintosh
X-useragentcategory: B
```

For `cart`, `address`, `cart/prescription`, `rxdeliveryeligibility`:

```
x-benefitsIndicator-feature: <true|false, varies per endpoint>
x-cost-feature: true
X-idType: guid    (address only)
```

For `placeorderMail`:

```
X-id: <patient GUID, e.g. 1234567>
X-region: MRN
x-idType: GUID    (note: capitalized vs other endpoints)
x-cost-feature: true
x-benefitsIndicator-feature: false
```

For `shippingDate`:

```
X-ZipCode: 90210
X-state: CA
x-OrderPlacedDate: 04/25/2026
```

OPTIONS preflights are sent by the browser for every unique URL but httpx doesn't need to replicate them. (Confirmed in session 8.)

## Request bodies

### POST `cart/prescription`

```json
{
  "identifier": {
    "id": "",
    "type": "",
    "removeRDOptions": false,
    "isM3PMem": false,
    "InsurancePlans": ["COMMERCIAL", "MFAP", "CASH"],
    "applyFeature": true,
    "isTeenUser": false,
    "isOrderByRx": false
  },
  "cartItems": { "<GUID>": [] },
  "rxNumber": "<stringified JSON of the full prescription detail object from rxDetails>"
}
```

The `rxNumber` field is named misleadingly — it's a stringified JSON dump of the whole prescription object that came from `rxDetails`. We can re-fetch this with `list_medications` internally and serialize it into the body.

### POST `rxdeliveryeligibility`

```json
{
  "cartItems": { "<GUID>": [<full prescription detail object, NOT stringified>] },
  "postalCode": "<patient zip from profile>"
}
```

Same prescription metadata as cart/prescription, but as a real array (not stringified) inside `cartItems[GUID]`.

### POST `placeorderMail`

The big one. Schema (truncated):

```json
{
  "placeOrderReq": {
    "deliveryMethod": "M",
    "transactionControlReference": "<client-generated UUIDv4>",
    "optionalPayment": true,
    "placerDetails": {
      "placerID": { "placerIdType": "GUID", "placerIDValue": "<GUID>" },
      "placerName": { "firstName": "...", "lastName": "...", "middleName": "" },
      "preferredLanguage": "EN",
      "region": "MRN",
      "emailID": "<from profile>",
      "mobileNumber": "<from profile, digits only>"
    },
    "rxDetails": {
      "rxInfo": [{
        "id": "<GUID>", "idType": "self",
        "memberName": {...},
        "rxNumber": "...", "rxName": "...",
        "nhinId": "...", "mrn": "...", "mrnRegion": "NCA",
        "daysSupplyRequested": 100, "daysSupplyMax": 100,
        "responseCode": "00",
        "estimatedCopay": "0.00",
        "estimatedCopaySource": "CE"
      }]
    },
    "cosmosData": {
      "member": {...},               // member identity, insurance plans
      "prescriptions": [<full Rx detail blob>]
    },
    "sourceApplication": "WPP",
    "sourceChannel": "WEB",
    "shippingAddress": {
      "street1": "...", "city": "...", "state": "CA", "zip": "...",
      "singleUse": false
    },
    "creditCardDetails": {
      "oneTimeUse": false,
      "cardHolderName": {...},
      "billingAddress": { "zipCode": "..." },
      "expiryDate": "MM/YY",
      "last4Digit": "....",
      "cardType": "AM",
      "walletPaymentToken": "<opaque token from /walletV3>"
    },
    "deliveryStatus": "Estimated to arrive between ... and ...",
    "regionalPhone": "1-888-218-6245"
  }
}
```

## Response shape (`placeorderMail`)

```json
{
  "submittedBy": {
    "placerName": {...},
    "placerID": {"placerIdType": "MRN", "placerIDValue": "<MRN>"}
  },
  "Order": {
    "orderPlacedDate": "MM/DD/YYYY HH:MM:SS",
    "transactionControlRefNo": "<echo of request UUID>",
    "orderNumber": "<order number, format: 030<UUID><timestamp>>"
  },
  "rxRefillArray": {
    "rxRefill": [{
      "rxNumber": "...",
      "mrn": "...",
      "memberName": {...},
      "rxOrderResponseCode": "000",
      "mrnRegion": "NCA"
    }]
  },
  "deliveryMethod": "M",
  "executionContext": {
    "statusDetails": "Success",
    "statusCode": "000"
  },
  "region": "NCA",
  "sourceApplication": "WPP"
}
```

`executionContext.statusCode == "000"` and `rxRefill[].rxOrderResponseCode == "000"` are the success markers. Any other code surfaces as a partial-success or failure.

## Confirmation page observations (from screenshot)

The post-submit screen at `/mychartcn/.../order-confirmation` (after `placeorderMail` returns 200) shows what KP itself surfaces to the patient. Useful signal for shaping our tool's response and for knowing what's adjacent-but-deferred:

- **Order number is NOT user-visible on this page.** The `Order.orderNumber` from `placeorderMail` (e.g. `030<UUID><timestamp>`) is an internal reference. Surface it from `request_refill` as an opaque ID for our own tracking, but warn it may not match what the patient sees if they "View order details" later. (Actual KP-facing order tracking probably comes from `/orderStatus`, see `medications.md`.)
- **Order status notifications are automatic.** "You'll receive updates on the status of your order by text message at <phone> and by email at <email>." We do NOT need a "subscribe to notifications" tool. KP pushes these out of the box.
- **Auto-refill upsell on this page.** "Your prescription for X is eligible for our Auto Refill program." Two buttons: "Set up Auto Refill" / "No thanks". This is a separate write capability (likely hits `rxnotificationpreferences`, see `medications.md`). **Out of scope for `request_refill`.** Future tool: `enroll_auto_refill(medication_id)`.
- **"View order details" link** → presumably the `/orderStatus` endpoint. **Out of scope for `request_refill`.** Future tool: `track_refill(order_number)`.
- **Cancel flow** is not visible from this page. Presumably lives inside order details. **Out of scope.**
- **Estimated delivery window** is shown verbatim ("Estimated to arrive between Wednesday, April 29 and Friday, May 1"). This string came back to us in the `placeorderMail` request body's `deliveryStatus` field. Pass it through to the user.

## Cancellation: not a self-service surface

Verified 2026-04-25: Kaiser does **not** expose a self-service "cancel order"
endpoint on either the web app or the mobile app. To cancel a refill order
the patient must phone the pharmacy where the order was placed. KP's own
help page reads: *"To cancel a prescription order with Kaiser Permanente,
call the pharmacy where the order was placed as soon as possible. If the
medication is being mailed, immediately contact the Kaiser Permanente
Pharmacy Services center to check if the shipping process can be stopped."*

Implications:

- No `cancel_refill_order` tool to build. Dropped from Phase 3 scope.
- No HAR to capture for cancellation work.
- **Raises the bar on our confirm-before-act gate.** Once `request_refill`
  with `confirm=True` returns a successful `OrderConfirmation`, the patient
  cannot roll it back through OpenKP — they have to call the pharmacy. The
  two-call confirm pattern (preview, then explicit `confirm=True`) is the
  right design precisely because there's no undo.

## Open design questions (resolved or remaining)

1. ~~**Mail-order vs. local pickup.**~~ **Resolved 2026-04-25:** mail only for v1, local deferred to v2. See "v1 scope" above.

2. **Payment token handling.** Resolved at the contract level: pass through `walletPaymentToken` from `/walletV3` verbatim, treat `optionalPayment: true` as KP intends, don't echo card last-4 in our tool response, redact card details from audit log. Still untested:
   - What does a no-saved-card user look like in `/walletV3`? (We assume KP requires one for mail-order; will surface a clear error if walletV3 returns empty.)
   - Does copay > $0 actually require a valid token? Likely yes, but we won't know until a copay-bearing refill is attempted.

3. **Header naming inconsistency.** `X-id` (placeorder) vs `x-guid` (medications) vs `X-idType: guid` (address) vs `x-idType: GUID` (placeorder). Some are case-sensitive, some aren't. Send each verbatim, do not normalize.

4. **State sequencing.** Browser does `GET /cart` → adds to cart → eligibility → place order. Untested whether httpx can skip `GET /cart` (cart may be created on-demand by `cart/prescription`). Worth a try-and-see.

5. **`cosmosData.prescriptions[]` is enormous.** ~3KB of duplicated metadata per Rx. Do we strictly need all of it? The Kaiser frontend just dumps everything it had. Conservative move: send everything we got from rxDetails verbatim. Optimization for later.

## Safety patterns this enables (DESIGN §8)

- **Confirm-before-act:** `request_refill` previews the rxName, copay, delivery method, shipping address, and last-4 of payment card BEFORE calling `placeorderMail`. The user (or Claude on their behalf) confirms.
- **Audit log:** Every call writes to `~/.openkp/audit.log` with timestamp, tool, args, and outcome.
- **Dry-run:** `OPENKP_DRY_RUN=1` short-circuits before `placeorderMail` and returns a fake `OrderConfirmation` with `dry_run: true`. The prep GETs (cart, address, wallet, shippingDate, eligibility) DO run because they're idempotent reads — useful for catching shape mismatches without spending a refill.

## HAR limitations

Chrome's first HAR export (`kp-refill-1.har`, 2026-04-25 mid-afternoon) elided response bodies for the four prep GET endpoints. The follow-up capture (`kp-refill-2.har`, 2026-04-25 evening, captured AFTER the chlorthalidone refill) **did include the response bodies** — Chrome's elision behavior seems intermittent. See "Verified response shapes" below.

## Verified response shapes (from kp-refill-2.har)

These shapes were not visible in the first HAR. Code in `refill.py` is now driven by these.

### `GET /walletV3` response

```json
{
  "card": [
    {
      "accountType": "creditcard",
      "billingAddress": "",                  // empty STRING, not a sub-object
      "cardType": "American Express",        // full name, NOT the 2-letter code
      "ccName": "Fake Person",
      "ccNumber": "2000",                    // last-4 of card
      "defaultOption": "true",
      "expDate": "2802",                     // YYMM format (year-then-month, 4 digits)
      "firstName": "...",
      "lastName": "...",
      "middleInitial": "",
      "nickName": "AMEX",
      "paymentToken": "<opaque>",            // ← payload for placeorderMail
      "zipCode": "90210"
    },
    null
  ],
  "check": [],
  "defaultToken": "<opaque>",
  "profileId": "<opaque>",
  "walletKey": ""
}
```

**Field mapping into `placeorderMail.creditCardDetails`:**

| placeorderMail field | walletV3 source | Transform |
| --- | --- | --- |
| `last4Digit` | `ccNumber` | passthrough |
| `expiryDate` | `expDate` | "2802" → "02/28" (YYMM → MM/YY) |
| `cardType` | `cardType` | "American Express" → "AM"; lookup table for Visa/MC/Discover (Amex verified, others inferred) |
| `cardHolderName.firstName` | `firstName` | siblings of paymentToken, NOT a sub-object |
| `cardHolderName.lastName` | `lastName` | siblings |
| `cardHolderName.middleName` | `middleInitial` | siblings (rename) |
| `billingAddress.zipCode` | `zipCode` | sibling, NOT inside `billingAddress` (which is empty string) |
| `walletPaymentToken` | `paymentToken` | rename |

### `GET /shippingDate` response

```json
{
  "region": "CN",
  "state": "CA",
  "zipCode": "90210",
  "from": "04/29/2026",
  "to": "05/01/2026",
  "fromDays": 3,
  "toDays": 5
}
```

The `deliveryStatus` string KP echoes into the placeorderMail body and onto the order-confirmation page (e.g. `"Estimated to arrive between Wednesday, April 29 and Friday, May 1. "`) is **constructed by Kaiser's frontend** from `from`/`to`. We compose the same string in `_compose_delivery_window`.

### `POST /rxdeliveryeligibility` response

```json
{"statuscode": "600", "rxinfo": [], "deliverytypeinfo": {}}
```

Despite `statuscode: "600"` (not "000"), this is a successful eligibility check — the placeorderMail call that follows succeeds. Treat 600 as OK for v1; revisit if we ever see other codes.

## Eligibility relaxation (verified 2026-04-25)

Live test showed Kaiser's `refillEligible` flag can lag reality. Specifically: a chlorthalidone Rx in the `fillable[]` bucket with `next_fill_eligible_date: 04/16/2026` (in the past) had `refillEligible: false` per `rxDetails`, yet kp.org's pharmacy UI offered an "Add to order" button and **the order placed successfully** when submitted via KP UI.

`_build_preview` now treats `bucket=fillable` + `next_fill_eligible_date` ≤ today as the authoritative "yes" signal and skips the stale-flag warning. The `nonFillable` bucket and a future date both still block.

## Bonus discoveries from kp-refill-2.har

These are not part of v1 but worth recording so we don't re-investigate:

- **`cancelOrderUri` is exposed in the cart response's `ui_config.apiRestUris`.** Even though kp.org's UI doesn't show a cancel button, the BFF appears to expose a cancellation endpoint. Worth investigating before assuming "no self-service cancel" is final. (KP's help docs still say to call the pharmacy.)
- **`placeOrderPickupApiUri` is in the same `apiRestUris` block** — confirms the local-pickup commit endpoint exists. Future v2 reference for when we add `deliveryMethod="L"`.
- **`csrfToken` lives in `ui_config.apiRestUris.csrfToken`** in the cart response. We don't currently send one to the BFF and orders still place — but worth knowing this exists if any endpoint ever requires it.
- **Adjacent endpoints surfaced in this capture (Phase 3 backlog):** `GET /orderStatus` (track refill — already noted in medications.md), `GET /rxnotificationpreferences` (read auto-refill state), `GET /paytoprovider`, `GET /medGuide`, `GET /drugImage`, `GET /rxTransferDetails`.

## `GET /orderDetails` — captured 2026-04-25

The "View order details" page on kp.org is backed by `GET .../rx-order-management-bff/v1/orderDetails?ordernum=<orderNumber>&pharmacyId=`. Backs the `track_refill_order(order_number)` MCP tool.

### Request

```
GET https://apims.kaiserpermanente.org/kp/mycare/pharmacy-microservices/rx-order-management-bff/v1/orderDetails
    ?ordernum=030&lt;orderId&gt;&lt;timestamp&gt;
    &pharmacyId=
```

`pharmacyId` is sent empty for mail orders. (For local-pickup orders it presumably carries the dispensing pharmacy code — out of scope for v1.)

Headers are a pared-down BFF set. **Notably absent vs `rxDetails`/`cart`:** no `x-guid`, no `X-region`, no `X-KPSessionID`, no `X-idType`. The endpoint resolves the order via `ordernum` alone — patient identity comes from session cookies.

```
Accept: */*
Content-Type: application/json
Origin: https://healthy.kaiserpermanente.org
Referer: https://healthy.kaiserpermanente.org/
X-IBM-client-Id: 9dea3678-801b-4069-b111-4d3c5f56c9de
x-cost-feature: true
x-benefitsIndicator-feature: false
```

### Response (captured `INPROGRESS` state)

```json
{
  "orderNumber": "030&lt;orderId&gt;&lt;timestamp&gt;",
  "orderType": "MAIL",
  "orderStatus": "INPROGRESS",
  "digitalStatus": "In Progress",
  "orderPlacedDate": "2026-04-25T23:50:52.207Z",
  "orderComittedDate": "2026-04-25T23:50:54Z",
  "placerName": "&lt;PLACER_NAME&gt;",
  "paymentInfo": [
    { "cardDigits": "<last4>", "expiryDate": "MM/YYYY",
      "cardType": "American Express", "cardImageCode": "AM" }
  ],
  "rxList": [
    {
      "orderForName": "&lt;PLACER_NAME&gt;",
      "memberId": "<MRN>", "rxmrnrgn": "NCA", "rxmrn": "<MRN>",
      "rxNumber": "...", "drugName": "CHLORTHALID 25 MG TAB MYLA",
      "drugNdc": "00378022201", "drgSchdl": "6",
      "rxStatus": "INPROGRESS", "rxStatusDetails": "",
      "nextFillDate": "", "phcPhone": "8882186245",
      "quantity": 100,
      "imageUrl": "https://content.fdbcloudconnector.com/...",
      "ccinfo": [{ "lastfour": "<last4>", "exprndt": "YYMM",
                   "ntwrktyp": "AM", "pcitkn": "<wallet-token>" }],
      "copay": null, "nhinid": "...",
      "trackingId": ""
    }
  ],
  "shippingAddress": {
    "street1": "...", "city": "...", "state": "...",
    "zipCode": "...", "country": "US"
  }
}
```

### Notable shape quirks

- **`orderComittedDate` is misspelled** in Kaiser's response (one `m`). Send through verbatim from the parser; we don't normalize keys.
- **`paymentInfo` is a list, not an object.** Multi-card orders appear possible. We surface the full list in `RefillOrder.payment`.
- **`rxList[].copay` is `null` on `INPROGRESS` orders** even when the patient saw an estimated copay at placement. Probably populated once Kaiser's pharmacy actually adjudicates the claim. Surface as `None`, don't fabricate.
- **`trackingId` is empty string when not yet shipped.** Treat empty string and missing field identically — both mean "no tracker yet."
- **`rxList[].ccinfo` duplicates wallet payment details per-Rx.** We do NOT echo `pcitkn` (wallet token) or `lastfour` from the per-Rx ccinfo; the consolidated `paymentInfo[]` covers payment surface.
- **`cardType` → `cardImageCode` mapping confirmed.** Response carries both `"cardType": "American Express"` and `"cardImageCode": "AM"`, validating the lookup we use in `request_refill`'s wallet bridging.
- **`drgSchdl` is the DEA controlled-substance schedule** ("6" = unscheduled / non-controlled). Surface as a string; we don't interpret.

### Status transitions (only `INPROGRESS` captured)

`orderStatus` is API-side and presumably cycles `INPROGRESS` → `SHIPPED` (with `trackingId` populated) → `DELIVERED`. `digitalStatus` is the UI-friendly mirror ("In Progress" → presumably "Shipped" / "Delivered"). Surface both — `orderStatus` for programmatic checks, `digitalStatus` for human display. Live-verify the `SHIPPED` branch on the next real mail order.

### Out of scope

- **Cancel / modify endpoints.** Kaiser presumably exposes them (the UI has the buttons), but they aren't in this capture and are write actions — defer until there's a clear use case and a fresh HAR.
- **Per-Rx tracking-link expansion.** `trackingId` is a bare carrier number; resolving it to a USPS/UPS link is a separate concern.
