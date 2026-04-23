# ADR-005: Interactive first-run auth, silent subsequent runs

**Date:** 2026-04-22
**Status:** Active
**Authors:** Test Patient

## Context

ADR-002 commits us to Playwright for the login layer. That settles *how* we drive the browser, but it leaves an open UX question about *who* completes the login: fully automated, or human-in-the-loop.

Kaiser's Ping Federate tenant uses adaptive risk scoring. The HAR analysis in `docs/research/endpoints/auth.md` confirms the Ping login page loads Fingerprint2, PingOne Risk, Quantum Metric, and Dynatrace RUM scripts that compute a device risk score. For a user and device pair with an acceptable score, Ping skips MFA entirely. For a virgin profile, Ping almost certainly challenges with one of SMS OTP, email OTP, push notification, or WebAuthn.

A persistent Chromium profile under Playwright accumulates Ping's trust cookies and fingerprint continuity over time. The first login from a fresh profile is the adversarial case. Every subsequent login benefits from whatever trust state that first login established.

The question: when OpenKP needs credentials to land in `/mychartcn/Home`, does it do so fully headlessly (with MFA automated somehow), or does it open a visible browser the first time and let the user complete the challenge by hand?

## Decision

**First run:** Playwright launches Chromium headed with a persistent user data dir under `~/.openkp/chromium-profile`. OpenKP reads the Kaiser username and password from the OS keychain and autofills the Ping form. The user completes any MFA challenge Ping presents in the visible browser window. Playwright polls the page URL until it lands on `/mychartcn/Home`, then extracts cookies into a `KaiserSession`.

**Subsequent runs:** Playwright reopens the same persistent profile, headless. OpenKP hits the lightweight authenticated probe `GET https://healthy.kaiserpermanente.org/mycare/v1.0/user`. If it returns 200 with a JSON body, the session is alive and we extract cookies. If it redirects to `identityauth.kaiserpermanente.org`, the session is dead and we fall back to the interactive first-run flow.

**Scope boundary:** OpenKP never holds or handles an MFA factor. No TOTP shared secret, no SMS scraping, no email scraping, no push-approval automation. Credentials are limited to what the user explicitly placed in keyring, which is the username and password only.

## Alternatives considered

**Fully headless from day one, with TOTP automation.** Rejected. Storing a TOTP shared secret in the same keyring that holds the password collapses a second factor into the first. It also presumes Kaiser offers TOTP, which the current Ping adapter does not advertise. Adaptive risk would also be more likely to challenge a virgin headless profile with WebAuthn or push, neither of which TOTP helps with.

**Scrape MFA codes from SMS or email.** Rejected. Reading SMS requires macOS Messages or a cloud provider integration. Reading email requires IMAP credentials or an OAuth scope for the user's mail provider. Each of those adds a new credential surface and a new trust boundary. A user at the keyboard is a simpler and more secure answer.

**Skip credential autofill entirely, require the user to type username and password every interactive session.** Rejected as friction for no safety gain. Keyring already holds the credentials. Making the user type them adds steps without removing any attack surface.

**Jump straight to browser-extension architecture (Phase 5) and piggyback on the user's existing Chrome session.** Rejected as Phase 1 scope. Phase 5 remains the right answer if Playwright maintenance becomes painful, but we need the Playwright path working first to have something to compare against.

**Run headless on first run too, and hope the user's keyring-stored device trust cookie from a prior manual login is enough.** Rejected. A fresh OpenKP install has no prior login. The first run has to produce the trust state, so it cannot depend on it.

## Consequences

**We commit to:**
- A visible Chromium window on first run, including any time a session expires past Ping's device-trust window
- A persistent user data dir under `~/.openkp/chromium-profile`, protected by normal file permissions
- Credential autofill limited to username and password, read from keyring
- A polling loop in `login_interactive()` that waits for the user to complete MFA in the visible browser
- The `/mycare/v1.0/user` probe as the canonical "is the session alive" check
- Never implementing TOTP, SMS scraping, email scraping, or push-approval automation in-process

**We give up:**
- Fully automated CI-style login. OpenKP is a user-facing tool, so this is not a real loss for Phase 1 through Phase 4.
- Any claim that OpenKP can run on a headless server. The architecture assumes a user at a keyboard on first run.

**We gain:**
- MFA method flexibility. Whatever Kaiser offers (SMS, email, push, WebAuthn), the user handles it in the browser. OpenKP never has to learn a new MFA pathway.
- A clean security story: OpenKP sees the password once per session refresh and never sees any MFA factor at all.
- Alignment with Kaiser's own device-trust model. We are not fighting Ping's risk scoring, we are cooperating with it.
- A small, auditable auth code path. `login_interactive()` can stay under a hundred lines because it delegates MFA to the human.

## Implementation notes (non-binding)

- First-run polling and timeout values live in `auth.py` and are free to evolve with experience. They are not architectural commitments.
- Subsequent-run mode may be relaxed to headed if headless turns out to fail Ping's risk scoring. The commitment is to silent by default, not strictly to headless.
- Cookie names observed on first real login should be logged (names only, never values) to close the gap left by HAR redaction.

## Status

Active. Supersede only if:

- Kaiser adds device attestation (hardware-backed, not just cookie-backed) that Playwright cannot satisfy. In that case, Phase 5 browser-extension architecture becomes the primary path and this ADR is replaced by one describing that shift.
- A future ADR defines a CI or server deployment path for OpenKP, which would require rethinking the user-at-keyboard assumption.
