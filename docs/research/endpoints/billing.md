# Billing & Coverage endpoints

Source HAR: `docs/research/captures/kp-capture-various-with-phi.har`, 2026-05-06.
**Bodies for these calls were stripped from this HAR by Chrome (large-payload
truncation).** URLs, methods, headers, and response sizes are intact. Future
work needs a fresh capture with bodies preserved.

## Summary

A whole new domain — entirely on the BFF microservices host
`apims.kaiserpermanente.org`, not `healthy.kaiserpermanente.org/mychartcn/...`.
None of these are surfaced as MCP tools yet. All observed during a single
"Billing & Coverage" page click from the homepage.

| Endpoint | HTTP | Status | Size | Likely purpose |
| --- | --- | --- | --- | --- |
| `/kp/mycare/my-coverage-and-costs/mcc-bff-medicalbilling/v1/allregionbalance` | GET | 200 | 312 B | Outstanding balance across all KP regions. Small response = probably zero in our data. |
| `/kp/mycare/my-coverage-and-costs/mcc-bff-benefitsummary/v1/coverageListBenefits` | GET | **404** | 202 B | Coverage benefits list. **Returned 404** — gating, missing query param, or NorCal-vs-other-region mismatch. Worth re-investigating. |
| `/kp/mycare/kpd/hpa-balance-bff/v1/guarantorcheck` | GET | 200 | 54 B | Guarantor (who's responsible for the bill) check. Tiny = likely a boolean flag. |
| `/kp/mycare/ehpe-member-transition-bff/v1/member-transition` | GET | 200 | 1.2 KB | Coverage transitions (employer changes, retirement, qualifying-event windows). |
| `/kp/mycare/digital-identity/di-bff-userpreferences/v2/notifications` | GET | 200 | 12 KB | Notification preferences (paperless toggles, alert channels). Adjacent surface, fired by the same page. |
| `/kp/mycare/digital-identity/di-bff-userpreferences/v2/documents?envlbl=prod` | GET | 200 | 6.3 KB | Document-related preferences (paperless billing prompt, etc.). |

Page route: `https://healthy.kaiserpermanente.org/mc/billing` and
`/mc/billing-benefits-center` (AEM React shells that fan out into the BFF
calls above).

## Auth contract — DIFFERENT from pharmacy

This is the second BFF tier OpenKP has touched, and it does **not** use the
pharmacy header set. See `medications.md` for the pharmacy contract.

Observed for `mcc-bff-medicalbilling/allregionbalance`:

```
Origin: https://healthy.kaiserpermanente.org
Referer: https://healthy.kaiserpermanente.org/
Content-Type: application/json; charset=UTF-8
X-appName: bills-claims-ui
X-componentName: MCC
X-env: prod
X-region: HomeAndCAFH
X-useragentcategory: I
X-useragenttype: <full UA string>
```

Notably absent:
- No `X-IBM-client-Id`.
- No `x-guid`.
- No `X-KPSessionID`.
- No `__RequestVerificationToken` (this isn't `/mychartcn/`, no CSRF).

Auth is carried entirely by the cookies that ride from `.kaiserpermanente.org`
to `apims.kaiserpermanente.org` (same crossover trick we use for pharmacy) plus
the custom `X-appName` / `X-componentName` / `X-region` triplet.

`X-region: HomeAndCAFH` is a different sentinel from the pharmacy BFF's
`x-region: MRN`. **Don't normalize either** — the BFFs appear to expect their
own region encodings. Send what was observed verbatim until proven otherwise.

The `userpreferences` BFF (`digital-identity`) uses yet a third pattern:

```
x-appName: coverage-banner
x-featureName: Paperless Banner
x-ibm-client-id: Z5OkZ8UDwKze2sARCiRVDiFmBAodrRmJ
x-mobileRequest: false
x-osversion: Mac OS 10.15.7
x-s: 1
x-useragentcategory: B
x-useragenttype: desktop
```

Same takeaway: the BFF tier is heterogeneous. Capture headers per BFF.

## What's NOT visible

The HAR did not include the actual claims/bills detail endpoint (`Pay my
bill`, `Statement details`, etc.). Hugo only navigated to the landing page and
clicked a top-level link. A future capture should drill into:

- An individual statement (PDF or HTML view).
- The "Pay this bill" flow (a write tool on this domain would be a real Phase
  3 candidate).
- The `coverageListBenefits` 404 — replay with various query params or after
  navigating into a specific coverage card.

## Tool candidates (when bodies are mapped)

- `get_balance()` — single number, derived from `allregionbalance`.
- `list_coverages()` — once `coverageListBenefits` works, returns plan name,
  network, deductible, OOP max.
- `list_member_transitions()` — qualifying-event windows. Useful for advice
  scenarios ("you have 30 days to add dependent X").
- `get_notification_preferences()` / `set_notification_preferences()` —
  read+write pair on the userpreferences BFF.

None of these block v1. File this whole document under "Phase 4+ surface
expansion."
