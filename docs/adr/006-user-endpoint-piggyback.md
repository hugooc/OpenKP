# ADR-006: Piggyback on the pharmacy consumer identity for `/mycare/v1.0/user`

**Date:** 2026-04-23
**Status:** Active
**Authors:** Test Patient

## Context

Phase 2's first read tool, `get_profile`, needs to return the patient's
demographics, contact info, and insurance plan details. The obvious candidate
was Kaiser's "KPDL" consumer data layer at `/mycare/v1.0/uidatalayer/s/profile`
— simple headers, no X-apiKey contract, looks like a shared consumer endpoint.

In practice it doesn't work from a cold httpx client. KPDL is a write-through
layer that populates as a side effect of other Kaiser calls, primarily
`/mycare/v1.0/user`. The browser's page-load sequence shows this clearly: the
first five KPDL hits during page load return 212-byte empty shells; only after
`/mycare/v1.0/user` completes do subsequent KPDL reads return populated data.
See `docs/research/endpoints/profile.md` for the HAR trace.

Two options surfaced:

1. Prime KPDL by calling `/mycare/v1.0/user` first (with its full header
   contract), then read the populated KPDL response.
2. Cut out the middle layer and call `/mycare/v1.0/user` directly, parsing
   its response ourselves.

Option 1 requires calling `/mycare/v1.0/user` with the pharmacy header
contract anyway, then makes an extra round trip to read a thinner version
of the same data. Option 2 is the same trust concession for less work and
a richer payload.

The tradeoff is that `/mycare/v1.0/user` is served behind a "per-consumer
contract" gateway. Missing or mismatched headers return 502. Kaiser has
named the consumer identity we piggyback on `rx-order-management` with
`X-apiKey = kprwdpharmctr68973257122561335296`.

## Decision

OpenKP calls `GET /mycare/v1.0/user` with the pharmacy app's identity
headers, using `X-inclusionJsonPath` to request only the fields we need
for `get_profile`.

Concretely, OpenKP sends:

```
X-apiKey: kprwdpharmctr68973257122561335296
X-appName: rx-order-management
X-componentName: User Profile Component
X-versionId: 3.0.1.2
(plus the full X-* set from the HAR)
```

The actual authentication gate for the response is the session cookie —
Kaiser returns *this user's* data, not the pharmacy app's data, because
the session cookie identifies the user. The `X-apiKey` / `X-appName` pair
controls consumer routing and telemetry attribution, not user identity.

`X-inclusionJsonPath` limits the response to the fields OpenKP actually
consumes (name, DOB, gender, contact info, MRN, guid, region, plan info,
email, preferred name). Data minimization by request construction.

## Alternatives considered

**Prime KPDL then read from KPDL.** Requires the same pharmacy header
contract on the priming call, so the trust concession is identical, but
adds an extra round trip to read a thinner subset of the same information.
No benefit.

**Drive Playwright through the profile page and scrape.** Heavier. Every
profile read launches a browser tab. Useful as a fallback if Kaiser ever
tightens `X-apiKey` validation against the session cookie, but needless
overhead in the normal case.

**Use Kaiser's sanctioned FHIR patient API.** Read-only, capped to USCDI,
and explicitly the thing OpenKP exists to route around. See DESIGN.md
Section 2.

**Pick a different consumer identity.** OpenKP does not have a published
consumer identity with Kaiser. Any X-apiKey/X-appName we send is adopted,
not issued. The pharmacy identity is chosen because it's the one the HAR
shows Kaiser actually accepting for this endpoint at the user's account
scope.

## Risks and mitigations

**Risk: Kaiser rotates the X-apiKey.**
Mitigation: OpenKP handles this as "Phase 1 is suddenly broken." A new
HAR capture surfaces the new key in under an hour. The key lives in
`openkp/src/openkp/scrapers/profile.py` as a module constant for easy
rotation. This is not treated as a catastrophic failure mode.

**Risk: Kaiser starts validating `X-apiKey` → X-appName → session-user
consistency, e.g. "this X-apiKey is for pharmacy, the session belongs to
a member not enrolled in a pharmacy benefit → 403."**
Mitigation: if this happens, fall back to Playwright-driven page scraping
for `get_profile`. Document as a new ADR if we go there.

**Risk: Rate limiting attributed to the pharmacy app.**
Mitigation: OpenKP is a single-user local tool, calls are infrequent
(tens per day at most), and the pharmacy app's own browser traffic dwarfs
this. Low risk in practice, but worth monitoring if we ever see 429s.

**Risk: Telemetry/log attribution misrepresents the call origin.**
Mitigation: accepted. This is a fundamental consequence of adopting another
consumer's identity. OpenKP documents this behavior publicly in README and
ADR-006.

## Scope

This ADR applies only to `/mycare/v1.0/user` as the source for `get_profile`.
Other Kaiser endpoints may have their own consumer-contract requirements;
each will be evaluated and documented when its scraper module lands.
