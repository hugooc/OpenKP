# Kaiser Permanente login flow — endpoint map

**Source:** `docs/research/captures/kp-login-2.har` (captured 2026-04-22 in a fresh Chrome Incognito session; 1835 total requests, 35 to `identityauth.kaiserpermanente.org`).

**Summary:** Kaiser fronts Epic MyChart with PingFederate OAuth 2.0 authorization-code flow. The user submits credentials on Ping, receives a 302 back to `kp.org/pa/oidc/cb?code=...` which Kaiser's backend exchanges server-side for an Epic session. The Epic session is then bootstrapped through a 7-step redirect chain that lands the user on `/mychartcn/Home`. All authentication state after that is **cookie-based** (no Bearer tokens visible to the browser).

---

## 1. End-to-end redirect chain

Every step. Numbered so `auth.py` can reference them.

```
 #  verb  status  host + path                                                          → next
 1  GET   302     healthy.kaiserpermanente.org/sign-on                                 → /secure/inner-door
 2  GET   302     healthy.kaiserpermanente.org/secure/inner-door                       → identityauth.kaiserpermanente.org/as/authorization.oauth2?…
 3  GET   200     identityauth.kaiserpermanente.org/as/authorization.oauth2?…           [Ping login page HTML loads here]
     ───── user interacts with the Ping form: fills pf.username + pf.pass ─────
 4  POST  302     identityauth.kaiserpermanente.org/as/{SESSION_SEG}/resume/as/authorization.ping
                                                                                        → healthy.kaiserpermanente.org/pa/oidc/cb?code=…&state=…
 5  GET   302     healthy.kaiserpermanente.org/pa/oidc/cb?code=…&state=…                → /secure/inner-door
 6  GET   302     healthy.kaiserpermanente.org/secure/inner-door                       → /mychartcn/Home
 7  GET   302     healthy.kaiserpermanente.org/mychartcn/Home                          → /mychartcn/Authentication/Login?postloginurl=Home%3Flang%3Den-US
 8  GET   302     healthy.kaiserpermanente.org/mychartcn/Authentication/Login?…        → /mychartcn/default.asp?token={EPIC_HANDOFF_TOKEN}&js=1&canSetCookie=1&lang=en-US
 9  GET   302     healthy.kaiserpermanente.org/mychartcn/default.asp?token=…           → logincheck.asp
10  GET   302     healthy.kaiserpermanente.org/mychartcn/logincheck.asp                → inside.asp
11  GET   302     healthy.kaiserpermanente.org/mychartcn/inside.asp                    → /mychartcn/Home/HandleRedirect?absoluteUrl=…
12  GET   302     healthy.kaiserpermanente.org/mychartcn/Home/HandleRedirect?…         → /mychartcn/inside.asp?lang=en-US
13  GET   302     healthy.kaiserpermanente.org/mychartcn/inside.asp?lang=en-US         → ./Home?lang=en-US
14  GET   200     healthy.kaiserpermanente.org/mychartcn/Home?lang=en-US                [logged-in dashboard]
```

Steps 1–6 are Kaiser's own sign-on flow plus the PingFed callback. Steps 7–14 are **Epic MyChart's own internal session bootstrap** — Kaiser proxies Epic at `/mychartcn/` and has to hand off a one-time Epic token to let Epic set its own session cookies.

Observed timing: steps 1–3 took ~600 ms. Steps 4–14 took ~6 seconds end-to-end (after user finished entering credentials). The human interaction gap on the Ping page was 82 seconds in our capture.

---

## 2. OAuth authorize request (step 3)

```
GET https://identityauth.kaiserpermanente.org/as/authorization.oauth2
  ?response_type=code
  &client_id=KPORGOauthClientPAWebSessionV1
  &redirect_uri=https://healthy.kaiserpermanente.org/pa/oidc/cb
  &scope=openid
  &state={opaque-JWE-blob}
  &nonce=…
  &pi.target=…
```

Key params:

- `response_type=code` — standard OAuth2 authorization-code grant.
- `client_id=KPORGOauthClientPAWebSessionV1` — confirmed matches the recon doc and earlier HAR analytics traces.
- `redirect_uri=https://healthy.kaiserpermanente.org/pa/oidc/cb` — Kaiser's OIDC callback endpoint.
- `state` — encrypted JWE blob (alg=dir, enc=A128CBC-HS256, zip=DEF). We cannot decrypt; Kaiser's backend does. Just round-trip it.

**Implementation note:** We don't need to build this URL. Step 2 (`/secure/inner-door`) generates it fresh each time, including a new `state`. `auth.py` just navigates to `https://healthy.kaiserpermanente.org/sign-on` and follows redirects.

---

## 3. Credential POST (step 4)

The single authentication submission looks like this:

```
POST https://identityauth.kaiserpermanente.org/as/{SESSION_SEG}/resume/as/authorization.ping
Content-Type: application/x-www-form-urlencoded

pf.username={url-encoded-email}
pf.rememberUsername=on
pf.pass={url-encoded-password}
pf.ok=clicked
pf.adapterId=KpOrgHTMLFormAdapter
```

**Fields:**

| field | meaning |
|---|---|
| `pf.username` | Email login |
| `pf.pass` | Password |
| `pf.ok` | Literal string `clicked` (marks the submit button) |
| `pf.adapterId` | Always `KpOrgHTMLFormAdapter` (routes Ping to Kaiser's form adapter) |
| `pf.rememberUsername` | `on` if user checks "Save User ID" |

**Session segment:** `{SESSION_SEG}` is an 8–12 character random path segment (observed: `V2NC7W8Rk9`). It's embedded in the form's `action` attribute on the Ping login page and changes every session. Playwright handles this automatically by submitting the form. A raw-HTTP implementation would need to fetch step 3, parse the HTML, extract the `action=` URL, and submit to that exact URL.

**Response:** 302 with `Location: https://healthy.kaiserpermanente.org/pa/oidc/cb?code={AUTH_CODE}&state={OPAQUE_JWE}`. Ping redirects the browser directly. The backend exchange for tokens happens between Kaiser and Ping server-to-server (not visible to us).

---

## 4. Epic MyChart session handoff (steps 7–14)

After the OAuth callback, Kaiser needs Epic to set its own session cookies. The handoff uses a one-time token:

```
/mychartcn/default.asp?token={EPIC_HANDOFF_TOKEN}&js=1&canSetCookie=1&lang=en-US
```

Observed token: `2dd3d912-19c8-43e8-986b-522a3fe78fbe` (GUID format, one-time). Epic validates it and sets its own session cookies. Subsequent `/mychartcn/*` requests authenticate via those cookies.

**Implementation note:** `auth.py` only has to navigate once. Playwright / the browser follows all 7 redirects automatically. When step 14 lands on `/mychartcn/Home?lang=en-US` with status 200, the session is complete.

---

## 5. Marker for "logged in"

**Success criterion for `auth.py`:** after the sign-on flow completes, the browser is sitting on `https://healthy.kaiserpermanente.org/mychartcn/Home?lang=en-US` (or any `/mychartcn/*` path) with status 200.

**Lightweight authenticated probe** (for `refresh_session` and the `session_check` tool):

```
GET https://healthy.kaiserpermanente.org/mychartcn/keepalive.asp?cnt=1
Accept: */*
Referer: https://healthy.kaiserpermanente.org/mychartcn/Home?lang=en-US
X-Requested-With: XMLHttpRequest
```

Returns 200 with a tiny HTML body when the session is alive; 302 to Ping when dead. No per-consumer header contract — it's MyChart's own keepalive endpoint (the same one the UI pulses every ~30s), so any authenticated browser call to it works. **Confirmed working on 2026-04-22** via a real `session_check` run.

### Do NOT use `/mycare/v1.0/user` as a probe

An earlier draft of this doc listed `/mycare/v1.0/user` as a lightweight probe. It isn't. That endpoint is the **pharmacy app's User Profile Component** and the Kaiser edge gateway returns 502 Bad Gateway unless the request carries the pharmacy app's full header contract:

```
X-apiKey: kprwdpharmctr68973257122561335296
X-sessionToken: true
X-Requested-With: XMLHttpRequest
X-appName: rx-order-management
X-componentName: User Profile Component
X-versionId: 3.0.1.2
X-node18Version: 2.0.7
X-useragenttype: Desktop
X-useragentcategory: B
X-osversion: 0
X-includeProxyEntitlements: false
X-includeEntitlements: false
X-retainJsonSchema: true
X-inclusionJsonPath: $.UserAccountData.ebizAccountsWithPersonInfos.nameDetails;…
Accept: */*
Referer: https://healthy.kaiserpermanente.org/…
User-Agent: <real browser UA>
```

With just the XHR-style headers (`X-apiKey` + `X-sessionToken` + `X-Requested-With`): still **502**. The gateway wants the app-specific signature. Treat `/mycare/v1.0/user` as a pharmacy-app implementation detail, not a general session probe. Other `/mycare/v1.0/*` endpoints likely have similar per-consumer contracts; plan on discovering them per-endpoint rather than assuming a uniform API.

### Alternative probe (not yet tested)

```
GET https://healthy.kaiserpermanente.org/mychartcn/Home/CSRFToken?noCache={random}
```

Returns the Epic CSRF token. Would be useful to keep on hand once we start making Epic XHR writes that need a token — but keepalive is simpler for a pure alive/dead check.

---

## 6. Cookies and session state

Chrome's HAR export strips cookie values for privacy, so we cannot enumerate cookie names from this capture alone. But we know:

- **Cookie-based auth** — no Bearer tokens appear in post-login XHR requests. The only "auth"-like header on authenticated requests is `X-apiKey` (a static client-app identifier, not a user token), `X-sessionToken: true` (a hint flag), and standard client fingerprinting headers.
- **Cookies are scoped across `*.kaiserpermanente.org`** — kp.org and Epic's /mychartcn/ both share the parent domain, so a single cookie jar serves the whole flow.
- **Playwright resolves this automatically.** `context.cookies()` after step 14 will enumerate every cookie Ping and Epic set. We hand that to httpx as-is.

**Expected cookie families** (based on Ping + Epic architecture, to confirm at runtime):

| family | typical names | purpose |
|---|---|---|
| Ping session | `PF`, `PF.PERSISTENT`, `pf-hfa-*` | Ping Federate SSO session; may carry "trust this device" state |
| Kaiser session | `JSESSIONID`, `KpRoute`, `x-pf-*` | Kaiser's server-side session on kp.org |
| Epic MyChart | `EpicCN-*`, `MyChartSession`, `.EPiC-*` | Epic's per-session cookies set after step 8 |
| Load balancer | `AWSALB*`, `AWSALBCORS`, `BIGipServer*` | Kaiser's edge load balancing |
| Client app | `OptanonConsent`, `OptanonAlertBoxClosed` | OneTrust consent (not auth-relevant) |

These are educated guesses. `auth.py` should log cookie names (not values) on first successful login to confirm, then gate future work on the ones that actually appear.

---

## 7. MFA — not triggered in this capture

**The captured login did not prompt for MFA.** After the credential POST (step 4), the flow went directly to 302 → auth code → back to kp.org. No second-factor POST, no push notification wait, no WebAuthn assertion.

Why: Kaiser's Ping uses **adaptive risk scoring**. The assets loaded on the Ping page include:

- `/assets/scripts/fingerprint2-2.1.4.min.js`
- `/assets/scripts/pingone-risk-management-profiling.js`
- `/assets/scripts/pingone-risk-management-embedded.js`

These fingerprint the browser and compute a risk score. For a user + device pair with an acceptable risk profile, Ping skips MFA. This matches Ping's standard "Risk Policy" adapter behavior.

**Implication for `auth.py`:**

1. The first Playwright-driven login from a brand-new persistent profile will **almost certainly trigger MFA** because Ping has no fingerprint history for that profile. Design the login flow to wait for the user to complete an MFA challenge if one appears.
2. Subsequent logins from the same persistent profile will usually not trigger MFA (the profile accumulates a "trusted" fingerprint + Ping cookies).
3. MFA detection pattern: after the credential POST, if the response is not an immediate 302 to `/pa/oidc/cb`, we're on an MFA challenge page. Expect URLs under `/as/{SESSION_SEG}/resume/as/authorization.ping` with different form adapters (SMS OTP, push, WebAuthn).

**We need a separate capture with MFA triggered** to map the MFA challenge endpoints. This is a Phase 1 follow-up, not a blocker — the first real Playwright login will generate it naturally.

---

## 8. Client fingerprinting concerns

Third-party telemetry fires heavily on the Ping page:

- **Quantum Metric** (`ingest.quantummetric.com`) — 216 requests in this capture
- **PingOne Risk** (client-side fingerprinting)
- **Fingerprint2** (browser feature hashing)
- **Dynatrace RUM** (`bf78095soe.bf.dynatrace.com`)

A headless browser with no user agent rotation and a virgin Chromium profile will stand out. For the first-run Playwright flow, we run **headed** (visible to the user, who is interacting with it anyway for MFA). For `refresh_session`, we may be able to run headless once the profile has accumulated trust — but this is worth testing and potentially tuning (keep the same user agent, viewport, timezone, language as a real user).

---

## 9. Implementation implications for `auth.py`

**Keep:** the strategy in the current stub. Playwright persistent context. First run headed. Subsequent runs headless.

**Concrete steps `login_interactive()` should execute:**

1. Launch Chromium via Playwright with `launch_persistent_context(user_data_dir=data_dir / "chromium-profile", headless=False, ...)`.
2. Navigate to `https://healthy.kaiserpermanente.org/sign-on`.
3. Let Playwright follow redirects to the Ping page automatically.
4. If the URL matches `identityauth.kaiserpermanente.org/as/authorization.oauth2`, we're on the login form. Fill `input[name="pf.username"]` and `input[name="pf.pass"]`, then click the submit button (or submit the form).
5. Wait for the URL to settle. Possible outcomes:
   - URL contains `/mychartcn/Home` — success, no MFA triggered.
   - URL still on `identityauth.kaiserpermanente.org/as/*/resume/as/authorization.ping` with MFA elements — wait for user to complete the challenge in the visible browser, poll URL every 500 ms, timeout after 5 minutes.
6. Once on `/mychartcn/Home?lang=en-US`, extract cookies via `context.cookies()` and return a `KaiserSession`.

**Concrete steps `refresh_session()` should execute:**

1. Open Playwright with the same persistent context, headless.
2. Navigate to `https://healthy.kaiserpermanente.org/mychartcn/keepalive.asp?cnt=1` (the lightweight probe).
3. If status is 200, we're still logged in — extract cookies, return the session.
4. If redirected to `identityauth.kaiserpermanente.org/as/*`, the session is dead — return `None`, caller will invoke `login_interactive()`.

**Observed on 2026-04-22:** headless refresh bounced to Ping within 2 minutes of a fresh headed login. The persistent Chromium profile does not preserve the KP session across a headed → headless transition well — almost certainly because the headless browser's fingerprint differs from the headed one that established the session, and Ping's risk adapter invalidates the session on fingerprint mismatch. Two options to investigate:

- Run `refresh_session` **headed** but minimized/offscreen. Kills the "no UI interruption" property but preserves fingerprint continuity.
- Keep a background keepalive loop pulsing `/mychartcn/keepalive.asp` every ~30s while the MCP server is up, holding the session open so re-opening the browser isn't needed between calls.

**Session-expiry detection in `request.py`:** already correct. A 302 with `Location:` containing `identityauth.kaiserpermanente.org` is the unambiguous expiry signal. Watch for it on every response.

---

## 10. Open questions to resolve during implementation

1. **Cookie names that actually matter.** Log them from the first real login to confirm which ones are load-bearing vs. analytics noise. **Partial answer (2026-04-22):** 55–56 cookies set on a fresh login. Names observed include `MyChartAccessToken4mychartcn`, `MyChartSessionToken4mychartcn`, `MyChartNetAuthenticationTicket4mychartcn`, `MyChart_Session`, `KPSessionId`, `HSESSIONID`, `ImpSessionRoP`, `PF`, `PF.PERSISTENT`, `isAuth`, `isSignedOn`. Which subset is load-bearing is still TBD — handing all 56 to httpx works.
2. **Keepalive cadence.** Both `/mychartcn/keepalive.asp?cnt=N` and `/mychartcn/Home/KeepAlive?cnt=N` fire every ~30s in the UI. Do we need to pulse them to keep the session alive between MCP calls, or is session lifetime generous enough to skip keepalives? **Partial answer (2026-04-22):** headless re-entry bounces to Ping within minutes (see section 9). A keepalive loop is very likely needed if we want silent refresh to work without re-launching the headed browser.
3. **MFA pathway map.** Needs a separate capture where MFA is forced (e.g., log in from a never-before-used device, or force Ping to challenge).
4. **Mobile OAuth shortcut.** The recon doc flagged that Kaiser's mobile app may use a PKCE-native public OAuth client. Still worth investigating, but the web flow is implementable today.
5. **`apims.kaiserpermanente.org` auth mechanism.** The pharmacy BFF at `apims.*` uses `X-apiKey` + presumably cookies. Needs confirmation for Phase 2 pharmacy tools.

---

## 11. What this HAR does NOT contain

- **No Ping login page HTML body.** Chrome's HAR strips document response bodies by default. The form fields are known from the POST capture (section 3), so this isn't blocking.
- **No cookie names or values.** Chrome redacts. Playwright will fill this in at runtime.
- **No MFA challenge flow.** This login didn't trigger MFA.
- **No token exchange endpoint.** Kaiser does the auth-code → access-token exchange server-side, behind `/pa/oidc/cb`. We never need to see it.

None of these are blockers for starting `auth.py`. They're runtime-observable gaps to close on first real login.
