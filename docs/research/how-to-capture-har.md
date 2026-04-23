# How to capture a kp.org login HAR

This HAR becomes the source of truth for the Kaiser Ping OAuth flow. We'll map every redirect, CSRF token, and cookie set during login, then replicate it in `openkp/scrapers/auth.py`.

**Captures contain session cookies and possibly PHI.** The `docs/research/captures/` directory is gitignored, as are all `*.har` files under `docs/research/`. Keep HARs there. Never commit one.

## What you need

- Google Chrome (or any Chromium-based browser with DevTools)
- A valid kp.org login + whatever MFA method you normally use
- About 10 minutes

## Step-by-step

### 1. Open a clean browser context

Use an Incognito window. This keeps existing kp.org cookies out of the capture so we see the full login from scratch.

```
Chrome -> File -> New Incognito Window
```

### 2. Open DevTools and set up recording

Once the Incognito window is open but **before** you navigate to Kaiser:

```
View -> Developer -> Developer Tools  (or Cmd+Opt+I)
```

Click the **Network** tab in DevTools. Then:

- Check **Preserve log** (top of the Network tab). This keeps requests across redirects.
- Check **Disable cache**.
- Leave the filter set to **All** (not XHR, not Fetch).
- Click the red circle to confirm recording is active. It should be red, not gray.

### 3. Drive the login

In the Incognito window's address bar, navigate to:

```
https://healthy.kaiserpermanente.org/sign-on
```

DevTools should immediately start showing a flurry of requests. That's the redirect chain into PingFederate.

Complete the full login:

1. Enter your **username and password**.
2. Complete your **MFA challenge** (SMS code, push notification, WebAuthn, whichever you use).
3. If prompted with "Trust this device" or similar, **choose trust this device**. We want to capture what that option sets so we can persist it.
4. Wait until you land on the authenticated dashboard (usually `healthy.kaiserpermanente.org/secure/inner-door` or similar).

Keep DevTools recording through every step. If the Network tab pauses recording when you hit a new page, verify **Preserve log** is still checked.

### 4. Optional: click one authenticated thing

Once you're on the dashboard, click something that loads real data (e.g. **Messages** or **Medications**). This captures at least one post-login authenticated request so we can see what kind of auth header or cookie the portal uses for actual API calls.

### 5. Export the HAR

In the DevTools Network tab:

- Right-click anywhere in the request list.
- Choose **Save all as HAR with content**.
- Save to:

```
~/OpenKP/docs/research/captures/kp-login-1.har
```

"With content" is the important part. It includes response bodies, which we need to parse Ping's login forms and see what the auth cookie looks like.

### 6. Tell Claude

Come back to the OpenKP project chat and say "HAR is saved." Claude will parse it, document every endpoint in `docs/research/endpoints/auth.md`, and build the auth implementation from that map.

## What to look for during capture (optional, for curiosity)

You'll see requests bounce across these hosts, roughly in this order:

1. `healthy.kaiserpermanente.org` (landing, sends you toward Ping)
2. `identityauth.kaiserpermanente.org` (Ping Federate, the actual auth server)
3. Possibly `healthyroster.kaiserpermanente.org` or similar for device fingerprinting
4. Back to `healthy.kaiserpermanente.org` with session cookies set

Look for:

- **`client_id=KPORGOauthClientPAWebSessionV1`** in the authorize URL. This is the OAuth client we're piggybacking on.
- **`pf.username` and `pf.pass`** as form field names on the login POST. Ping's convention.
- **A CSRF token** in a hidden input (often `_csrf` or `lt`).
- **A `ping-device-trust` or `PF` cookie** that may be the long-lived "trust this device" credential.

Don't worry about memorizing these. Claude will pull them out of the HAR.

## Troubleshooting

**"Preserve log" grayed out.** You're not in the Network tab. Click Network first.

**HAR file is huge (>50 MB).** That's fine. HAR files are verbose. We can parse 100 MB easily.

**HAR file is tiny (<100 KB).** The recording didn't capture the login. Try again, making sure the red record button is on before you navigate.

**MFA fails in Incognito.** Some WebAuthn platform authenticators refuse Incognito. Use a regular window instead, but sign out first to ensure we capture the login from scratch. Delete the capture when done (never commit).

**You get a "suspicious activity" email from Kaiser.** This is normal for a login from a new browser session, especially one with DevTools open. It doesn't break anything.

## After capture

The HAR lives in your local workspace only. When we're done extracting what we need, you can delete it:

```bash
rm ~/OpenKP/docs/research/captures/kp-login-1.har
```

Or keep it for reference. Either way, it never leaves your machine.
